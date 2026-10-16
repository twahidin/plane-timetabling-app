/* The setup tabs' five scenes (spec 2026-10-13-setup-campus §2), ported from the approved mockup
   (docs/design/2026-10-13-setup-campus/mockup.html, SCENES) and drawn from GET /api/campus/setup.

   Two halves:
   - Pure layout functions (no Three.js, Node-testable): layoutTeachers, layoutTowers, layoutWeek.
   - buildScene(mode, doc, kit, school) -> {legend: [[name, colour]], counters: [[value, name, bad]], note}
     draws one scene into kit.group, where
       mode   is project | manpower | locations | requirements | constraints;
       doc    is the /api/campus/setup document;
       kit    is scene.overlay().kit (THREE, T, rbox, mat, label, tween, decor, pickable, instanced,
              figureParts, grounds, group);
       school is {showSchool, colourRooms, fx} from the scene object (the scene's school as the setting).
     Every builder takes kit.THREE: this module imports no `three`, so the Node tests can load it.

   Motion follows window.planeMotion through school.fx.motion(): Full plays every tween; Reduced halves
   them (fx.speed(0.5)) and drops the fly-ins; Off draws the end state at once. Text from data is only
   ever set through kit.label (textContent). Nothing here writes: the scenes only read the document.

   A redraw of the scene already showing (a reload, the look switch) plays no intro: it draws the end state,
   and Project flies in only the files it has not shown before. Call resetScenes() when the campus leaves the
   setup tabs (or call buildScene with any other mode, e.g. 'build'), so the next setup tab plays its intro.
   Departments (and subjects) take Build's colours from doc.legend ("d:<dept>" / "s:<subject>"). */

const NO_DEPARTMENT = 'No department';               // the document's name for staff with no department
const GOLDEN = Math.PI * (3 - Math.sqrt(5));
const TOWERS_ONE_ROW = 40, TOWERS_MAX = 120, SHEETS_MAX = 8, NOTE_LABELS = 3, LEGEND_MAX = 8;
const RED = '#c0262f', GOOD = '#22c88a', WARN = '#ffb020', AMBER = '#ffc85a', WOOD = '#d9a35f', FLAG = '#ff8c7a', POLE = '#a8754b';

const num = (v) => { const n = typeof v === 'number' ? v : typeof v === 'string' && v.trim() ? Number(v) : NaN; return Number.isFinite(n) ? n : 0; };
const int = (v) => Math.max(0, Math.round(num(v)));
const list = (v) => (Array.isArray(v) ? v : []);
const obj = (v) => (v && typeof v === 'object' && !Array.isArray(v) ? v : {});
const r2 = (v) => Math.round(v * 100) / 100;
const clamp = (v, a, b) => Math.min(b, Math.max(a, v));
/* Manpower's figures, at a real school's scale (dozens of teachers, many over their allowance): FIG is the figure's
   scale per unit of height; the tallest (height 2.26) is 1.05 wide in Toy town's hair and 1.12 in Construction's
   hard hat. SPACING is the sunflower spacing in a cluster, its nearest neighbours about 1.54 × SPACING apart, never
   closer than SPACING_MIN however crowded the ring (1.12 apart: even the tallest do not touch); GAP the least
   distance between two figures moved off a footprint. An over-allowance ring's outer radius stays under half that. */
export const FIG = 0.4, SPACING = 0.74, SPACING_MIN = 0.73, GAP = 1.13;
const byName = (a, b) => String(a).localeCompare(String(b), undefined, { numeric: true, sensitivity: 'base' });
const groundsOf = (g) => ({ w: num(obj(g).w) || 48, d: num(obj(g).d) || 34 });
const deptName = (d) => (typeof d === 'string' && d.trim() ? d : NO_DEPARTMENT);
/* A legend of at most LEGEND_MAX keys and "and N more" (no colour), then `tail` (keys that must stay, e.g.
   the red line's): a long one would cover the scene at phone width. */
function capLegend(items, tail = []) {
  const room = LEGEND_MAX - tail.length;
  if (items.length + tail.length <= LEGEND_MAX + 1) return items.concat(tail);
  return items.slice(0, room).concat([[`and ${items.length - room} more`, null]], tail);
}

/* ---------------------------------------------------------------- pure layouts ---- */

/* A figure's height factor: 1.3 + 0.6 × min(load/allowance, 1.6); no allowance and some load is the most. */
export function figureHeight(load, allowance) {
  const l = num(load), a = num(allowance);
  const ratio = a > 0 ? Math.max(0, l / a) : l > 0 ? 1.6 : 0;
  return r2(1.3 + 0.6 * Math.min(ratio, 1.6));
}

/* Departments in a ring of clusters around the courtyard (an ellipse about the centre, each department's arc
   as wide as its cluster); a cluster is a filled circle (sunflower order, tallest in the middle) whose radius
   grows with the square root of its size. Staff whose department is not listed get a cluster of their own.
   footprints (optional): [{x, z, w, d}], the campus's buildings and areas. With them every cluster stands on
   free ground: the free spot nearest its place on the ring where the whole circle clears the footprints
   (+0.5), the grounds and the clusters already placed (biggest first); a figure that still lands on a
   footprint steps to the nearest free spot. */
