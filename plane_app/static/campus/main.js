/* The campus (spec 2026-10-11-campus-looks-design.md §2): mount(el, opts) → controller. Loads
   /api/campus, draws it with scene.js, and shows the chosen period: lessons lit, free teachers walking,
   clashes pinned. campus-boot.js drives it (look, active, resize, reloads, grid selection). */
import { createScene } from 'campus/scene';
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
  let doc = null, look = opts.look, t = null, palette = {}, active = false, stale = false, ui = null, loads = null, allClashes = false, seq = 0, marked = null, layoutKey = null, builtFinished = false;
  stage.tabIndex = 0;
  stage.setAttribute('role', 'application');
  stage.setAttribute('aria-label', 'Campus view. Left and right arrows change the period; find a teacher or room below.');

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
    if (!doc || !ui) return;
    const c = jc || {}, num = (v) => (typeof v === 'number' ? v : null);
    ui.setCounters({
      lessons: num(c.lessons_total) != null ? [num(c.lessons_placed) || 0, c.lessons_total] : null,
      teachers: num(c.teachers_total) != null ? [num(c.teachers_deployed) || 0, c.teachers_total] : null,
      rooms: [Object.keys(D.occupancy(doc.lessons, t)).length, doc.rooms.length],
      clashes: num(c.clashes) != null ? c.clashes : (doc.clashes || []).length,
    }, motion() === 'off');
  }
  const onState = (e) => { jc = (e.detail && e.detail.counters) || null; counters(); };
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
    // a timetable change re-lights the same school; only a new layout (or look) is rebuilt
    const key = JSON.stringify([doc.buildings, doc.areas, doc.grounds, doc.checked_ok, doc.rooms.map((x) => x.home), look]);
    const sameLayout = key === layoutKey;
    layoutKey = key;
    if (!sameLayout) { scene.build(doc, look, !!doc.checked_ok); builtFinished = !!doc.checked_ok; }
    loads = null;
    if (doc.source === 'live') {
      fetch('/api/loads', { credentials: 'same-origin' }).then((x) => (x.ok ? x.json() : null)).then((j) => { if (n === seq) loads = (j && j.loads) || null; }).catch(() => {});
    }
    ui.setDoc(doc, palette);
    showPeriod();
    if (marked) markRoom(marked);                            // keep the grid's selection, and the period
  }

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
  });
  ui.setMotion(motion());
  scene.onPick((hit) => { if (hit.kind === 'room') ui.showRoom(hit.id); else ui.showTeacher(hit.id); });

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

  stage.addEventListener('keydown', (e) => { if (e.key === 'Escape') ui.hideCard(); });
  active = true;
  try {
    await reload();
  } catch (e) {
    document.removeEventListener('plane:journey-state', onState); document.removeEventListener('plane:motion', onMotion); document.removeEventListener('plane:tab', onTab);
    ui.dispose(); scene.dispose(); el.replaceChildren();                     // the stage and the controls: nothing left for a retry to stack on
    throw e;
  }
  active = false;

  const controller = {
    setLook(next) { if (next !== look) { present(false); stages.stop(); look = next; layoutKey = null; if (doc) { scene.build(doc, look, !!doc.checked_ok); builtFinished = !!doc.checked_ok; showPeriod(); if (marked) markRoom(marked); } } },
    play(detail) { if (active) stages.play(detail); },
    replay() { return active ? stages.replay() : Promise.resolve(); },
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
    },
    resize() { if (presenting && !el.closest('.expanded')) present(false); scene.resize(); ui.relayout(); },   // leaving full screen or a dock change
    reload(force) { present(false); return reload(force); },
    highlightEvent(id) {
      marked = id || null;
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
      scene.dispose(); stage.remove();
    },
  };
  return controller;
}
