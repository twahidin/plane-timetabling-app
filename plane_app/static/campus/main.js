/* The campus (spec 2026-10-11-campus-looks-design.md §2): mount(el, opts) → controller. Loads
   /api/campus, draws it with scene.js, and shows the chosen period: lessons lit, free teachers walking,
   clashes pinned. campus-boot.js drives it (look, active, resize, reloads, grid selection). */
import { createScene } from 'campus/scene';
import { colours } from 'campus/themes';
import * as D from 'campus/data';
import { createUI } from 'campus/ui';

export async function mount(el, opts) {
  const probe = document.createElement('canvas');
  const gl = probe.getContext('webgl2') || probe.getContext('webgl');
  if (!gl) throw new Error('no WebGL');
  const lose = gl.getExtension('WEBGL_lose_context'); if (lose) lose.loseContext();
  const stage = document.createElement('div'); stage.className = 'campus-stage';
  el.appendChild(stage);
  const scene = createScene(stage);
  let doc = null, look = opts.look, t = null, palette = {}, active = false, stale = false, ui = null, loads = null, allClashes = false, seq = 0, marked = null, layoutKey = null;
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
  }
  function markRoom(id) {                                   // the grid's lesson: its room, without moving the period
    if (!doc || !id) { scene.highlight(null); return; }
    for (const [room, list] of Object.entries(doc.lessons)) if (list.some((x) => x.event === id)) { scene.highlight(room); return; }
    scene.highlight(null);
  }

  async function reload() {
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
    if (!sameLayout) scene.build(doc, look, !!doc.checked_ok);
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
  });
  scene.onPick((hit) => { if (hit.kind === 'room') ui.showRoom(hit.id); else ui.showTeacher(hit.id); });

  stage.addEventListener('keydown', (e) => { if (e.key === 'Escape') ui.hideCard(); });
  active = true;
  try {
    await reload();
  } catch (e) {
    scene.dispose(); el.replaceChildren();                     // the stage and the controls: nothing left for a retry to stack on
    throw e;
  }
  active = false;

  const controller = {
    setLook(next) { if (next !== look) { look = next; layoutKey = null; if (doc) { scene.build(doc, look, !!doc.checked_ok); showPeriod(); if (marked) markRoom(marked); } } },
    setActive(on) {
      active = !!on; scene.setActive(active);
      if (!active) ui.stop();
      if (active && stale) reload().catch(() => {});
    },
    resize() { scene.resize(); ui.relayout(); },
    reload,
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
    dispose() { ui.stop(); scene.dispose(); stage.remove(); },
  };
  return controller;
}