export function layoutTeachers(staff, departments, grounds, footprints) {
  const g = groundsOf(grounds);
  const groups = new Map(list(departments).map((d) => [deptName(obj(d).name), []]));
  for (const s of list(staff)) {
    if (!s || typeof s !== 'object' || s.id === undefined || s.id === null) continue;
    const d = deptName(s.dept);
    if (!groups.has(d)) groups.set(d, []);
    groups.get(d).push(s);
  }
  const clusters = [...groups].filter(([, l]) => l.length);
  if (!clusters.length) return [];
  const a0 = g.w * 0.3, b0 = g.d * 0.28;
  const perimeter = Math.PI * (3 * (a0 + b0) - Math.sqrt((3 * a0 + b0) * (a0 + 3 * b0)));
  const widths = clusters.map(([, l]) => 2 * Math.sqrt(l.length) + 1.5);       // in units of the spacing
  const total = widths.reduce((s, w) => s + w, 0);
  const sp = clusters.length === 1 ? SPACING : clamp(perimeter / total, SPACING_MIN, SPACING);   // a crowded ring packs closer
  const rMax = Math.max(...clusters.map(([, l]) => sp * Math.sqrt(l.length)));
  const A = Math.max(0, Math.min(a0, g.w / 2 - 2 - rMax)), B = Math.max(0, Math.min(b0, g.d / 2 - 2 - rMax));
  let cum = 0;
  const plan = clusters.map(([name, people], i) => {
    const frac = (cum + widths[i] / 2) / total; cum += widths[i];
    const ang = Math.PI / 2 + 2 * Math.PI * frac;                                  // the first department west of the field
    return { name, people, r: sp * Math.sqrt(people.length) + 0.3,
             x: clusters.length === 1 ? 0 : A * Math.cos(ang), z: clusters.length === 1 ? 0 : B * Math.sin(ang) };
  });
  const fps = list(footprints).map(obj).map((f) => ({ x: num(f.x), z: num(f.z), hw: num(f.w) / 2 + 0.55, hd: num(f.d) / 2 + 0.55 }))
    .filter((f) => f.hw > 0.55 && f.hd > 0.55);
  const hx = g.w / 2 - 1, hz = g.d / 2 - 1;
  const clear = (x, z) => {                                // distance to the nearest footprint (0 inside one)
    let m = Infinity;
    for (const f of fps) m = Math.min(m, Math.hypot(Math.max(Math.abs(x - f.x) - f.hw, 0), Math.max(Math.abs(z - f.z) - f.hd, 0)));
    return m;
  };
  if (fps.length) {
    const step = Math.max(1, Math.max(g.w, g.d) / 80), placed = [];
    for (const c of [...plan].sort((p, q) => q.people.length - p.people.length)) {
      let best = null, loose = null;
      for (let x = -hx; x <= hx; x += step) for (let z = -hz; z <= hz; z += step) {
        const d = Math.hypot(x - c.x, z - c.z), room = clear(x, z);
        if (room <= 0 || placed.some((p) => Math.hypot(x - p.x, z - p.z) < p.r)) continue;
        if (!loose || d < loose.d) loose = { x, z, d };      // a free spot, for a cluster too big for any gap
        if (room < c.r || Math.abs(x) + c.r > hx || Math.abs(z) + c.r > hz) continue;
        if (placed.some((p) => Math.hypot(x - p.x, z - p.z) < p.r + c.r + 1)) continue;
        if (!best || d < best.d) best = { x, z, d };
      }
      const at = best || loose;
      if (at) { c.x = at.x; c.z = at.z; }
      placed.push(c);
    }
  }
  const out = [], taken = [];
  const ok = (x, z) => Math.abs(x) <= hx && Math.abs(z) <= hz && (!fps.length || clear(x, z) > 0)
    && taken.every((t) => Math.hypot(x - t[0], z - t[1]) >= GAP);
  plan.forEach(({ name, people, x: cx, z: cz }) => {
    const ranked = people.map((s) => ({ s, h: figureHeight(s.load, s.allowance) }))
      .sort((p, q) => q.h - p.h || byName(p.s.id, q.s.id));
    ranked.forEach(({ s, h }, k) => {
      const r = sp * Math.sqrt(k + 0.5), a = k * GOLDEN;
      let x = clamp(cx + r * Math.cos(a), -hx, hx), z = clamp(cz + r * Math.sin(a), -hz, hz);
      if (fps.length && !ok(x, z)) {                       // still on a footprint (or a neighbour): the nearest free spot
        search: for (let rad = 0.5; rad < g.w + g.d; rad += 0.5) {
          const n = Math.ceil((2 * Math.PI * rad) / 0.5);
          for (let i = 0; i < n; i++) {
            const nx = x + rad * Math.cos((2 * Math.PI * i) / n), nz = z + rad * Math.sin((2 * Math.PI * i) / n);
            if (ok(nx, nz)) { x = nx; z = nz; break search; }
          }
        }
      }
      taken.push([x, z]);
      out.push({ id: s.id, x: r2(x), z: r2(z), height: h, over: s.over === true, provisional: s.provisional === true, dept: name });
    });
  });
  return out;
}

/* Each class a tower of blocks, one per subject (subjects by name); a subject's periods with no teacher
   (`untaught_by_subject`) are that block's `untaught`, drawn see-through at the top of the block. A tower is drawn to at most
   max(2 × the cycle, 80) periods (`capped`, `drawn`); `over` is the periods past the cycle. One row when there
   are 40 classes or fewer; past that one row per level (natural order, no level last). A row that would hold
   more than 40 towers, or stand them closer than 1 apart, goes on in more rows of the same level; `label` is
   true on a level's first row (past 40 classes). */
