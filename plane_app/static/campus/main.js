/* The campus (spec 2026-10-11-campus-looks-design.md §2): mount(el, opts) → controller. Loads
   /api/campus, draws it with scene.js, and shows the chosen period: lessons lit, free teachers walking,
   clashes pinned. campus-boot.js drives it (look, active, resize, reloads, grid selection).
   The setup tabs (spec 2026-10-13-setup-campus §1, §4): setMode(mode) — project | manpower | locations |
   requirements | constraints draws that tab's scene from /api/campus/setup (the school from doc.campus, then
   setup.js's buildScene into scene.overlay()); 'build' is everything above, as it was. */
import { createScene } from 'campus/scene';
import { buildScene, resetScenes } from 'campus/setup';
import { colours } from 'campus/themes';
import * as D from 'campus/data';
import { createUI } from 'campus/ui';
import { createStages } from 'campus/stages';

export async function mount(el, opts) {
  const probe = document.createElement('canvas');
  const gl = probe.getContext('webgl2') || probe.getContext('webgl');
  if (!gl) throw new Error('no WebGL');
  const lose = gl.getExtension('WEBGL_lose_context'); if (lose) lose.loseContext();
  const stage = document.createElement('div'); stage.className = 'campus-stage';
  el.appendChild(stage);
  const scene = createScene(stage);
  const SETUP = new Set(['project', 'manpower', 'locations', 'requirements', 'constraints']);
  let mode = SETUP.has(opts.mode) ? opts.mode : 'build';
  let doc = null, look = opts.look, t = null, palette = {}, active = false, stale = false, ui = null, loads = null, allClashes = false, seq = 0, marked = null, layoutKey = null, builtFinished = false;
  stage.tabIndex = 0;
  stage.setAttribute('role', 'application');
  const ARIA = 'Campus view. Left and right arrows change the period; find a teacher or room below.';
  const SETUP_ARIA = 'Campus view of this tab. Click a figure, tower, building or tile for its facts and its row in the table.';
  stage.setAttribute('aria-label', mode === 'build' ? ARIA : SETUP_ARIA);   // mounted straight onto a setup tab: its label

  const colourOf = (key) => palette[key] || '#9aa6b2';
  const deptColour = (p) => palette['d:' + p.dept] || '#9aa6b2';

  function showPeriod() {
    if (!doc) return;
    const byRoom = D.occupancy(doc.lessons, t);
    scene.setOccupancy(byRoom, colourOf);
    const pins = [];
    if (allClashes) {
      const counts = D.clashCounts(doc.clashes);
      for (const [r, n] of Object.entries(counts)) pins.push({ roomId: r, text: n === 1 ? '1 clash' : n + ' clashes' });
    } else {
      for (const c of D.clashesAt(doc.clashes, t)) for (const r of c.rooms) if (!pins.some((p) => p.roomId === r)) pins.push({ roomId: r, text: c.message });
    }
    scene.setPins(pins);
    scene.setWalkers(D.freeTeachers(doc.people, doc.lessons, t).slice(0, 14), deptColour);
    if (ui) { ui.setPeriod(t); ui.refresh(); }
    counters();
  }

  /* ---- the counters (spec §3.3): the journey's lessons, teachers and clashes; rooms in use at this period ---- */
  const motion = () => (window.planeMotion ? window.planeMotion.level() : 'full');
  let jc = (window.planeJourney && window.planeJourney.state() && window.planeJourney.state().counters) || null;
  function counters() {
    if (!doc || !ui || mode !== 'build') return;                // a setup scene shows its own counters
    const c = jc || {}, num = (v) => (typeof v === 'number' ? v : null);
    ui.setCounters({
      lessons: num(c.lessons_total) != null ? [num(c.lessons_placed) || 0, c.lessons_total] : null,
      teachers: num(c.teachers_total) != null ? [num(c.teachers_deployed) || 0, c.teachers_total] : null,
      rooms: [Object.keys(D.occupancy(doc.lessons, t)).length, doc.rooms.length],
      clashes: num(c.clashes) != null ? c.clashes : (doc.clashes || []).length,
    }, motion() === 'off');
  }
  let timetable = null;
  const onState = (e) => {
    jc = (e.detail && e.detail.counters) || null; counters();
    const id = e.detail && e.detail.timetable;               // another timetable: its setup scenes play their intros
    if (timetable && id && id !== timetable) { switched(); forgetSetup(); refreshSetup().catch(() => {}); }
    if (id) timetable = id;
  };
  const onMotion = () => { if (ui) ui.setMotion(motion()); if (presenting) scene.fx.autoRotate(!presentPaused && motion() === 'full', 0.4); };
  document.addEventListener('plane:journey-state', onState);
  document.addEventListener('plane:motion', onMotion);
  const onTab = () => requestAnimationFrame(() => { if (presenting && !el.clientWidth) present(false); });   // another tab: leave
  document.addEventListener('plane:tab', onTab);

  /* ---- presentation mode (spec §3.3): full screen only; the build once, then a period every 4 s ---- */
  let presenting = false, presentTimer = 0, presentPaused = false, presentGen = 0;
  function present(on) {
    if (on ? presenting || !active || !el.closest('.expanded') : !presenting) return;
    presenting = !!on; presentPaused = false; const gen = ++presentGen;
    clearInterval(presentTimer); presentTimer = 0;
    ui.setPresenting(presenting);
    if (!presenting) {                                       // back to the everyday view
      scene.fx.autoRotate(false);
      if (stages.busy()) { stages.stop(); reload(true).catch(() => {}); }
      return;
    }
    scene.fx.autoRotate(motion() === 'full', 0.4);
    (motion() === 'off' ? Promise.resolve() : stages.replay()).catch(() => {}).then(() => {
      if (!presenting || presentTimer || gen !== presentGen) return;      // left, or a newer presentation owns the stepping
      presentTimer = setInterval(() => {
        if (presentPaused || document.hidden || !doc) return;
        const total = ((doc.time && doc.time.labels) || []).length || 1;
        t = (t + 1) % total; showPeriod();
      }, 4000);
    });
  }
  function pausePresenting() {
    if (!presenting) return;
    presentPaused = !presentPaused;
    scene.fx.autoRotate(!presentPaused && motion() === 'full', 0.4);
  }
  function markRoom(id) {                                   // the grid's lesson: its room, without moving the period
    if (!doc || !id) { scene.highlight(null); return; }
    for (const [room, list] of Object.entries(doc.lessons)) if (list.some((x) => x.event === id)) { scene.highlight(room); return; }
    scene.highlight(null);
  }

  async function reload(force) {
    if (force) layoutKey = null;                             // after a replay or a handover: rebuild the true school
    if (!active) { ++seq; stale = true; return; }            // and any reload already in flight is now old
    const n = ++seq;
    const r = await fetch('/api/campus', { credentials: 'same-origin' });
    if (!r.ok) throw new Error('campus ' + r.status);
    const next = await r.json();
    if (n !== seq) return;                                   // a newer reload is on its way
    doc = next;
    stale = false;
    palette = colours(doc.legend);
    const total = ((doc.time && doc.time.labels) || []).length || 1;
    if (t === null) t = D.currentSlot(doc.time, new Date());
    t = Math.max(0, Math.min(t, total - 1));
    loads = null;
    if (doc.source === 'live') {
      fetch('/api/loads', { credentials: 'same-origin' }).then((x) => (x.ok ? x.json() : null)).then((j) => { if (n === seq) loads = (j && j.loads) || null; }).catch(() => {});
    }
    if (mode !== 'build') { drawSetup(); return; }           // kept for Build; a setup scene follows the check (Construction)
    drawBuild();
  }
  /* Build's school and period from the doc: a timetable change re-lights the same school; only a new layout
     (or look, or a setup scene drawn in between) is rebuilt */
  function drawBuild() {
    const key = JSON.stringify([doc.buildings, doc.areas, doc.grounds, doc.checked_ok, doc.rooms.map((x) => x.home), look]);
    const sameLayout = key === layoutKey;
    layoutKey = key;
    if (!sameLayout) { scene.build(doc, look, !!doc.checked_ok); builtFinished = !!doc.checked_ok; }
    ui.setDoc(doc, palette);
    showPeriod();
    if (marked) markRoom(marked);                            // keep the grid's selection, and the period
  }

  /* ---- the setup tabs' scenes ---- */
  let sdoc = null, sText = null, sVer = 0, sSeq = 0, sStale = true, sBusy = false, shownKey = null, picked = null;
  async function refreshSetup() {                            // GET /api/campus/setup; redrawn only when it changed
    if (mode === 'build') { sStale = true; return; }
    if (!active || switching) { ++sSeq; sStale = true; sBusy = false; return; }   // hidden, or a switch still settling
    const n = ++sSeq;
    let text;
    sBusy = true;
    try {
      const r = await fetch('/api/campus/setup', { credentials: 'same-origin' });
      if (!r.ok) throw new Error('campus setup ' + r.status);
      text = await r.text();
    } catch (err) {
      if (n === sSeq) { sBusy = false; if (mode !== 'build' && !sdoc) ui.setMode(mode, { legend: [], counters: [], note: 'The campus could not load this tab just now.' }); }
      throw err;
    }
    if (n !== sSeq) return;
    sBusy = false;                                  // a newer load is on its way, or the tab changed to Build
    sStale = false;
    if (text !== sText) { const next = JSON.parse(text); sText = text; sdoc = next; sVer++; }
    drawSetup();
  }
  function drawSetup(force) {
    if (mode === 'build' || !sdoc) return;                   // a scene is drawn only once its document is here
    const finished = !!(doc && doc.checked_ok);
    const key = [mode, look, sVer, finished].join('|');
    if (!force && key === shownKey) return;
    shownKey = key;
    const c = sdoc.campus || {};
    scene.build({ grounds: c.grounds, areas: c.areas || [], buildings: c.buildings || [], rooms: c.rooms || [] }, look, finished);
    layoutKey = null; builtFinished = finished;              // Build rebuilds its own school when it shows again
    const ov = scene.overlay();
    const out = buildScene(mode, sdoc, ov ? ov.kit : null, { showSchool: scene.showSchool, colourRooms: scene.colourRooms, fx: scene.fx });
    ui.setMode(mode, out);
    if (picked && !ui.showSetup(setupCard(picked))) picked = null;   // an open card follows the data; gone → closed
  }
  /* another timetable: nothing of the old one's document may draw again — not on a tab change while the new
     one loads, not in a card; the tab's scene is emptied to bare grounds until the new document is here */
  function forgetSetup() {
    resetScenes();
    const grounds = sdoc && sdoc.campus && sdoc.campus.grounds;
    sdoc = null; sText = null; shownKey = null; picked = null;
    ++sSeq; sBusy = false; sStale = true;                    // a load still in flight answers for the old timetable
    if (mode === 'build') return;
    ui.hideCard();
    scene.build({ grounds, areas: [], buildings: [], rooms: [] }, look, false);
    layoutKey = null;
    ui.setMode(mode, { legend: [], counters: [], note: '' });
  }
  /* the user's switch: forgotten at once (the next journey state names the new one), and no load until the
     page's own reload after the switch (plane:solid / plane:plan), which cannot answer for the old timetable */
  const ttPick = document.getElementById('timetable');
  let switching = 0;
  const onPickTimetable = () => {
    forgetSetup(); timetable = null;
    clearTimeout(switching); switching = setTimeout(() => { switching = 0; if (mode !== 'build') refreshSetup().catch(() => {}); }, 3000);
  };
  const switched = () => { clearTimeout(switching); switching = 0; };
  if (ttPick) ttPick.addEventListener('change', onPickTimetable);
  function setMode(next) {
    const m = SETUP.has(next) ? next : 'build';
    if (m === mode) { if (m !== 'build' && sStale && !sBusy) refreshSetup().catch(() => {}); return; }
    const was = mode;
    mode = m; picked = null;
    if (was === 'build') {                                   // leaving Build: nothing of it keeps running here
      present(false); ui.stop();
      if (stages.busy()) { const left = stages.stop(); if (opts.hold) left.forEach((d) => opts.hold(d)); }
    }
    if (m === 'build') {
      resetScenes(); shownKey = null; ++sSeq; sStale = true; sBusy = false;   // the next setup tab plays its intro again
      stage.setAttribute('aria-label', ARIA);
      ui.setMode('build');
      layoutKey = null;
      if (doc) drawBuild();
      if (stale && active) reload().catch(() => {});
      return;
    }
    stage.setAttribute('aria-label', SETUP_ARIA);
    if (sdoc) drawSetup();
    else {                                                   // until the document is here: Build's lessons and walkers go
      scene.setOccupancy({}, colourOf); scene.setPins([]); scene.setWalkers([], deptColour); scene.highlight(null);
      ui.setMode(m, { legend: [], counters: [], note: '' });
    }
    if (was === 'build' || sStale || !sdoc) refreshSetup().catch(() => {});
  }
  const onData = () => { switched(); if (mode === 'build') sStale = true; else refreshSetup().catch(() => {}); };
  document.addEventListener('plane:plan', onData);
  document.addEventListener('plane:readings', onData);

  /* ---- what the cards show ---- */
  const byId = (list, id) => (list || []).find((x) => x.id === id) || null;
  const label = (n) => (doc.time.labels || [])[n] || 'P' + (n + 1);
  const names = (ids) => (ids || []).map((id) => (byId(doc.people, id) || { name: id }).name).join(', ');
  function roomInfo(id) {
    const room = byId(doc.rooms, id); if (!room) return null;
    const list = doc.lessons[id] || [], now = D.occupancy({ [id]: list }, t)[id] || null, nx = D.nextLesson(list, t);
    return {
      room, now, nowTeachers: now ? names(now.teachers) : '',
      next: nx ? { subject: nx.subject, label: label(nx.t0) } : null,
      used: D.usedPeriods(list), total: (doc.time.labels || []).length,
      clashes: D.clashesAt(doc.clashes, t).filter((c) => c.rooms.includes(id)).map((c) => c.message),
    };
  }
  function teacherInfo(id) {
    const person = byId(doc.people, id); if (!person) return null;
    const spd = doc.time.slots_per_day || 1, day = Math.floor(t / spd);
    const at = D.whereIs(id, doc.lessons, t);
    let next = null;
    for (let s = t + 1; s < (day + 1) * spd && !next; s++) {
      const w = D.whereIs(id, doc.lessons, s);
      if (w && w.lesson.t0 === s) next = { subject: w.lesson.subject, roomName: (byId(doc.rooms, w.room) || { name: w.room }).name, label: label(s) };
    }
    const l = loads && loads[id];
    return {
      person, load: l ? { load: l.load, max: Number.isFinite(l.max_load) ? l.max_load : null } : null,
      where: at ? { room: at.room, roomName: (byId(doc.rooms, at.room) || { name: at.room }).name, lesson: at.lesson } : null,
      next, free: D.freePeriods(id, doc.lessons, day, spd).map((p) => D.labelParts(doc.time.labels, day * spd + p, spd, doc.time.days).period),
    };
  }

  ui = createUI(el, {
    period(next) { t = next; showPeriod(); },
    info: { room: roomInfo, teacher: teacherInfo },
    search: (q) => (doc ? doc.people.filter((p) => p.name.toLowerCase().includes(q)).map((p) => ({ kind: 'teacher', id: p.id, name: p.name }))
      .concat(doc.rooms.filter((r) => r.name.toLowerCase().includes(q)).map((r) => ({ kind: 'room', id: r.id, name: r.name }))) : []),
    allClashes(on) { allClashes = on; showPeriod(); },
    select: (eventId) => opts.select && opts.select(eventId),
    focus: (roomId) => { scene.focusRoom(roomId); scene.highlight(roomId); },
    replay: () => { if (active && !presenting) stages.replay(); },
    present,
    pause: pausePresenting,
    onClose: () => { picked = null; },                     // closed by the user: a redraw does not reopen it
  });
  ui.setMotion(motion());
  if (mode !== 'build') ui.setMode(mode, { legend: [], counters: [], note: '' });
  scene.onPick((hit) => {
    if (mode === 'build') { if (hit.kind === 'room') ui.showRoom(hit.id); else if (hit.kind === 'teacher') ui.showTeacher(hit.id); return; }
    // a setup scene's own picks; the school's rooms only on Locations (a ghost school's pitches still answer)
    const p = hit.kind === 'setup' ? hit.hit : hit.kind === 'room' && mode === 'locations' ? { type: 'room', id: hit.id } : null;
    if (!p) return;
    picked = ui.showSetup(setupCard(p)) ? p : null;
  });

  /* ---- the setup cards (spec §2): the facts of what was clicked, and its row in the tab's table ---- */
  const planNow = async () => {                              // plan.js's plan, loaded if it is not yet
    const P = window.planeSetup;
    if (P && !P.plan() && window.loadPlan) await window.loadPlan();
    return (P && P.plan()) || null;
  };
  const flashRow = (table, id) => (window.planeSetup ? window.planeSetup.highlightRow(table, id) : false);
  const staffName = (id) => ((sdoc.staff || []).find((x) => x.id === id) || { name: id }).name;
  const nOf = (n, word) => `${n} ${word}${n === 1 ? '' : 's'}`;
  const STATUS = { applied: 'Applied', waiting: 'Read, waiting to be applied', failed: 'Could not read', retry: 'To try again', skipped: 'Skipped' };
  function setupCard(p) {
    if (!sdoc || !p) return null;
    const P = window.planeSetup, plan = P && P.plan ? P.plan() : null, reqs = (plan && plan.requirements) || [];
    if (p.type === 'teacher') {
      const s = (sdoc.staff || []).find((x) => x.id === p.id); if (!s) return null;
      const own = (plan && (plan.staff || []).find((x) => x.id === s.id)) || {};
      const cuts = (own.reductions || []).map((r) => `${r.reason || r.name || 'Reduction'} ${r.periods}`).join(', ');
      const teaches = reqs.filter((r) => (r.teachers || []).includes(s.id));
      return {
        kind: 'Teacher' + (s.dept ? ' · ' + s.dept : ''), title: String(s.name || s.id),
        sub: `${s.load} of ${s.allowance} periods` + (s.over ? ' · over allowance' : '') + (s.provisional ? ' · provisional' : ''),
        rows: [['Department', s.dept || 'No department'], ['Load', nOf(s.load, 'period')], ['Allowance', nOf(s.allowance, 'period')], ['Reductions', cuts]],
        lines: teaches.slice(0, 8).map((r) => [r.subject, (r.classes || []).join(', '), nOf(r.periods || 0, 'period')].filter(Boolean).join(' · '))
          .concat(teaches.length > 8 ? [`and ${teaches.length - 8} more`] : []),
        link: { text: 'Show in the staff table', go: () => flashRow('plan-staff', s.id) },
      };
    }
    if (p.type === 'class') {
      const c = (sdoc.classes || []).find((x) => x.code === p.id); if (!c) return null;
      const cycle = sdoc.cycle_periods || 0;
      const who = (subject) => [...new Set(reqs.filter((r) => r.subject === subject && (r.classes || []).includes(c.code)).flatMap((r) => r.teachers || []))].map(staffName).join(', ');
      return {
        kind: 'Class' + (c.level ? ' · Level ' + c.level : ''), title: String(c.code),
        sub: `${nOf(c.periods, 'period')} a cycle` + (cycle ? ` of ${cycle}` : '') + (cycle && c.periods > cycle ? ` · ${c.periods - cycle} over` : ''),
        rows: Object.entries(c.by_subject || {}).map(([subject, n]) => [subject, [nOf(n, 'period'), who(subject) || 'no teacher yet'].join(' · ')])
          .concat(c.untaught ? [['No teacher yet', nOf(c.untaught, 'period')]] : []),
        link: { text: 'Show in the requirements table', go: async () => {
          const pl = await planNow(), r = ((pl && pl.requirements) || []).find((x) => (x.classes || []).includes(c.code));
          return r ? flashRow('plan-requirements', r.id) : false;
        } },
      };
    }
    if (p.type === 'room') {
      const r = ((sdoc.campus && sdoc.campus.rooms) || []).find((x) => x.id === p.id); if (!r) return null;
      return {
        kind: 'Room' + (r.kind ? ' · ' + r.kind : ''), title: String(r.name || r.id),
        sub: [r.block ? 'Block ' + r.block : 'No block', r.level ? 'Level ' + r.level : null, r.cap ? r.cap + ' seats' : null].filter(Boolean).join(' · '),
        rows: [['Block', r.block], ['Level', r.level], ['Capacity', r.cap == null ? '' : String(r.cap)], ['Kind', r.kind], ['Home of', r.home]],
        link: { text: 'Show in the rooms table', go: () => flashRow('locations-table', r.id) },
      };
    }
    if (p.type === 'tile') {
      const w = sdoc.week || {}, spd = w.slots_per_day || 0, id = p.id || {}, day = id.day, slot = id.slot;
      if (!Number.isInteger(day) || !Number.isInteger(slot)) return null;
      const labels = w.labels || [], label = labels[day * spd + slot] || `Day ${day + 1}, period ${slot + 1}`;
      const rest = (w.rests || []).includes(slot);
      const pins = (w.pinned || []).filter((x) => x.day === day && slot >= x.slot && slot < x.slot + (x.dur || 1)).map((x) => x.name || 'Pinned');
      const open = [...Array(spd).keys()].filter((k) => !(w.rests || []).includes(k));
      const edge = (w.edge_subjects || []).length && !rest && (slot === open[0] || slot === open[open.length - 1]);
      const notes = (sdoc.notes || []).filter((n) => n.day === day).map((n) => 'Note: ' + n.text);
      return {
        kind: 'Period', title: String(label), sub: rest ? 'A rest for everyone' : 'A lesson period',
        rows: [['Pinned here', pins.join(', ')], ['Edge subjects', edge ? w.edge_subjects.join(', ') : '']],
        lines: notes,
        link: { text: 'Show the rules in the table', go: () => flashRow('plan-rules', 'rules') },
      };
    }
    if (p.type === 'file') {
      const f = (sdoc.files || []).find((x) => x.id === p.id); if (!f) return null;
      return {
        kind: 'File' + (f.kind ? ' · ' + f.kind : ''), title: String(f.file || 'File'), sub: STATUS[f.status] || String(f.status || ''),
        rows: [['Read by', f.via === 'saved' ? 'A saved reader' : 'The Assistant'], ['Problem', f.error]],
        lines: f.summary && f.summary !== f.file ? [String(f.summary)] : [],
        link: { text: 'Show in the Assistant', go: () => (window.planeSetup ? window.planeSetup.showReading(f.id) : false) },
      };
    }
    return null;
  }

  /* ---- the journey on the campus (spec §3.2–3.3) ---- */
  const pause = (ms) => new Promise((r) => setTimeout(r, ms));
  const stages = createStages(scene, {
    doc: () => doc,
    look: () => look,
    reload: (force) => reload(!!force).catch(() => {}),
    async popLessons(seconds, live) {                         // lessons pop into rooms in four growing slices
      if (!doc) return;
      const all = Object.entries(D.occupancy(doc.lessons, t)), shown = {};
      for (let k = 1; k <= 4; k++) {
        all.slice(0, Math.ceil((all.length * k) / 4)).forEach(([room, l]) => { shown[room] = l; });
        if (live && !live()) return;                          // stopped: the next reload draws the truth
        scene.setOccupancy({ ...shown }, colourOf);
        await pause((seconds * 1000) / 4);
      }
      if (!live || live()) showPeriod();
    },
    unfinished() {                                            // Construction: back to the site for the handover
      if (!doc || look !== 'site' || !builtFinished) return;
      scene.build(doc, look, false); builtFinished = false; layoutKey = null; showPeriod();
    },
    clearForReplay() {
      if (doc && look === 'site' && builtFinished) { scene.build(doc, look, false); builtFinished = false; layoutKey = null; }
      scene.setOccupancy({}, colourOf); scene.setPins([]); scene.setWalkers([], deptColour); scene.fx.flatten(look === 'site');
    },
    setPins() { showPeriod(); },
  });

  stage.addEventListener('keydown', (e) => { if (e.key === 'Escape') ui.closeCard(); });
  active = true;
  try {
    await reload();
  } catch (e) {
    document.removeEventListener('plane:journey-state', onState); document.removeEventListener('plane:motion', onMotion); document.removeEventListener('plane:tab', onTab);
    document.removeEventListener('plane:plan', onData); document.removeEventListener('plane:readings', onData); if (ttPick) ttPick.removeEventListener('change', onPickTimetable);
    ui.dispose(); scene.dispose(); el.replaceChildren();                     // the stage and the controls: nothing left for a retry to stack on
    throw e;
  }
  active = false;

  const controller = {
    setLook(next) {
      if (next === look) return;
      look = next; layoutKey = null;
      if (mode !== 'build') { drawSetup(true); return; }      // the look switch redraws the tab's scene in its palette
      present(false); stages.stop();
      if (doc) { scene.build(doc, look, !!doc.checked_ok); builtFinished = !!doc.checked_ok; showPeriod(); if (marked) markRoom(marked); }
    },
    setMode,
    play(detail) { if (active && mode === 'build') stages.play(detail); else if (opts.hold) opts.hold(detail); },
    replay() { return active && mode === 'build' ? stages.replay() : Promise.resolve(); },
    setActive(on) {
      active = !!on; scene.setActive(active);
      if (!active) {
        present(false); ui.stop();
        if (stages.busy()) {                                   // hand the unfinished steps back to be played later
          const left = stages.stop();
          if (opts.hold) left.forEach((d) => opts.hold(d));
          reload(true).catch(() => {});
        }
      }
      if (active && stale) reload().catch(() => {});
      if (active && mode !== 'build' && sStale && !sBusy) refreshSetup().catch(() => {});
    },
    resize() { if (presenting && !el.closest('.expanded')) present(false); scene.resize(); ui.relayout(); },   // leaving full screen or a dock change
    reload(force) {
      present(false); switched();
      if (mode !== 'build') refreshSetup().catch(() => {});   // a new check: the setup tab's document too
      else sStale = true;
      return reload(force);
    },
    highlightEvent(id) {
      marked = id || null;
      if (mode !== 'build') return;                            // kept for Build
      if (!doc || !id) { scene.highlight(null); return; }
      for (const [room, list] of Object.entries(doc.lessons)) {
        const l = list.find((x) => x.event === id);
        if (l) {
          if (!(l.t0 <= t && t < l.t0 + (l.dur || 1))) { t = l.t0; showPeriod(); }
          scene.highlight(room); return;
        }
      }
      scene.highlight(null);
    },
    dispose() {
      present(false); ui.stop(); ui.dispose();
      document.removeEventListener('plane:journey-state', onState); document.removeEventListener('plane:motion', onMotion); document.removeEventListener('plane:tab', onTab);
      document.removeEventListener('plane:plan', onData); document.removeEventListener('plane:readings', onData); if (ttPick) ttPick.removeEventListener('change', onPickTimetable);
      clearTimeout(switching);
      scene.dispose(); stage.remove();
    },
  };
  return controller;
}
