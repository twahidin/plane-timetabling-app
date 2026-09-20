(function () {
  'use strict';

  // ---------- API ----------
  const api = async (path, opts = {}) => {
    const r = await fetch(path, { headers: { 'Content-Type': 'application/json' }, ...opts });
    if (!r.ok) throw new Error((await r.json().catch(() => ({}))).detail || r.statusText);
    return r.json();
  };
  function toInternal(org) {
    const locIndex = Object.fromEntries(org.locations.map((l, i) => [l.id, i]));
    const perIndex = Object.fromEntries(org.persons.map((p, i) => [p.id, i]));
    return {
      name: org.name, timeLabels: org.time_labels, timeUnit: org.time_unit,
      rules: { maxLoad: org.rules.max_load, maxRun: org.rules.max_run, mandatoryRest: org.rules.mandatory_rest, slotsPerDay: org.rules.slots_per_day || null },
      locations: org.locations.map((l) => ({ id: l.id, name: l.name, cap: l.cap, shared: !!l.shared, rest: !!l.rest })),
      persons: org.persons.map((p) => ({ id: p.id, name: p.name, role: p.role, avail: Array.isArray(p.avail[0]) ? p.avail : [p.avail], eligible: p.eligible.map((x) => locIndex[x]).filter((x) => x !== undefined) })),
      // student groups are planes on the engine's side (it synthesises a person per group); here they only join the load panel
      groups: (org.groups || []).map((g) => ({ id: g.id, name: g.name, band: g.band || null })),
      events: org.events.filter((e) => e.loc !== null && e.t0 !== null && locIndex[e.loc] !== undefined).map((e) => ({
        id: e.id, name: e.name, members: e.members.map((m) => perIndex[m]).filter((x) => x !== undefined),
        loc: locIndex[e.loc], t0: e.t0, dur: e.dur, sync: e.sync || undefined, fixed: !!e.fixed })),
    };
  }

  // ---------- Display helpers (lookups only; every check comes from the engine) ----------
  const T = (ds) => ds.timeLabels.length;
  const eventsIn = (ds, l, t) => ds.events.filter((e) => e.loc === l && t >= e.t0 && t < e.t0 + e.dur);
  const slotLabel = (ds, t) => ds.timeLabels[t];
  const isRestEvent = (ds, e) => !!ds.locations[e.loc].rest;
  const spanLabel = (ds, e) => (e.dur === 1 ? slotLabel(ds, e.t0) : slotLabel(ds, e.t0) + '\u2013' + slotLabel(ds, e.t0 + e.dur - 1));
  const esc = (s) => String(s == null ? '' : s).replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));

  // ---------- State ----------
  const state = { ds: null, view: 'orbit', selected: null, clashes: [], check: null };

  // ---------- Theme colours ----------
  const cssVar = (n) => getComputedStyle(document.documentElement).getPropertyValue(n).trim();
  function theme() {
    return {
      ink: cssVar('--ink'), ink2: cssVar('--ink-2'), muted: cssVar('--muted'), plane: cssVar('--plane'),
      grid: cssVar('--grid'), bad: cssVar('--bad'), stage: cssVar('--stage'), rest: cssVar('--rest'),
      locs: [1, 2, 3, 4, 5].map((i) => cssVar('--loc-' + i)),
    };
  }

  // ---------- Why "Plane"? ----------
  // Above the Three.js guard below: the dialog is page copy, and must open even with no 3D library.
  document.getElementById('why-plane').addEventListener('click', (ev) => {
    ev.preventDefault(); document.getElementById('why-dialog').showModal();
  });

  // ---------- Three.js scene ----------
  const ZS = 1.5;           // spacing between one person's plane and the next
  const canvas = document.getElementById('c');
  const viewEl = document.getElementById('view');
  if (typeof THREE === 'undefined') {
    const st = document.getElementById('status');
    if (st) st.textContent = 'The 3D library did not load (/static/three.min.js). The rest of the page still works.';
    document.getElementById('empty').hidden = false; viewEl.hidden = true;
    window.reloadModel = async () => {};
    return;
  }
  const renderer = new THREE.WebGLRenderer({ canvas, antialias: true, alpha: true });
  renderer.setPixelRatio(Math.min(window.devicePixelRatio || 1, 2));
  const scene = new THREE.Scene();
  const persp = new THREE.PerspectiveCamera(38, 1, 0.1, 4000);
  const ortho = new THREE.OrthographicCamera(-1, 1, 1, -1, 0.1, 4000);
  scene.add(new THREE.AmbientLight(0xffffff, 0.85));
  const key = new THREE.DirectionalLight(0xffffff, 0.55); key.position.set(4, 8, 6); scene.add(key);
  const fill = new THREE.DirectionalLight(0xffffff, 0.25); fill.position.set(-6, 3, -4); scene.add(fill);

  let world = new THREE.Group(); scene.add(world);
  let tiles = [];                 // meshes with userData {eventId, p}
  let extents = { w: 8, h: 5, d: 10 };
  const target = new THREE.Vector3();
  const orbit = { theta: 0.62, phi: 1.08, r: 22 };
  let orthoZoom = 1;
  const pan = new THREE.Vector3();
  let needsRender = true;
  const invalidate = () => { needsRender = true; };

  function makeLabel(text, color, opts = {}) {
    const size = opts.size || 26, pad = 6, font = opts.font || 'IBM Plex Mono';
    const c = document.createElement('canvas'); const ctx = c.getContext('2d');
    const f = `${opts.weight || 500} ${size}px "${font}", monospace`;
    ctx.font = f;
    const w = Math.ceil(ctx.measureText(text).width) + pad * 2, h = size + pad * 2;
    c.width = w * 2; c.height = h * 2; ctx.scale(2, 2);
    ctx.font = f; ctx.fillStyle = color; ctx.textBaseline = 'middle'; ctx.fillText(text, pad, h / 2);
    const tex = new THREE.CanvasTexture(c); tex.minFilter = THREE.LinearFilter;
    const sp = new THREE.Sprite(new THREE.SpriteMaterial({ map: tex, depthTest: false, transparent: true }));
    const k = opts.scale || 0.0125; sp.scale.set(w * k, h * k, 1); sp.renderOrder = 20;
    if (opts.anchor) sp.center.set(opts.anchor[0], opts.anchor[1]);
    return sp;
  }

  function disposeWorld() {
    world.traverse((o) => {
      if (o.geometry) o.geometry.dispose();
      if (o.material) { if (o.material.map) o.material.map.dispose(); o.material.dispose(); }
    });
    scene.remove(world);
    world = new THREE.Group(); scene.add(world); tiles = [];
  }

  function build() {
    disposeWorld();
    const ds = state.ds, th = theme();
    const nT = T(ds), nL = ds.locations.length, nP = ds.persons.length;
    extents = { w: nT, h: nL, d: (nP - 1) * ZS };
    world.position.set(-nT / 2, -(nL - 1) / 2, -extents.d / 2);

    const planeMat = new THREE.MeshBasicMaterial({ color: new THREE.Color(th.plane), transparent: true, opacity: 0.16, side: THREE.DoubleSide, depthWrite: false });
    const planeEdge = new THREE.LineBasicMaterial({ color: new THREE.Color(th.plane), transparent: true, opacity: 0.55 });
    const frameMat = new THREE.LineBasicMaterial({ color: new THREE.Color(th.plane), transparent: true, opacity: 0.35 });
    const gridMat = new THREE.LineBasicMaterial({ color: new THREE.Color(th.grid), transparent: true, opacity: 0.9 });

    // Floor grid on the front face: time ticks and location rows, drawn once in front of the first plane
    const zFront = -0.9;
    const g = new THREE.BufferGeometry(); const pts = [];
    for (let t = 0; t <= nT; t++) pts.push(t, -0.5, zFront, t, nL - 0.5, zFront);
    for (let l = 0; l <= nL; l++) pts.push(0, l - 0.5, zFront, nT, l - 0.5, zFront);
    g.setAttribute('position', new THREE.Float32BufferAttribute(pts, 3));
    world.add(new THREE.LineSegments(g, gridMat));

    // Person planes: one strip per eligible location, per window, spanning that window
    ds.persons.forEach((per, p) => {
      const z = p * ZS;
      per.avail.forEach(([x0, x1]) => {
        const len = x1 - x0;
        per.eligible.forEach((l) => {
          const geo = new THREE.PlaneGeometry(len, 0.92);
          const m = new THREE.Mesh(geo, planeMat); m.position.set(x0 + len / 2, l, z); m.renderOrder = 1; world.add(m);
          const e = new THREE.LineSegments(new THREE.EdgesGeometry(geo), planeEdge); e.position.copy(m.position); world.add(e);
        });
        // frame: this window's full extent across every location row, so gaps (between or outside windows) read as "not allowed here"
        const fg = new THREE.BufferGeometry();
        fg.setAttribute('position', new THREE.Float32BufferAttribute([x0, -0.5, z, x1, -0.5, z, x1, -0.5, z, x1, nL - 0.5, z, x1, nL - 0.5, z, x0, nL - 0.5, z, x0, nL - 0.5, z, x0, -0.5, z], 3));
        world.add(new THREE.LineSegments(fg, frameMat));
      });
      const lab = makeLabel(per.name, th.ink, { anchor: [1, 0.5] }); lab.position.set(-0.35, -0.5, z); world.add(lab);
      const role = makeLabel(per.role, th.muted, { size: 20, anchor: [1, 0.5] }); role.position.set(-0.35, -0.95, z); world.add(role);
    });

    // Location labels along L, time labels along T (on the front face)
    ds.locations.forEach((loc, l) => {
      const lab = makeLabel(loc.name, th.ink2, { size: 22, anchor: [1, 0.5] }); lab.position.set(-0.35, l, zFront); world.add(lab);
    });
    const spd = ds.rules.slotsPerDay, nTot = ds.timeLabels.length;
    ds.timeLabels.forEach((tl, t) => {
      // long cycles: label each day's first slot with the day, then every sixth slot with the time only
      if (nTot > 40) {
        const dayStart = spd ? t % spd === 0 : false;
        if (!dayStart && t % 6 !== 0) return;
        const text = dayStart ? tl : tl.split(' ').slice(-1)[0];
        const lab = makeLabel(text, dayStart ? th.ink : th.muted, { size: dayStart ? 22 : 18 }); lab.position.set(t + 0.5, -1.05, zFront); world.add(lab);
        return;
      }
      const lab = makeLabel(tl, th.ink2, { size: 22 }); lab.position.set(t + 0.5, -1.05, zFront); world.add(lab);
    });
    const axT = makeLabel('T  time \u2192', th.muted, { size: 20, anchor: [0, 0.5] }); axT.position.set(nT + 0.3, -1.05, zFront); world.add(axT);
    const axL = makeLabel('L  location \u2191', th.muted, { size: 20, anchor: [1, 0.5] }); axL.position.set(-0.35, nL - 0.2, zFront); world.add(axL);
    const axP = makeLabel('P  people \u27f6', th.muted, { size: 20, anchor: [0, 0.5] }); axP.position.set(nT + 0.3, -0.5, extents.d); world.add(axP);

    // Events: a tile on every member plane, plus a faint prism binding the group together
    ds.events.forEach((e) => {
      const color = new THREE.Color(ds.locations[e.loc].rest ? th.rest : th.locs[e.loc % 5]);
      const x = e.t0 + e.dur / 2, y = e.loc, w = e.dur - 0.14;
      e.members.forEach((p) => {
        const geo = new THREE.BoxGeometry(w, 0.74, 0.16);
        const mat = new THREE.MeshLambertMaterial({ color: color.clone(), transparent: true, opacity: 1 });
        const m = new THREE.Mesh(geo, mat); m.position.set(x, y, p * ZS); m.renderOrder = 3;
        m.userData = { eventId: e.id, p, pid: ds.persons[p].id, base: color.clone() };
        world.add(m); tiles.push(m);
        const edge = new THREE.LineSegments(new THREE.EdgesGeometry(geo), new THREE.LineBasicMaterial({ color: new THREE.Color(th.ink), transparent: true, opacity: 0.35 }));
        edge.position.copy(m.position); world.add(edge); m.userData.edge = edge;
      });
      if (e.members.length > 1) {
        const zs = e.members.map((p) => p * ZS), z0 = Math.min(...zs), z1 = Math.max(...zs);
        const geo = new THREE.BoxGeometry(w, 0.74, z1 - z0);
        const prism = new THREE.Mesh(geo, new THREE.MeshBasicMaterial({ color, transparent: true, opacity: 0.09, depthWrite: false }));
        prism.position.set(x, y, (z0 + z1) / 2); prism.renderOrder = 2; world.add(prism);
        const pe = new THREE.LineSegments(new THREE.EdgesGeometry(geo), new THREE.LineBasicMaterial({ color, transparent: true, opacity: 0.4 }));
        pe.position.copy(prism.position); world.add(pe);
        e._prism = pe;
      }
    });

    orbit.r = Math.max(nT, nL, extents.d) * 1.55 + 6;
    paint();
    layoutCamera();
    invalidate();
  }

  function paint() {
    if (!state.ds) return;
    const th = theme();
    const bad = new Set(state.clashes.flatMap((c) => c.tiles || []));   // engine tiles are "<event>@<person id>"
    const sel = state.selected;
    tiles.forEach((m) => {
      const k = m.userData.eventId + '@' + m.userData.pid;
      const isBad = bad.has(k), isSel = sel && sel === m.userData.eventId;
      m.material.color.copy(isBad ? new THREE.Color(th.bad) : m.userData.base);
      m.material.emissive = isSel ? m.material.color.clone().multiplyScalar(0.45) : new THREE.Color(0x000000);
      m.material.opacity = sel && !isSel && !isBad ? 0.3 : 1;
      m.material.needsUpdate = true;
      m.userData.edge.material.color.set(isBad ? th.bad : th.ink);
      m.userData.edge.material.opacity = isBad ? 0.95 : isSel ? 0.9 : 0.35;
    });
    state.ds.events.forEach((e) => { if (e._prism) e._prism.material.opacity = sel === e.id ? 0.95 : 0.4; });
    invalidate();
  }

  // ---------- Cameras ----------
  function activeCam() { return state.view === 'orbit' ? persp : ortho; }
  function resize() {
    const w = viewEl.clientWidth, h = viewEl.clientHeight;
    if (!w || !h) return;   // the 3D tab is hidden: sizing from a zero box would blank the canvas
    renderer.setSize(w, h, false);
    persp.aspect = w / h; persp.updateProjectionMatrix();
    layoutCamera(); invalidate();
  }
  // grid.js calls this when the 3D tab is shown again: the canvas is sized from its box, which was
  // zero while the tab was hidden.
  window.resizeModel = () => { resize(); invalidate(); };
  function layoutCamera() {
    const w = viewEl.clientWidth || 1, h = viewEl.clientHeight || 1, aspect = w / h;
    if (state.view === 'orbit') {
      const p = orbit;
      persp.position.set(target.x + p.r * Math.sin(p.phi) * Math.sin(p.theta), target.y + p.r * Math.cos(p.phi), target.z + p.r * Math.sin(p.phi) * Math.cos(p.theta));
      persp.up.set(0, 1, 0); persp.lookAt(target);
      return;
    }
    let uw, uh, pos, up;
    if (state.view === 'P') { uw = extents.w + 5; uh = extents.h + 3; pos = [0, 0, 1500]; up = [0, 1, 0]; }
    else if (state.view === 'L') { uw = extents.w + 5; uh = extents.d + 3; pos = [0, 1500, 0]; up = [0, 0, -1]; }
    else { uw = extents.d + 5; uh = extents.h + 3; pos = [-1500, 0, 0]; up = [0, 1, 0]; }
    let halfW = Math.max(uw / 2, (uh / 2) * aspect) * 1.08 / orthoZoom;
    let halfH = halfW / aspect;
    ortho.left = -halfW; ortho.right = halfW; ortho.top = halfH; ortho.bottom = -halfH; ortho.updateProjectionMatrix();
    ortho.position.set(pos[0] + pan.x, pos[1] + pan.y, pos[2] + pan.z);
    ortho.up.set(up[0], up[1], up[2]); ortho.lookAt(new THREE.Vector3().copy(pan));
  }

  function setView(v) {
    state.view = v; orthoZoom = 1; pan.set(0, 0, 0);
    document.querySelectorAll('[data-view]').forEach((b) => b.classList.toggle('on', b.dataset.view === v));
    const hint = document.getElementById('hint');
    hint.textContent = v === 'orbit' ? 'Drag to rotate \u00b7 scroll to zoom \u00b7 click a tile' : 'Drag to pan \u00b7 scroll to zoom \u00b7 click a tile';
    const axes = document.getElementById('axes');
    axes.innerHTML = v === 'orbit' ? 'T \u2192 time<br>L \u2191 location<br>P \u27f6 people'
      : v === 'P' ? 'Looking along P<br>every plane collapses onto one<br>T \u2192 time \u00b7 L \u2191 location'
      : v === 'L' ? 'Looking along L<br>rooms collapse, people stay apart<br>T \u2192 time \u00b7 P \u2193 person'
      : 'Looking along T<br>one instant, everyone at once<br>P \u2192 person \u00b7 L \u2191 location';
    layoutCamera(); invalidate();
  }

  // ---------- Pointer handling ----------
  let drag = null;
  canvas.addEventListener('pointerdown', (ev) => {
    drag = { x: ev.clientX, y: ev.clientY, sx: ev.clientX, sy: ev.clientY, moved: false };
    canvas.setPointerCapture(ev.pointerId); canvas.classList.add('dragging');
  });
  canvas.addEventListener('pointermove', (ev) => {
    if (!drag) return;
    const dx = ev.clientX - drag.x, dy = ev.clientY - drag.y; drag.x = ev.clientX; drag.y = ev.clientY;
    if (Math.hypot(ev.clientX - drag.sx, ev.clientY - drag.sy) > 4) drag.moved = true;
    if (state.view === 'orbit') {
      orbit.theta -= dx * 0.006; orbit.phi = Math.min(Math.PI - 0.15, Math.max(0.15, orbit.phi - dy * 0.006));
    } else {
      const unitsPerPx = (ortho.right - ortho.left) / viewEl.clientWidth;
      const right = new THREE.Vector3().setFromMatrixColumn(ortho.matrixWorld, 0);
      const upv = new THREE.Vector3().setFromMatrixColumn(ortho.matrixWorld, 1);
      pan.addScaledVector(right, -dx * unitsPerPx).addScaledVector(upv, dy * unitsPerPx);
    }
    layoutCamera(); invalidate();
  });
  canvas.addEventListener('pointerup', (ev) => {
    canvas.classList.remove('dragging');
    if (drag && !drag.moved) pick(ev);
    drag = null;
  });
  canvas.addEventListener('wheel', (ev) => { ev.preventDefault(); zoom(ev.deltaY > 0 ? 1.1 : 0.9); }, { passive: false });
  function zoom(f) {
    if (state.view === 'orbit') orbit.r = Math.min(1500, Math.max(4, orbit.r * f)); else orthoZoom = Math.min(6, Math.max(0.4, orthoZoom / f));
    layoutCamera(); invalidate();
  }
  document.getElementById('zoom-in').addEventListener('click', () => zoom(0.85));
  document.getElementById('zoom-out').addEventListener('click', () => zoom(1.18));

  const ray = new THREE.Raycaster();
  function pick(ev) {
    const r = canvas.getBoundingClientRect();
    const nd = new THREE.Vector2(((ev.clientX - r.left) / r.width) * 2 - 1, -((ev.clientY - r.top) / r.height) * 2 + 1);
    ray.setFromCamera(nd, activeCam());
    const hit = ray.intersectObjects(tiles, false)[0];
    select(hit ? hit.object.userData.eventId : null);
  }
  // ---------- Panels ----------
  const el = (id) => document.getElementById(id);
  const CHECKS = () => [
    { key: 'person', name: 'Nobody is in two places at once' },
    { key: 'location', name: `No ${Words.word('venue')} is double-booked or over capacity` },
    { key: 'sync', name: `${Words.word('requirement', { plural: true, cap: true })} that must start together do` },
    { key: 'plane', name: 'Everyone works only when and where they may' },
    { key: 'load', name: 'Nobody is overloaded or without rest' },
  ];
  const clashesFor = (e) => { const pre = e.id + '@'; return state.clashes.filter((c) => (c.tiles || []).some((t) => t.startsWith(pre))); };

  function renderReview() {
    const ds = state.ds, check = state.check;
    state.clashes = (check && check.clashes) || [];
    const byType = {};
    state.clashes.forEach((c) => { byType[c.type] = (byType[c.type] || 0) + 1; });
    el('checks').innerHTML = CHECKS().map((c) => {
      const n = byType[c.key] || 0;
      const chip = check ? `<span class="chip ${n ? 'bad' : 'ok'}">${n ? n + ' problem' + (n > 1 ? 's' : '') : 'ok'}</span>` : '<span class="chip">\u2014</span>';
      return `<div><div class="name">${esc(c.name)}</div></div>${chip}`;
    }).join('');
    const n = state.clashes.length;
    el('verdict').textContent = !check ? 'Not checked yet.'
      : n ? `${n} problem${n > 1 ? 's' : ''} found.`
      : !ds ? 'No problems found.'
      : `No problems found: ${ds.events.length} ${Words.word('requirement', { plural: ds.events.length !== 1 })}, ${ds.persons.length} ${Words.word('person', { plural: ds.persons.length !== 1 })}.`;
    el('verdict-dot').classList.toggle('bad', n > 0);
    const list = el('clash-list'); list.textContent = '';
    state.clashes.forEach((c) => {
      const li = document.createElement('li'), b = document.createElement('button');
      b.textContent = c.message || c.msg || c.type; b.addEventListener('click', () => select(c.event));
      li.appendChild(b);
      if (c.event && !String(c.event).startsWith('bk-')) {   // a booking is not an event the assistant can move
        const fix = document.createElement('a');
        fix.className = 'fix'; fix.href = '#'; fix.textContent = 'Fix…'; fix.dataset.event = c.event;
        fix.addEventListener('click', (ev) => { ev.preventDefault(); if (window.sendChat) window.sendChat('Fix ' + c.event); });
        li.appendChild(fix);
      }
      list.appendChild(li);
    });
    paint();
  }

  let loadGen = 0;   // overlapping reloads: only the newest renderLoads may touch the DOM after an await
  async function renderLoads() {
    const gen = ++loadGen;
    const ds = state.ds, box = el('loads');
    el('loads-summary').textContent = 'loading…';   // never blank while the /api/loads call is in flight
    box.textContent = '';
    const cell = (cls, text) => { const d = document.createElement('div'); if (cls) d.className = cls; d.textContent = text; box.appendChild(d); return d; };
    cell('n', ''); cell('n', `${ds.timeUnit}s (of ${ds.rules.maxLoad})`); cell('n', `in a row (max ${ds.rules.maxRun})`); cell('n', 'rest');
    let loads = {};
    let failed = null;
    try { loads = (await api('/api/loads')).loads || {}; }     // one engine call for every plane
    catch (e) { failed = e; }
    if (gen !== loadGen) return;
    try {
      if (failed) throw failed;
      const reports = ds.persons.map((p) => loads[p.id]).filter(Boolean);
      const heaviest = reports.reduce((m, r) => Math.max(m, r.load || 0), 0);
      const over = reports.filter((r) => (r.load || 0) > (r.max_load ?? ds.rules.maxLoad)).length;
      el('loads-summary').textContent = `${ds.persons.length} ${Words.word('person', { plural: true })}, busiest ${heaviest} of ${ds.rules.maxLoad}, ${over} over the limit`;
    } catch (e) {
      el('loads-summary').textContent = 'loads unavailable';   // a failed /api/loads, or anything else that went wrong computing it
    }
    // persons by role in order of first appearance (teachers first in every dataset so far), then the groups
    const roles = [];
    ds.persons.forEach((p) => { if (!roles.includes(p.role)) roles.push(p.role); });
    const rows = [];
    roles.forEach((role) => ds.persons.filter((p) => p.role === role).forEach((p, i) => rows.push({ id: p.id, name: p.name, role, first: i === 0 })));
    ds.groups.forEach((g, i) => rows.push({ id: g.id, name: g.name + (g.band ? ' (option)' : ''), role: 'Group', first: i === 0 }));
    const labelled = roles.length > 1 || ds.groups.length > 0;
    for (const per of rows) {
      if (labelled && per.first) cell('role', per.role === 'Group' ? 'Groups' : per.role + 's');
      const rep = loads[per.id];
      if (failed || !rep) { cell('', per.name); cell('n', failed ? String(failed.message) : 'no report'); cell('', ''); cell('', ''); continue; }
      const load = rep.load || 0, maxLoad = rep.max_load ?? ds.rules.maxLoad, longest = rep.longest || 0, maxRun = rep.max_run ?? ds.rules.maxRun;
      const restCount = rep.rest_count || 0, missed = (rep.rest_violations || []).length > 0;
      const pct = Math.min(100, maxLoad ? (load / maxLoad) * 100 : 0);
      const name = cell('', per.name);
      name.title = `${load} of ${maxLoad} slots, longest run ${longest} of ${maxRun}` + (missed ? `, rest missed at ${rep.rest_violations.map((t) => slotLabel(ds, t)).join(', ')}` : '');
      const bar = document.createElement('div'); bar.className = 'bar';
      const fillEl = document.createElement('i'); if (load > maxLoad) fillEl.className = 'over'; fillEl.style.width = pct + '%';
      bar.appendChild(fillEl); box.appendChild(bar);
      cell('n' + (longest > maxRun ? ' over' : ''), String(longest));
      const rc = cell('', ''); const chip = document.createElement('span');
      chip.className = 'chip ' + (missed ? 'bad' : 'ok');
      chip.textContent = missed ? 'missed' : restCount ? `${restCount} ${ds.timeUnit === 'period' ? 'slot' : 'block'}${restCount > 1 ? 's' : ''}` : 'none';
      rc.appendChild(chip);
    }
  }

  function select(eventId) {
    state.selected = eventId;
    const ds = state.ds, e = ds && ds.events.find((x) => x.id === eventId);
    if (!e) {
      el('selected').innerHTML = `<p class="empty">Nothing selected. Click a ${esc(Words.word('requirement'))} in the timetable to see where it is and whether anything is wrong with it.</p>`;
      paint();
      if (window.gridHighlight) window.gridHighlight(eventId);   // a deselect clears the grid's own 'on' cell
      return;
    }
    const loc = ds.locations[e.loc];
    const occupancy = eventsIn(ds, e.loc, e.t0).reduce((n, x) => n + x.members.length, 0);
    const mine = clashesFor(e), of = (type) => mine.filter((c) => c.type === type);
    const chip = (ok, txt) => `<span class="chip ${ok ? 'ok' : 'bad'}">${esc(txt)}</span>`;
    const first = (cs) => cs.map((c) => c.message).join('; ');
    const personBad = of('person').concat(of('plane')), locBad = of('location'), loadBad = of('load'), syncBad = of('sync');
    el('selected').innerHTML = `
      <dl class="kv">
        <dt>what</dt><dd><strong>${esc(e.name)}</strong></dd>
        <dt>where</dt><dd>${esc(loc.name)}</dd>
        <dt>when</dt><dd>${esc(spanLabel(ds, e))}</dd>
        <dt>who</dt><dd class="members">${e.members.map((p) => `<span>${esc(ds.persons[p].name)}</span>`).join('')}</dd>
        <dt>People</dt><dd>${chip(!personBad.length, personBad.length ? first(personBad) : 'everyone is free')}</dd>
        <dt>${esc(Words.word('venue', { cap: true }))}</dt><dd>${chip(!locBad.length, locBad.length ? first(locBad) : `${occupancy} of ${loc.cap} seats${loc.shared ? `, shared ${Words.word('venue')}` : ''}`)}</dd>
        <dt>Workload</dt><dd>${chip(!loadBad.length, isRestEvent(ds, e) ? 'rest, not counted' : loadBad.length ? first(loadBad) : 'within limits')}</dd>
        <dt>Timing</dt><dd>${chip(!syncBad.length, syncBad.length ? first(syncBad) : e.sync ? 'starts with its group' : 'on its own')}</dd>
        <dt></dt><dd class="actions">${e.fixed ? '' : '<a class="move" href="#">Move…</a> '}<a class="book" href="#">Book this room</a></dd>
        <dt>print</dt><dd class="actions">${e.members.filter((p) => String(ds.persons[p].role || '').startsWith('Teacher'))
          .map((p) => `<a class="print-teacher" href="#" data-id="${esc(ds.persons[p].id)}">Print ${esc(ds.persons[p].name)}</a>`).join(' ')}
          ${isRestEvent(ds, e) ? '' : `<a class="print-room" href="#" data-id="${esc(loc.id)}">Print ${esc(loc.name)}</a>`}</dd>
      </dl>`;
    // A pinned event cannot be moved, but its room can still be booked for another day.
    const moveLink = el('selected').querySelector('.move');
    if (moveLink) moveLink.addEventListener('click', (ev) => { ev.preventDefault(); if (window.sendChat) window.sendChat('Propose a move for ' + e.id); });
    const bookLink = el('selected').querySelector('.book');
    if (bookLink) bookLink.addEventListener('click', (ev) => {
      ev.preventDefault();
      if (window.draftChat) window.draftChat('Book ' + loc.name + ' on YYYY-MM-DD ' + spanLabel(ds, e) + ' for ');
    });
    el('selected').querySelectorAll('.print-teacher').forEach((a) => a.addEventListener('click', (ev) => {
      ev.preventDefault(); if (window.openPrint) window.openPrint('teacher', a.dataset.id);
    }));
    el('selected').querySelectorAll('.print-room').forEach((a) => a.addEventListener('click', (ev) => {
      ev.preventDefault(); if (window.openPrint) window.openPrint('room', a.dataset.id);
    }));
    paint();
    if (window.gridHighlight) window.gridHighlight(eventId);   // the grid view (if any) follows the model
  }
  window.selectEvent = select;

  // Search agent: the question goes to the engine; the panel only routes and renders
  function qArgs() {
    const ds = state.ds, type = el('q-type').value;
    if (!ds) { el('q-args').innerHTML = ''; return; }
    const persons = ds.persons.map((p) => `<option value="${esc(p.id)}">${esc(p.name)}</option>`).join('');
    const locs = ds.locations.map((l) => `<option value="${esc(l.id)}">${esc(l.name)}</option>`).join('');
    const times = ds.timeLabels.map((t, i) => `<option value="${i}">${esc(t)}</option>`).join('');
    if (type === 'load') el('q-args').innerHTML = `<select id="q-a" aria-label="Person">${persons}</select><div></div>`;
    else if (type === 'where') el('q-args').innerHTML = `<select id="q-a" aria-label="Person">${persons}</select><select id="q-b" aria-label="Time">${times}</select>`;
    else if (type === 'who') el('q-args').innerHTML = `<select id="q-a" aria-label="Location">${locs}</select><select id="q-b" aria-label="Time">${times}</select>`;
    else {
      el('q-args').innerHTML = `<select id="q-a" aria-label="First person">${persons}</select><select id="q-b" aria-label="Second person">${persons}</select>`;
      if (ds.persons.length > 1) el('q-b').selectedIndex = 1;
    }
    el('q-answer').textContent = ''; el('q-trace').textContent = '';
  }
  async function runQuery() {
    if (!state.ds) return;
    const kind = el('q-type').value, a = el('q-a').value, b = el('q-b') ? el('q-b').value : null;
    const args = kind === 'load' ? { person: a }
      : kind === 'where' ? { person: a, slot: +b }
      : kind === 'who' ? { location: a, slot: +b }
      : { a, b };
    el('q-answer').textContent = '\u2026'; el('q-trace').textContent = '';
    let res;
    try { res = await api('/api/query', { method: 'POST', body: JSON.stringify({ kind, args }) }); }
    catch (e) { el('q-answer').textContent = e.message; return; }
    el('q-answer').textContent = res.answer || '';
    const ul = el('q-trace');
    (res.trace || []).forEach(([who, what]) => {
      const li = document.createElement('li'), b = document.createElement('b'), s = document.createElement('span');
      b.textContent = who; s.textContent = what;
      if (/^\S*\s+[01]+$/.test(String(what))) s.className = 'mask';
      li.appendChild(b); li.appendChild(s); ul.appendChild(li);
    });
  }

  function renderLegend() {
    const ds = state.ds;
    el('legend').innerHTML = `<span><span class="sw plane"></span>one strip per ${esc(Words.word('person'))}, as wide as their working hours</span>` +
      ds.locations.map((l, i) => `<span><span class="sw" style="background:${l.rest ? 'var(--rest)' : 'var(--loc-' + ((i % 5) + 1) + ')'}"></span>${esc(l.name)}${l.rest ? ' (rest row)' : l.shared ? ' (shared)' : ''}</span>`).join('') +
      `<span><span class="sw prism"></span>${esc(Words.word('requirement', { plural: true }))} that must start together</span><span><span class="sw bad"></span>problem</span>`;
  }

  // ---------- Status strip ----------
  function statusError(msg) {
    const s = document.createElement('span'); s.className = 'err'; s.textContent = msg; el('status').appendChild(s);
  }
  function setStatus(parts, err) {
    const box = el('status'); box.textContent = '';
    parts.forEach((t) => { const s = document.createElement('span'); s.textContent = t; box.appendChild(s); });
    if (err) statusError(err);
  }
  async function renderStatus(data) {
    const parts = [];
    const c = data.check;
    parts.push(!c ? 'check: none yet' : c.ok ? 'check: ok' : `check: ${c.clashes.length} clash${c.clashes.length === 1 ? '' : 'es'}`);
    if (data.draft) parts.push(`draft: ${data.draft.persons} persons, ${data.draft.locations} locations, ${data.draft.events} events (${data.draft.placed} placed)`);
    let err = null;
    try {
      const u = await api('/api/usage');
      if (u.error) err = 'engine: ' + u.error;
      else parts.push(`engine ${u.period || ''}: ${u.builds} of ${u.monthly_builds} builds, ${u.queries} queries, ${u.checks} checks`);
    } catch (e) { err = 'engine: ' + e.message; }
    setStatus(parts, err);
  }

  // ---------- Loading from the API ----------
  async function loadFromApi() {
    const data = await api('/api/solid');
    state.check = data.check;
    if (window.Words) { Words.set(data.vocabulary); Words.apply(document); }   // the timetable's own words, page-wide
    if (!data.organisation) {
      state.ds = null; state.selected = null;
      el('empty').hidden = false; el('view').hidden = true;
      renderReview(); select(null); qArgs(); el('loads').textContent = ''; el('legend').textContent = '';
      el('loads-summary').textContent = 'no timetable yet';
      await renderStatus(data);
      if (window.gridReload) window.gridReload();
      return;
    }
    el('empty').hidden = true; el('view').hidden = false;
    state.ds = toInternal(data.organisation); state.selected = null;
    renderLegend(); build(); renderReview(); select(null); qArgs(); await renderLoads();
    await renderStatus(data);   // after the loads call, so the usage counts include it
    if (window.gridReload) window.gridReload();
  }
  window.reloadModel = loadFromApi;

  function refresh() { if (!state.ds) return; build(); renderReview(); }

  // ---------- Wire up ----------
  document.querySelectorAll('[data-view]').forEach((b) => b.addEventListener('click', () => setView(b.dataset.view)));
  el('q-type').addEventListener('change', qArgs);
  el('q-run').addEventListener('click', runQuery);
  el('rebuild').addEventListener('click', async () => {
    const btn = el('rebuild'); btn.disabled = true;
    let problem = null;   // reported after the reload, so renderStatus does not wipe it
    try {
      const res = await api('/api/build', { method: 'POST' });
      if (!res.ok) {
        const n = (res.unplaced || []).length, m = (res.clashes || []).length;
        problem = `build did not settle: ${n} unplaced, ${m} clash${m === 1 ? '' : 'es'}. The draft is unchanged.`;
      }
      await loadFromApi();
      if (window.loadDraft) window.loadDraft();
    } catch (e) { problem = e.message; }
    finally { btn.disabled = false; if (problem) statusError(problem); }
  });

  const loadsDetails = el('loads-details');
  if (loadsDetails) {
    try { loadsDetails.open = localStorage.getItem('loads-open') === '1'; } catch (e) { /* private mode etc: stays collapsed */ }
    loadsDetails.addEventListener('toggle', () => {
      try { localStorage.setItem('loads-open', loadsDetails.open ? '1' : '0'); } catch (e) { /* ignore */ }
    });
  }

  new ResizeObserver(resize).observe(viewEl);
  window.matchMedia('(prefers-color-scheme: dark)').addEventListener('change', refresh);
  new MutationObserver(refresh).observe(document.documentElement, { attributes: true, attributeFilter: ['data-theme'] });

  function frame() {
    if (needsRender) { needsRender = false; renderer.render(scene, activeCam()); }
    requestAnimationFrame(frame);
  }

  const start = () => {
    resize(); setView('orbit'); frame();
    loadFromApi().catch((e) => setStatus([], e.message));
  };
  if (document.fonts && document.fonts.ready) document.fonts.ready.then(start); else start();
})();