export function layoutTowers(classes, cyclePeriods, grounds) {
  const g = groundsOf(grounds), cycle = int(cyclePeriods), cap = Math.max(2 * cycle, 80);
  const all = list(classes).filter((c) => c && typeof c === 'object' && c.code !== undefined && c.code !== null).slice(0, TOWERS_MAX);
  const towers = all.map((c) => {
    const by = obj(c.by_subject), no = obj(c.untaught_by_subject);
    let blocks = Object.keys(by).filter((s) => int(by[s]) > 0).sort(byName).map((s) => ({ subject: s, periods: int(by[s]), untaught: int(no[s]) }));
    if (!blocks.length && int(c.periods) > 0) blocks.push({ subject: 'Other', periods: int(c.periods), untaught: int(no.Other) });
    const periods = blocks.reduce((s, b) => s + b.periods, 0);
    let room = cap;
    blocks = blocks.map((b) => { const p = Math.min(b.periods, room); room -= p; return Object.assign(b, { periods: p, untaught: Math.min(b.untaught, p) }); }).filter((b) => b.periods > 0);
    const drawn = Math.min(periods, cap);
    return { code: String(c.code), level: c.level === undefined || c.level === null || c.level === '' ? null : String(c.level),
             periods, drawn, capped: periods > cap, blocks, over: cycle > 0 ? Math.max(0, periods - cycle) : 0, x: 0, z: 0, row: 0 };
  });
  const byLevel = towers.length > TOWERS_ONE_ROW, groupsOf = new Map();
  for (const t of towers) { const k = byLevel ? t.level : null; if (!groupsOf.has(k)) groupsOf.set(k, []); groupsOf.get(k).push(t); }
  const levels = [...groupsOf.keys()].sort((a, b) => (a === null) - (b === null) || (a === null ? 0 : byName(a, b)));
  const perRow = Math.max(1, Math.min(TOWERS_ONE_ROW, Math.floor(Math.max(1, g.w - 12))));
  const spec = [];
  for (const level of levels) {
    const members = groupsOf.get(level), parts = Math.ceil(members.length / perRow), size = Math.ceil(members.length / parts);
    for (let j = 0; j < parts; j++) spec.push({ level, members: members.slice(j * size, (j + 1) * size), label: byLevel && j === 0 });
  }
  const pz = spec.length > 1 ? Math.min(9, (g.d - 10) / spec.length) : 0;
  const rows = spec.map(({ level, members, label }, r) => {
    const z = r2((r - (spec.length - 1) / 2) * pz);
    const px = Math.min(6, Math.max(1, g.w - 12) / Math.max(1, members.length));
    members.forEach((t, i) => { t.x = r2((i - (members.length - 1) / 2) * px); t.z = z; t.row = r; });
    return { level: byLevel ? level : null, z, pitch: r2(px), depth: r2(pz), label };
  });
  return { towers, rows };
}

/* The cycle as rows of period tiles, one row a day; mandatory rests are gaps; pinned events mark their
   tiles (and plant a flag on the first); edge subjects colour each day's first and last periods.
   With the week's positions (spec 2026-10-15-week-shape §1) each row is that day at its own length: tiles
   for its periods, gaps for its own breaks, nothing for a closed position; a slot is still the position's
   index, so the days line up on the left. */
export function layoutWeek(week, grounds) {
  const g = groundsOf(grounds), w = obj(week);
  const days = int(w.days), spd = int(w.slots_per_day);
  const out = { tiles: [], gaps: [], flags: [], pitch: { x: 0, z: 0 } };
  if (!days || !spd) return out;
  const ps = list(w.positions).length ? list(w.positions) : null;
  const kindAt = (day, slot) => (ps ? obj(ps[day * spd + slot]).kind : null);
  const rests = new Set(ps ? [] : list(w.rests).map(int).filter((s) => s < spd));
  const px = Math.min(6, Math.max(1, g.w - 14) / spd), pz = Math.min(8, Math.max(1, g.d - 8) / days);
  out.pitch = { x: r2(px), z: r2(pz) };
  const x = (slot) => r2(2 + (slot - (spd - 1) / 2) * px), z = (day) => r2((day - (days - 1) / 2) * pz);
  const pinned = new Set();
  for (const p of list(w.pinned)) {
    const day = int(obj(p).day), slot = int(obj(p).slot), dur = Math.max(1, int(obj(p).dur));
    if (day >= days || slot >= spd) continue;
    for (let k = 0; k < dur && slot + k < spd; k++) pinned.add(day * spd + slot + k);
    out.flags.push({ name: String(obj(p).name || 'Pinned'), x: x(slot), z: z(day), day, slot });
  }
  const openOf = (day) => [...Array(spd).keys()].filter((s) => (ps ? kindAt(day, s) === 'period' : !rests.has(s)));
  const edgy = list(w.edge_subjects).length > 0;
  for (let day = 0; day < days; day++) {
    const open = openOf(day), edges = edgy && open.length ? new Set([open[0], open[open.length - 1]]) : new Set();
    for (let slot = 0; slot < spd; slot++) {
      const k = kindAt(day, slot);
      if (ps && k !== 'period' && k !== 'break') continue;                 // closed: nothing drawn
      if (ps ? k === 'break' : rests.has(slot)) { out.gaps.push({ day, slot, x: x(slot), z: z(day) }); continue; }
      const kind = pinned.has(day * spd + slot) ? 'pinned' : edges.has(slot) ? 'edge' : 'lesson';
      out.tiles.push({ day, slot, x: x(slot), z: z(day), kind });
    }
  }
  return out;
}

/* ---------------------------------------------------------------- the scenes ---- */

const ease = {
  back: (k) => { const c = 1.7, c3 = c + 1; return 1 + c3 * Math.pow(k - 1, 3) + c * Math.pow(k - 1, 2); },
  cubic: (k) => 1 - Math.pow(1 - k, 3),
  inout: (k) => (k < 0.5 ? 4 * k * k * k : 1 - Math.pow(-2 * k + 2, 3) / 2),
};

/* name → colour from the theme's dept palette. A name with a key in Build's legend (prefix + name, e.g.
   "d:Maths") takes Build's colour (themes.colours: the key's place in the legend); the rest follow in name
   order after the legend's keys, so they never take a colour Build already gives. */
function palette(names, T, legend, prefix) {
  const out = new Map(), dept = list(T.dept).length ? T.dept : ['#8b7cf6'];
  const keys = Object.keys(obj(legend)), rest = [];
  for (const n of [...new Set(names)].sort(byName)) {
    const k = keys.indexOf(prefix + n);
    if (k >= 0) out.set(n, dept[k % dept.length]); else rest.push(n);
  }
  rest.forEach((n, i) => out.set(n, dept[(keys.length + i) % dept.length]));
  return new Map([...out].sort((a, b) => byName(a[0], b[0])));
}

function motionOf(school) {
  if (school.fx && typeof school.fx.motion === 'function') return school.fx.motion();
  return typeof window !== 'undefined' && window.planeMotion ? window.planeMotion.level() : 'full';
}

/* What every builder shares: the kit, the school, the motion, and helpers that respect it. */
function context(kit, school, motion, fresh) {
  const THREE = kit.THREE;
  const m4 = new THREE.Matrix4(), p = new THREE.Vector3(), q = new THREE.Quaternion(), s = new THREE.Vector3(), e = new THREE.Euler();
  const c = {
    kit, school, motion, fresh, THREE, T: kit.T, g: groundsOf(kit.grounds), site: !!kit.T.site,
    fly: motion === 'full',                                  // fly-ins: Full only
    still: motion === 'off' || !fresh,                       // a redraw of the same scene: its end state, no intro
    show(mode) { if (school.showSchool) school.showSchool(mode); },
    /* a tween, or its end state at once when motion is off or the scene is only redrawn */
    anim(o, always = false) {
      if (motion === 'off' || (c.still && !always)) { o.update(1); if (o.done) o.done(); return Promise.resolve(); }
      return kit.tween(o);
    },
    place(mesh, i, x, y, z, sx, sy, sz, ry = 0) {
      e.set(0, ry, 0); q.setFromEuler(e);
      mesh.setMatrixAt(i, m4.compose(p.set(x, y, z), q, s.set(Math.max(0.001, sx), Math.max(0.001, sy), Math.max(0.001, sz))));
    },
    /* n instances in one tween: each(i, k) with k eased per instance, starting after delayOf(i) */
    stagger(meshes, n, each, { delay = 0, delayOf = () => 0, dur = 0.6, curve = ease.back } = {}) {
      const flush = () => meshes.forEach((m) => { m.instanceMatrix.needsUpdate = true; });
      let span = 0; for (let i = 0; i < n; i++) span = Math.max(span, delayOf(i));
      const run = (t) => { for (let i = 0; i < n; i++) each(i, curve(clamp((t - delayOf(i)) / dur, 0, 1))); flush(); };
      if (!c.still) run(0);
      return c.anim({ delay, dur: span + dur, ease: (k) => k, update: (k) => run(k * (span + dur)) });
    },
    colours(mesh, cols) {
      const col = new THREE.Color();
      cols.forEach((x, i) => mesh.setColorAt(i, col.set(x)));
      if (mesh.instanceColor) mesh.instanceColor.needsUpdate = true;
    },
    see(material, opacity) { Object.assign(material, { transparent: true, opacity, depthWrite: false }); return material; },
  };
  return c;
}

/* ---- 02 Project: the faint school to come, and each file a sheet on the intake pad ---- */
function projectScene(doc, c) {
  const { kit, THREE, g } = c;
  c.show('ghost');
  const files = list(doc.files).filter((f) => f && typeof f === 'object');
  // a reading set aside (skipped) is neither read nor to try again: a plain chip, no mark
  const stateOf = (f) => (f.status === 'failed' ? 'bad' : f.status === 'retry' ? 'warn' : f.status === 'skipped' ? 'skip' : 'good');
  const COL = { good: GOOD, warn: WARN, bad: RED, skip: '#9aa6b2' }, MARK = { good: '✓', warn: '⚠', bad: '×' };
  const pad = new THREE.Vector3(-g.w / 2 + 4.5, 0.3, -g.d / 2 + 4);   // scene.js's intake pad, top face
  if (!list(obj(doc.campus).buildings).length) {          // no school yet: the mockup's faint outline of one to come
    const ghost = c.see(kit.mat('#ffffff'), 0.28), sx = g.w / 70, sz = g.d / 52;
    for (const [x, z, w, d] of [[-2, -10, 18, 7], [14, -6, 9, 14], [8, 12, 12, 7]]) {
      const b = kit.rbox(w * sx, 5.2, d * sz, ghost, 0.25); b.position.set(x * sx, 2.9, z * sz); b.castShadow = false;
      b.userData.outline = true; kit.decor(b); kit.group.add(b);
    }
  }
  const shown = files.slice(-SHEETS_MAX);                             // the latest
  const step = Math.min(0.6, 3 / Math.max(1, shown.length));
  shown.forEach((f, n) => {
    const st = stateOf(f);
    const sheet = new THREE.Group(); kit.group.add(sheet);
    sheet.add(kit.rbox(2.4, 0.12, 3.1, kit.mat('#ffffff'), 0.05));
    const strip = kit.rbox(2.42, 0.14, 0.7, kit.mat(COL[st]), 0.05); strip.position.z = -1.2; sheet.add(strip);
    kit.pickable(sheet, { type: 'file', id: f.id });
    const end = pad.clone().add(new THREE.Vector3(0, 0.1 + n * 0.16, 0)), turn = n * 0.25;
    const name = String(f.file || 'File');
    const chip = () => kit.label(st === 'skip' ? `${name} · skipped` : `${MARK[st]} ${name}`, 'campus-chip' + (st === 'good' ? '' : ' ' + st),
      [pad.x + 6, 1.8 + n * 1.9, pad.z + 2]);
    if (c.fly && !memory.flown.has(f.id)) {                // a new file flies in, even on a redraw
      const a = new THREE.Vector3(-g.w / 2 - 14, 26, g.d / 2), mid = new THREE.Vector3(-g.w / 2 - 6, 16, 0), tmp = new THREE.Vector3();
      sheet.position.copy(a);
      c.anim({ delay: 0.3 + n * step, dur: 1.5, ease: 'inout', done: chip, update: (k) => {
        sheet.position.copy(a).lerp(mid, k).lerp(tmp.copy(mid).lerp(end, k), k);
        sheet.rotation.set((1 - k) * 1.6, (1 - k) * 2.4 + turn, (1 - k) * 0.6);
      } }, true);
    } else {
      sheet.position.copy(end); sheet.rotation.set(0, turn, 0); chip();
    }
  });
  for (const f of files) memory.flown.add(f.id);          // every file given so far: shown, never flown in again
  const read = files.filter((f) => stateOf(f) === 'good').length, retry = files.filter((f) => stateOf(f) === 'warn').length;
  return {
    legend: [['Read', GOOD], ['To try again', WARN], ['Could not read', RED]],
    counters: [[files.length, 'Files given'], [read, 'Read'], [retry, 'To try again', retry > 0]],
    note: !files.length ? "Give the Assistant your files (staff lists, allocations, last year's timetable) and they land on the intake pad."
      : (files.length > SHEETS_MAX ? `The latest ${SHEETS_MAX} of ${files.length} files are on the intake pad` : 'Files land on the intake pad')
        + '; the outline shows the school still to be built.',
  };
}

/* ---- 03 Manpower: each teacher a figure with their department, taller with more periods ---- */
function manpowerScene(doc, c) {
  const { kit, THREE } = c;
  c.show('full');
  const staff = list(doc.staff).filter((s) => s && typeof s === 'object' && s.id !== undefined && s.id !== null);
  const campus = obj(doc.campus), footprints = list(campus.buildings).concat(list(campus.areas));
  const people = layoutTeachers(staff, doc.departments, c.g, footprints);
  const byId = new Map(staff.map((s) => [s.id, s]));
  const cols = palette(list(doc.departments).map((d) => deptName(obj(d).name)).concat(people.map((p) => p.dept)), c.T, doc.legend, 'd:');
  const groups = new Map();                                // one instanced set per department, solid or provisional
  people.forEach((p) => { const k = p.dept + (p.provisional ? '\u0000p' : ''); if (!groups.has(k)) groups.set(k, []); groups.get(k).push(p); });
  const order = [...cols.keys()];
  [...groups].forEach(([, ps]) => {
    const parts = kit.figureParts(cols.get(ps[0].dept));
    const meshes = ['body', 'head', 'hair'].map((k) => {
      if (ps[0].provisional) c.see(parts[k].material, 0.4);
      const m = kit.instanced(parts[k].geometry, parts[k].material, ps.length);
      kit.pickable(m, ps.map((p) => ({ type: 'teacher', id: p.id })));
      return m;
    });
    const di = order.indexOf(ps[0].dept);
    c.stagger(meshes, ps.length, (i, k) => {
      const p = ps[i], sc = p.height * FIG * k;
      meshes.forEach((m) => c.place(m, i, p.x, 0, p.z, sc, sc, sc, Math.PI / 4));   // facing the camera's corner
    }, { delay: 0.2 + Math.min(di, 8) * 0.25, delayOf: (i) => i * Math.min(0.05, 1.2 / ps.length) });
  });
  const overs = people.filter((p) => p.over);
  if (overs.length) {                                      // a red ring at the feet of each teacher over their allowance
    const ring = kit.instanced(new THREE.TorusGeometry(1, 0.12, 8, 28).rotateX(Math.PI / 2), kit.mat(RED, { emissive: RED, emissiveIntensity: 0.4 }), overs.length);
    kit.pickable(ring, overs.map((p) => ({ type: 'teacher', id: p.id })));
    c.stagger([ring], overs.length, (i, k) => { const p = overs[i], r = (0.42 * p.height * FIG + 0.07) * k; c.place(ring, i, p.x, 0.14, p.z, r, r, r); },
      { delay: 0.8, delayOf: (i) => i * Math.min(0.05, 1 / overs.length) });
  }
  const top = (p) => 2.5 * p.height * FIG;
  let worst = null;
  for (const p of overs) { const s = byId.get(p.id), by = num(s.load) - num(s.allowance); if (!worst || by > worst.by) worst = { p, s, by }; }
  for (const name of order) {                              // a label over each department
    const ps = people.filter((p) => p.dept === name); if (!ps.length) continue;
    const x = ps.reduce((s, p) => s + p.x, 0) / ps.length, z = ps.reduce((s, p) => s + p.z, 0) / ps.length;
    const crowded = worst && worst.p.dept === name;        // the worst teacher's label below: raised, its pill wholly above its point
    const o = kit.label(name, 'blk in soft', [x, Math.max(...ps.map(top)) + 0.9 + (crowded ? 1.3 : 0), z]);
    if (crowded && o && o.center) o.center.set(0.5, 1.6);
  }
  if (worst) kit.label(`${String(worst.s.name || worst.s.id)} · ${num(worst.s.load)} of ${num(worst.s.allowance)}`, 'blk in bad',
    [worst.p.x, top(worst.p) + 0.4, worst.p.z]);
  const provisional = people.some((p) => p.provisional);
  return {
    legend: capLegend(order.filter((n) => people.some((p) => p.dept === n)).map((n) => [n, cols.get(n)])),
    counters: [[staff.length, 'Teachers'], [order.filter((n) => n !== NO_DEPARTMENT).length, 'Departments'], [overs.length, 'Over allowance', overs.length > 0]],
    note: !staff.length ? 'Give the Assistant your staff list and the teachers will gather here.'
      : 'Departments gather in the courtyard; taller figures carry more periods and a red ring marks someone over their allowance.'
        + (provisional ? ' See-through figures are provisional.' : ''),
  };
}

/* ---- 04 Locations: the school as the plan has it, each window coloured by its room's kind ---- */
const GROUNDS = { education: ['field', null], health: ['garden', 'lawn'], business: ['plaza', 'paving'], sports: ['track', 'track'] };
function roomKind(r) {
  const text = `${obj(r).kind || ''} ${obj(r).name || ''}`.toLowerCase();
  if (/\b(computer|ict|it)\b|\bcomp(uter)?\s*lab/.test(text)) return 'computer';
  if (/\blab(oratory)?s?\b|\bscience\b/.test(text)) return 'lab';
  if (/\b(hall|field|gym|gymnasium|auditorium|court|pitch|track|playground)\b/.test(text)) return 'hall';
  if (obj(r).home) return 'home';
  return 'classroom';
}
function locationsScene(doc, c) {
  const { kit, T, school } = c;
  c.show('full');
  // halls share their chip with the courtyard's ground: the field, or the timetable type's garden, plaza or track
  const ground = GROUNDS[obj(doc.campus).ground] ? obj(doc.campus).ground : GROUNDS[doc.ground] ? doc.ground : 'education';
  const [outside, role] = GROUNDS[ground], toy = obj(obj(kit.groundColours).toy);
  const K = { classroom: T.winOff, lab: list(T.dept)[3] || '#ff6b9a', computer: list(T.dept)[0] || '#8b7cf6',
              hall: (role && toy[role]) || T.fieldFinal || T.field, home: AMBER };
  if (school.colourRooms) school.colourRooms((r) => K[roomKind(r)]);
  if (!c.still && school.fx && school.fx.riseAll) school.fx.riseAll(2);   // on arrival only: the school rises (halved when Reduced)
  const campus = obj(doc.campus), rooms = list(campus.rooms).filter((r) => r && typeof r === 'object');
  const others = list(campus.buildings).filter((b) => obj(b).kind === 'other');
  const n = others.reduce((s, b) => s + list(b.levels).reduce((t, l) => t + list(l).length, 0), 0);
  if (others.length && n) {                                // over the school's own "Other rooms" label, with the count
    const b = others[0], o = kit.label(`Other rooms · ${n}`, 'blk in', [num(b.x), list(b.levels).length * 3.2 + 2, num(b.z)]);
    if (o) o.renderOrder = 1;
  }
  const blocks = new Set(rooms.filter((r) => r.block).map((r) => String(r.block))).size;
  return {
    legend: [['Classroom', K.classroom], ['Lab', K.lab], ['Computer room', K.computer], [`Hall / ${outside}`, K.hall], ['Home room', K.home]],
    counters: [[rooms.length, 'Rooms'], [blocks, 'Blocks'], [n, 'With no block', n > 0]],
    note: !rooms.length ? 'Give the Assistant your room list and the school will be laid out here.'
      : "Each window is a room, coloured by its kind; amber marks a class's home room." + (n ? ` ${n === 1 ? 'One room with no block waits' : `${n} rooms with no block wait`} in Other rooms.` : ''),
  };
}

/* ---- 05 Requirements: each class a tower of its periods, a red line where the cycle runs out ---- */
function requirementsScene(doc, c) {
  const { kit, THREE, g } = c;
  c.show('none');
  const cycle = int(doc.cycle_periods), hidden = int(doc.classes_hidden);
  const { towers, rows } = layoutTowers(doc.classes, cycle, g);
  const subjects = palette(towers.flatMap((t) => t.blocks.map((b) => b.subject)), c.T, doc.legend, 's:');
  const tallest = Math.max(cycle, ...towers.map((t) => t.drawn), 1);
  const s = Math.min(0.55, 22 / tallest);                 // one period's height
  const pitchZ = rows.length > 1 ? Math.abs(rows[1].z - rows[0].z) : 99;
  const f = Math.min(2.6, ...rows.map((r) => r.pitch * 0.72), pitchZ * 0.72);   // a tower's footprint
  const items = { solid: [], hollow: [], over: [] };     // one block a period
  towers.forEach((t, ti) => {
    let h = 0;
    for (const b of t.blocks) for (let k = 0; k < b.periods; k++, h++) {
      const kind = cycle > 0 && h >= cycle ? 'over' : k >= b.periods - b.untaught ? 'hollow' : 'solid';
      items[kind].push({ t, ti, h, colour: subjects.get(b.subject) });
    }
  });
  const crate = c.site ? 0.68 : 0.92;                     // Construction: crates on pallets
  const geo = kit.rbox(f, s * crate, f, null, Math.min(0.12, s * 0.3)).geometry;
  const meshes = [];
  const add = (key, material, geometry, per, offset) => {
    const its = items[key]; if (!its.length) return;
    const m = kit.instanced(geometry, material, its.length * per);
    kit.pickable(m, its.flatMap((it) => Array(per).fill({ type: 'class', id: it.t.code })));
    if (key !== 'over' && per === 1) c.colours(m, its.map((it) => it.colour));
    meshes.push({ m, its, per, offset });
  };
  add('solid', kit.mat('#ffffff'), geo, 1, () => [0, 0]);
  add('hollow', c.see(kit.mat('#ffffff'), 0.25), geo, 1, () => [0, 0]);
  add('over', kit.mat(RED, { emissive: RED, emissiveIntensity: 0.35 }), geo, 1, () => [0, 0]);
  if (c.site) {                                            // three slats under every crate
    const slat = kit.rbox(f, s * 0.2, f * 0.18, null, 0.02).geometry;
    items.slats = items.solid.concat(items.hollow, items.over);
    add('slats', kit.mat(WOOD), slat, 3, (j) => [(j - 1) * f * 0.4, 0]);
  }
  const towerStep = Math.min(0.12, 2.4 / Math.max(1, towers.length));
  const yOf = (it, slat) => (slat ? it.h * s + s * 0.1 : it.h * s + (c.site ? s * 0.2 + s * crate / 2 : s / 2));
  for (const { m, its, per, offset } of meshes) {
    const slat = per === 3;
    c.stagger([m], its.length * per, (i, k) => {
      const it = its[Math.floor(i / per)], [dz] = offset(i % per), y = yOf(it, slat);
      if (c.fly) c.place(m, i, it.t.x, y + 8 * (1 - k), it.t.z + dz, 1, 1, 1);   // blocks drop in
      else c.place(m, i, it.t.x, y * k, it.t.z + dz, 1, k, 1);                    // Reduced: they grow in place
    }, { delay: 0.2, dur: 0.35, curve: ease.cubic, delayOf: (i) => { const it = its[Math.floor(i / per)]; return it.ti * towerStep + Math.min(it.h * 0.012, 0.6); } });
  }
  if (cycle > 0 && towers.length) {                        // the red line where the cycle runs out, along each row
    const lineM = kit.mat(RED, { emissive: RED, emissiveIntensity: 0.3 });
    rows.forEach((r, ri) => {
      const xs = towers.filter((t) => t.row === ri).map((t) => t.x), w = Math.max(...xs) - Math.min(...xs) + f + 3;
      const line = kit.rbox(w, 0.12, 0.12, lineM, 0.02); line.position.set((Math.max(...xs) + Math.min(...xs)) / 2, cycle * s, r.z + f / 2 + 0.3);
      kit.decor(line); kit.group.add(line);
    });
    const xs = towers.filter((t) => t.row === 0).map((t) => t.x);
    kit.label(`${cycle} periods`, 'blk in soft', [Math.max(...xs) + f + 3, cycle * s, rows[0].z]);
  }
  let labels = 0;
  for (const t of towers) {                                // the class's code: every tower in one row, else those over
    if (towers.length > TOWERS_ONE_ROW && !t.over) continue;
    if (labels++ >= TOWERS_ONE_ROW) break;
    kit.label(t.code, 'blk in' + (t.over ? ' bad' : ''), [t.x, t.drawn * s + 1.2, t.z]);
  }
  rows.forEach((r, ri) => {                                // a label per level past 40 classes, on its first row
    if (!r.label) return;
    const xs = towers.filter((t) => t.row === ri).map((t) => t.x);
    kit.label(r.level === null ? 'No level' : /^\d+$/.test(r.level) ? `Level ${r.level}` : r.level, 'blk in soft', [Math.min(...xs) - f - 3, 1, r.z]);
  });
  const overs = towers.filter((t) => t.over > 0), untaught = towers.reduce((n, t) => n + t.blocks.reduce((m, b) => m + b.untaught, 0), 0);
  const lessons = towers.reduce((n, t) => n + t.periods, 0);
  let note = 'Give the Assistant your allocations and each class\'s week will stack up here.';
  if (towers.length) {
    note = cycle ? `The red line is the cycle's ${cycle} periods` : 'The week has no periods yet';
    if (overs.length === 1) note += `; ${overs[0].code}'s ${overs[0].over} extra period${overs[0].over === 1 ? '' : 's'} glow red`;
    else if (overs.length) note += `; ${overs.length} classes go over it and their extra periods glow red`;
    note += '.';
    if (untaught) note += ' See-through blocks have no teacher yet.';
    const capped = towers.filter((t) => t.capped);
    if (capped.length) note += ` ${capped.slice(0, 3).map((t) => t.code).join(', ')}${capped.length > 3 ? ` and ${capped.length - 3} more` : ''}`
      + ` ${capped.length === 1 ? 'is' : 'are'} drawn only to ${capped[0].drawn} periods.`;
    if (hidden) note += ` Only the first ${towers.length} classes are drawn; ${hidden} more are not shown.`;
  }
  return {
    legend: capLegend([...subjects], overs.length ? [['Over the cycle', RED]] : []),
    counters: [[lessons, 'Lessons a cycle'], [towers.length + hidden, 'Classes'], [untaught, 'No teacher yet', untaught > 0]],
    note,
  };
}

/* ---- 06 Constraints: the cycle as a path of period tiles, rests as gaps, pinned events as flags ---- */
function constraintsScene(doc, c) {
  const { kit, THREE, T } = c;
  c.show('none');
  const week = obj(doc.week), W = layoutWeek(week, c.g);
  const days = int(week.days), spd = int(week.slots_per_day), labels = list(week.labels).map(String);
  const px = W.pitch.x, pz = W.pitch.z, tw = px * 0.78, td = pz * 0.66;
  const edgeCol = WARN, COL = { lesson: T.winOff, edge: edgeCol, pinned: AMBER };
  const tileAt = (t) => ({ type: 'tile', id: { day: t.day, slot: t.slot } });
  const delayOf = (t) => t.day * Math.min(0.18, 1.2 / Math.max(1, days)) + t.slot * Math.min(0.04, 0.8 / Math.max(1, spd));
  for (const kind of ['lesson', 'edge', 'pinned']) {
    const ts = W.tiles.filter((t) => t.kind === kind); if (!ts.length) continue;
    const lit = kind === 'lesson' ? {} : { emissive: COL[kind], emissiveIntensity: 0.2 };
    const m = kit.instanced(kit.rbox(tw, 0.6, td, null, Math.min(0.25, tw / 4)).geometry, kit.mat(COL[kind], lit), ts.length);
    kit.pickable(m, ts.map(tileAt));
    c.stagger([m], ts.length, (i, k) => c.place(m, i, ts[i].x, 0.3 * k, ts[i].z, 1, k, 1), { delay: 0.2, dur: 0.4, delayOf: (i) => delayOf(ts[i]) });
  }
  if (W.gaps.length) {                                     // rests: a low strip of path where a period would be
    const m = kit.instanced(kit.rbox(tw * 0.45, 0.3, td, null, 0.1).geometry, kit.mat(T.dirt), W.gaps.length);
    kit.pickable(m, W.gaps.map(tileAt));
    c.stagger([m], W.gaps.length, (i, k) => c.place(m, i, W.gaps[i].x, 0.15 * k, W.gaps[i].z, 1, k, 1), { delay: 0.2, dur: 0.4, delayOf: (i) => delayOf(W.gaps[i]) });
  }
  const poleM = kit.mat(POLE), clothM = kit.mat(FLAG);
  W.flags.forEach((fl, n) => {                             // a flag on each pinned event's first tile, named
    const flag = new THREE.Group(); flag.position.set(fl.x, 0, fl.z); kit.group.add(flag);
    const pole = kit.rbox(0.25, 4, 0.25, poleM, 0.05); pole.position.y = 2.6; flag.add(pole);
    const cloth = kit.rbox(Math.min(2, px * 0.9), 1.2, 0.1, clothM, 0.05); cloth.position.set(Math.min(2, px * 0.9) / 2 + 0.1, 4, 0); flag.add(cloth);
    kit.pickable(flag, tileAt(fl));
    kit.label(fl.name, 'blk in', [fl.x, 5.6, fl.z]);
    flag.scale.set(1, 0.001, 1);
    c.anim({ delay: 0.6 + n * 0.1, dur: 0.5, ease: 'back', update: (k) => flag.scale.set(1, Math.max(0.001, k), 1) });
  });
  const names = list(week.days_names), ps = list(week.positions);
  const dayName = (d) => (names[d] != null && String(names[d])) || (labels[d * spd] || '').split(/\s+/)[0] || `Day ${d + 1}`;
  const open = ps.filter((p) => obj(p).kind === 'period').length;   // the periods a cycle, breaks and closed left out
  const x0 = W.tiles.length || W.gaps.length ? Math.min(...W.tiles.concat(W.gaps).map((t) => t.x)) : 0;
  const x1 = W.tiles.length || W.gaps.length ? Math.max(...W.tiles.concat(W.gaps).map((t) => t.x)) : 0;
  const zOf = (d) => (d - (days - 1) / 2) * pz;
  for (let d = 0; d < days && spd; d++) kit.label(dayName(d), 'blk in soft', [x0 - px / 2 - 2.4, 0.8, zOf(d)]);
  const notes = list(doc.notes).filter((n) => n && typeof n === 'object' && n.text);
  const byDay = new Map();
  for (const n of notes) {
    const d = n.day; if (!Number.isInteger(d) || d < 0 || d >= days || !spd) continue;
    if (!byDay.has(d)) byDay.set(d, []); byDay.get(d).push(n);
  }
  const noteM = c.see(kit.mat(RED), 0.25);
  for (const [d, ns] of byDay) {                           // a red wash over the day a note names, and the note
    const wash = kit.rbox(x1 - x0 + px, 0.2, pz * 0.8, noteM, 0.15); wash.position.set((x0 + x1) / 2, 0.75, zOf(d));
    kit.decor(wash); kit.group.add(wash);
    ns.slice(0, NOTE_LABELS).forEach((n, i) => {
      const text = String(n.text);
      kit.label('Note: ' + (text.length > 60 ? text.slice(0, 59) + '…' : text), 'blk in soft', [x1 + px / 2, 2 + i * 1.6, zOf(d)]);
    });
    if (ns.length > NOTE_LABELS) kit.label(`+${ns.length - NOTE_LABELS} more notes`, 'blk in soft', [x1 + px / 2, 2 + NOTE_LABELS * 1.6, zOf(d)]);
  }
  const edges = list(week.edge_subjects).map(String), pinned = list(week.pinned);
  const said = [];
  if (W.flags.length) { const f = W.flags[0]; said.push(`${f.name} is pinned to ${labels[f.day * spd + f.slot] || `day ${f.day + 1}, period ${f.slot + 1}`}`); }
  if (edges.length) said.push(`${edges.slice(0, 3).join(', ')}${edges.length > 3 ? '…' : ''} ${edges.length === 1 ? 'sits' : 'sit'} at the start or end of each day`);
  return {
    legend: [['Lesson period', COL.lesson], ['Rest', T.dirt], ['Pinned', COL.pinned]]
      .concat(edges.length ? [[`Edge subject (${edges.slice(0, 3).join(', ')}${edges.length > 3 ? '…' : ''})`, edgeCol]] : [])
      .concat(byDay.size ? [['Note to check', RED]] : []),
    counters: [ps.length && days && spd ? [`${days} days · ${open}`, 'Periods a cycle'] : [days && spd ? `${days} × ${spd}` : '0', 'Days × periods'],
      [pinned.length, 'Pinned'], [notes.length, 'Notes to check', notes.length > 0]],
    note: !(days && spd) ? "Give the Assistant your timetable's week and its periods will be laid out here."
      : (said.length ? said.join('; ') : 'Each row is a day of the cycle; rests are gaps') + '.',
  };
}

/* What this page has shown: the last scene (a redraw of the same scene plays no intro) and the files already
   flown in. resetScenes() forgets the scene, for when the campus leaves the setup tabs (Build). */
const memory = { mode: null, flown: new Set() };
export function resetScenes() { memory.mode = null; }

const SCENES = { project: projectScene, manpower: manpowerScene, locations: locationsScene, requirements: requirementsScene, constraints: constraintsScene };

/* Draw one setup scene into kit.group. school = {showSchool, colourRooms, fx} from the scene object. */
export function buildScene(mode, doc, kit, school) {
  const sc = school || {};
  const motion = motionOf(sc);
  if (sc.fx && sc.fx.speed) sc.fx.speed(motion === 'reduced' ? 0.5 : 1);   // fxScale is global: set it every time
  const build = SCENES[mode];
  if (!kit || !build) { memory.mode = null; if (sc.showSchool) sc.showSchool('full'); return { legend: [], counters: [], note: '' }; }
  const d = obj(doc), fresh = memory.mode !== mode;
  memory.mode = mode;
  const out = build(d, context(kit, sc, motion, fresh));
  const problems = list(d.problems).filter((p) => typeof p === 'string' && p);
  if (problems.length) out.note = [out.note].concat(problems).filter(Boolean).join(' ');
  return out;
}
