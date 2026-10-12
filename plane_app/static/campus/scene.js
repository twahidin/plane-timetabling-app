/* The campus scene (spec 2026-10-11-campus-looks-design.md §2.4–2.5), ported from the approved mockup
   (docs/design/2026-10-11-campus/mockup.html) and drawn from the /api/campus document: buildings by block
   and level, rooms as windows that light with their lesson, halls, fields, trees, walkers and clash pins.
   Construction draws the building site until the timetable is checked with no clashes. The scene only
   draws; main.js loads the data. Rendering is on demand and stops while inactive. */
import * as THREE from 'three';
import { OrbitControls } from 'three/addons/controls/OrbitControls.js';
import { RoundedBoxGeometry } from 'three/addons/geometries/RoundedBoxGeometry.js';
import { CSS2DRenderer, CSS2DObject } from 'three/addons/renderers/CSS2DRenderer.js';
import { themeFor } from 'campus/themes';

const ROOM_W = 2.6, H = 3.2;
const ease = {
  back: (k) => { const c = 2.2, c3 = c + 1; return 1 + c3 * Math.pow(k - 1, 3) + c * Math.pow(k - 1, 2); },
  cubic: (k) => 1 - Math.pow(1 - k, 3), inout: (k) => (k < 0.5 ? 4 * k * k * k : 1 - Math.pow(-2 * k + 2, 3) / 2), lin: (k) => k,
};
function mulberry(a) {
  return () => { a |= 0; a = (a + 0x6d2b79f5) | 0; let t = Math.imul(a ^ (a >>> 15), 1 | a); t = (t + Math.imul(t ^ (t >>> 7), 61 | t)) ^ t; return ((t ^ (t >>> 14)) >>> 0) / 4294967296; };
}

export function createScene(host) {
  const renderer = new THREE.WebGLRenderer({ antialias: true, alpha: true });
  renderer.shadowMap.enabled = true; renderer.shadowMap.type = THREE.PCFSoftShadowMap;
  renderer.toneMapping = THREE.ACESFilmicToneMapping; renderer.outputColorSpace = THREE.SRGBColorSpace;
  host.appendChild(renderer.domElement);
  const labels = new CSS2DRenderer();
  Object.assign(labels.domElement.style, { position: 'absolute', inset: '0', pointerEvents: 'none' });
  labels.domElement.className = 'campus-labels';
  host.appendChild(labels.domElement);
  const camera = new THREE.PerspectiveCamera(30, 1, 0.5, 2000);
  const controls = new OrbitControls(camera, renderer.domElement);
  controls.enableDamping = true; controls.enablePan = false; controls.minPolarAngle = 0.45; controls.maxPolarAngle = 1.22;
  controls.addEventListener('change', () => { dirty = true; });

  let scene = null, T = null, theme = null, world = null, mats = [], tweens = [], rnd = mulberry(7);
  let active = false, dirty = true, raf = 0, last = 0, frame = 0, picker = null, fitted = false;
  let quality = 2, slow = 0, w0 = 0, h0 = 0;

  /* ---- helpers (the mockup's) ---- */
  const mat = (color, opts = {}) => { const m = new THREE.MeshStandardMaterial(Object.assign({ color, roughness: T.rough, metalness: T.metal }, opts)); mats.push(m); return m; };
  function rbox(w, h, d, m, r = T.round) {
    const g = r > 0.02 ? new RoundedBoxGeometry(w, h, d, 3, Math.min(r, w / 2 - 0.01, h / 2 - 0.01, d / 2 - 0.01)) : new THREE.BoxGeometry(w, h, d);
    const mesh = new THREE.Mesh(g, m); mesh.castShadow = true; mesh.receiveShadow = true; return mesh;
  }
  function decor(o) { o.traverse((m) => { m.userData.nopick = true; }); }     // never swallows a click
  function label(text, cls, parent, pos) {
    const el = document.createElement('div'); el.className = cls; el.textContent = text;
    const o = new CSS2DObject(el); o.position.copy(pos); parent.add(o); return o;
  }
  function tween(o) { o.t = 0; o.delay = o.delay || 0; o.ease = o.ease || ease.cubic; tweens.push(o); dirty = true; return o; }
  function stepTweens(dt) {
    for (let i = tweens.length - 1; i >= 0; i--) {
      const w = tweens[i]; if (w.delay > 0) { w.delay -= dt; continue; }
      w.t += dt; const k = Math.min(1, w.t / w.dur); w.update(w.ease(k));
      if (k >= 1) { tweens.splice(i, 1); if (w.done) w.done(); }
    }
  }
  function clear() {
    tweens = [];
    if (!scene) return;
    scene.traverse((o) => { if (o.geometry) o.geometry.dispose(); if (o.element) o.element.remove(); if (o.isLight && o.dispose) o.dispose(); });
    mats.splice(0).forEach((m) => { if (m.map) m.map.dispose(); m.dispose(); });
  }

  /* ---- quality: pixel ratio, then shadows, step down after 1.5 s of slow frames ---- */
  function applyQuality() {
    renderer.setPixelRatio(Math.min(devicePixelRatio, [1, 1.25, 2][quality]));
    renderer.shadowMap.enabled = quality > 0;
    if (world && world.sun) world.sun.castShadow = quality > 0;              // recompiles: shadows truly off
    w0 = 0; sizeTo(false);
  }
  function budget(dt) {
    if (dt > 0.045) slow += dt; else slow = Math.max(0, slow - dt);
    if (slow > 1.5 && quality > 0) { quality--; slow = 0; applyQuality(); }
  }

  /* ---- camera: fit the grounds, horizontal-FOV aware; a taller FOV when portrait ---- */
  function placeCamera(reset) {
    if (!world) return;
    const g = world.grounds;
    camera.fov = camera.aspect < 1 ? 44 : 30; camera.updateProjectionMatrix();
    const vf = THREE.MathUtils.degToRad(camera.fov), hf = 2 * Math.atan(Math.tan(vf / 2) * camera.aspect);
    // seen from the corner the plate is a diamond about 0.71 × (w + d) wide: fit that, and its height
    const need = Math.max((0.37 * (g.w + g.d)) / Math.tan(hf / 2), (0.38 * Math.hypot(g.w, g.d)) / Math.tan(vf / 2)) * (camera.aspect < 1 ? 1.18 : 1);   // near corners loom larger in portrait
    controls.minDistance = need * 0.35; controls.maxDistance = need * 2.2;
    const dir = reset ? new THREE.Vector3(1, 0.86, 1.08).normalize() : camera.position.clone().sub(controls.target).normalize();
    if (reset) controls.target.set(0, 2, 0);
    camera.position.copy(controls.target).addScaledVector(dir, need);
    camera.far = need * 6; camera.updateProjectionMatrix();
    controls.update(); dirty = true;
  }
  function sizeTo(force) {
    const w = host.clientWidth, h = host.clientHeight;
    if (!w || !h || (!force && w === w0 && h === h0)) return;
    w0 = w; h0 = h;
    const was = camera.aspect;
    renderer.setSize(w, h); labels.setSize(w, h);
    camera.aspect = w / h; camera.updateProjectionMatrix();
    if (!fitted || (was < 1) !== (camera.aspect < 1)) placeCamera(!fitted);
    fitted = true; dirty = true;
  }

  /* ---- the world ---- */
  function build(doc, look, finished) {
    clear();
    theme = themeFor(look, finished); T = theme.T; rnd = mulberry(7);
    scene = new THREE.Scene();
    renderer.toneMappingExposure = T.exposure;
    const g = doc.grounds || { w: 48, d: 34 };
    const R = 0.5 * Math.hypot(g.w, g.d);
    scene.add(new THREE.HemisphereLight(T.hemi[0], T.hemi[1], T.hemi[2]));
    scene.add(new THREE.AmbientLight('#ffffff', T.ambient));
    const sun = new THREE.DirectionalLight(T.sun[0], T.sun[1]);
    sun.position.set(-R * 0.6, R * 1.1, R * 0.7); sun.castShadow = quality > 0;
    const small = host.clientWidth < 760; sun.shadow.mapSize.set(small ? 1024 : 2048, small ? 1024 : 2048);
    Object.assign(sun.shadow.camera, { left: -R - 4, right: R + 4, top: R + 4, bottom: -R - 4, near: 1, far: R * 4 });
    sun.shadow.bias = -0.0004; sun.shadow.normalBias = 0.04; sun.shadow.radius = 3;
    scene.add(sun);

    world = { sun, grounds: g, rooms: {}, pickables: [], walkers: [], clouds: [], pins: [], mark: null, jib: null, trolley: null, boxes: [] };
    ground(g);
    courtyardPaths(doc);
    (doc.areas || []).forEach(field);
    const wallM = mat(T.wall), trimM = mat(T.trim);
    world.sillM = mat('#ffffff');
    let roofN = 0;
    for (const b of doc.buildings || []) {
      world.boxes.push(b);
      if (b.kind === 'hall') hall(b, roofN++);
      else building(b, wallM, trimM, roofN++);
    }
    for (const r of doc.rooms || []) {                       // home tint and anchors
      const rec = world.rooms[r.id];
      if (rec) { rec.home = r.home; rec.mat.color.set(r.home ? T.home : T.winOff); }
    }
    const before = scene.children.length;
    if (!theme.site) trees(g);
    if (theme.name === 'toy') clouds(g);
    if (theme.site) site(doc);
    scene.children.slice(before).forEach(decor);
    dirty = true;
  }

  function ground(g) {
    const top = rbox(g.w, 1, g.d, mat(T.grass), 0.5); top.position.y = -0.5; scene.add(top);
    const base = rbox(g.w - 1.4, 3.2, g.d - 1.4, mat(T.dirt), 0.9); base.position.y = -2.4; scene.add(base);
  }

  function courtyardPaths(doc) {
    const as = doc.areas || [];
    if (!as.length) return;
    const x0 = Math.min(...as.map((a) => a.x - a.w / 2)) - 2, x1 = Math.max(...as.map((a) => a.x + a.w / 2)) + 2;
    const z0 = Math.min(...as.map((a) => a.z - a.d / 2)) - 2, z1 = Math.max(...as.map((a) => a.z + a.d / 2)) + 2;
    const pathM = mat(T.path), wd = 2.2;
    const strip = (x, z, w, d) => { const p = rbox(w, 0.14, d, pathM, 0.06); p.position.set(x, 0.07, z); p.castShadow = false; scene.add(p); };
    const gw = world.grounds.w - 4;
    strip(0, z0 - wd / 2, gw, wd); strip(0, z1 + wd / 2, gw, wd);
    strip(x0 - wd / 2, (z0 + z1) / 2, wd, z1 - z0); strip(x1 + wd / 2, (z0 + z1) / 2, wd, z1 - z0);
    world.court = { x0: x0 - wd, x1: x1 + wd, z0: z0 - wd, z1: z1 + wd };
  }

  function fieldTexture() {
    const c = document.createElement('canvas'); c.width = 512; c.height = 384; const x = c.getContext('2d');
    x.fillStyle = T.field; x.fillRect(0, 0, 512, 384);
    if (!theme.site) for (let i = 0; i < 8; i++) { x.fillStyle = i % 2 ? 'rgba(255,255,255,.07)' : 'rgba(0,0,0,.03)'; x.fillRect(i * 64, 0, 64, 384); }
    else { x.strokeStyle = 'rgba(120,80,40,.18)'; x.lineWidth = 10; for (let i = -2; i < 6; i++) { x.beginPath(); x.moveTo(i * 110, 384); x.bezierCurveTo(i * 110 + 80, 250, i * 110 + 20, 120, i * 110 + 140, 0); x.stroke(); } }
    x.strokeStyle = T.fieldLine; x.lineWidth = 6; x.globalAlpha = 0.95;
    x.strokeRect(24, 24, 464, 336); x.beginPath(); x.moveTo(256, 24); x.lineTo(256, 360); x.stroke();
    x.beginPath(); x.arc(256, 192, 52, 0, Math.PI * 2); x.stroke(); x.strokeRect(24, 120, 70, 144); x.strokeRect(418, 120, 70, 144);
    const t = new THREE.CanvasTexture(c); t.colorSpace = THREE.SRGBColorSpace; t.anisotropy = 8; return t;
  }
  function field(a) {
    const m = mat('#ffffff', { map: fieldTexture(), roughness: 0.9 });
    const f = rbox(a.w, 0.16, a.d, m, 0.05); f.position.set(a.x, 0.08, a.z); f.castShadow = false; scene.add(f);
    if (a.room) {
      f.userData = { kind: 'room', id: a.room };
      world.pickables.push(f);
      const rec = { mesh: f, mat: m, anchor: new THREE.Vector3(a.x, 0.5, a.z), home: null, field: true };
      world.rooms[a.room] = rec;
      label(a.name, 'blk in', scene, new THREE.Vector3(a.x, 1.2, a.z - a.d / 2));
    }
  }

  function building(b, wallM, trimM, n) {
    const g = new THREE.Group(); g.position.set(b.x, 0, b.z); scene.add(g);
    const per = b.per_facade || 1, levels = b.levels || [];
    for (let lv = 0; lv < levels.length; lv++) {
      const L = new THREE.Group(); L.position.y = lv * H; g.add(L);
      const slab = rbox(b.w, H - 0.25, b.d, wallM); slab.position.y = (H - 0.25) / 2; L.add(slab);
      const band = rbox(b.w + 0.25, 0.25, b.d + 0.25, trimM, 0.05); band.position.y = H - 0.125; L.add(band);
      levels[lv].forEach((id, i) => {
        const front = i < per, k = front ? i : i - per;
        const along = -b.w / 2 + 0.5 + ROOM_W * (k + 0.5);
        const wm = mat(T.winOff, { emissive: '#000000', roughness: 0.35, metalness: 0 });
        const win = rbox(ROOM_W * 0.66, 1.5, 0.18, wm, Math.min(T.round, 0.18));
        win.position.set(along, 1.55, front ? b.d / 2 + 0.02 : -b.d / 2 - 0.02); win.castShadow = false; L.add(win);
        const sill = rbox(ROOM_W * 0.74, 0.14, 0.4, world.sillM, 0.06); sill.position.set(along, 0.72, win.position.z); L.add(sill);
        win.userData = { kind: 'room', id };
        world.pickables.push(win);
        const anchor = new THREE.Vector3(b.x + along, lv * H + 1.55, b.z + win.position.z);
        world.rooms[id] = { mesh: win, mat: wm, anchor, home: null };
      });
    }
    const roofY = levels.length * H;
    const roof = rbox(b.w + 1.2, 0.7, b.d + 1.2, mat(theme.site ? T.roofs[0] : T.roofs[n % 3]), 0.3); roof.position.set(0, roofY + 0.2, 0); g.add(roof);
    for (let k = 0; k < Math.min(3, Math.max(1, Math.floor(b.w / 8))); k++) {
      const ac = rbox(1.4, 0.8, 1.1, world.sillM, 0.25); ac.position.set(-b.w / 3 + k * (b.w / 3), roofY + 0.9, -1); g.add(ac);
    }
    if (theme.site) scaffold(g, b, roofY);
    label(b.label, 'blk in', g, new THREE.Vector3(0, roofY + 2.0, 0));
  }

  function hall(b, n) {
    const g = new THREE.Group(); g.position.set(b.x, 0, b.z); scene.add(g);
    const body = rbox(b.w, 4.4, b.d, mat(T.wall)); body.position.y = 2.2; g.add(body);
    const dm = mat(T.winOff, { emissive: '#000000' });
    const door = rbox(3, 2.4, 0.2, dm, 0.1); door.position.set(0, 1.2, b.d / 2 + 0.05); g.add(door);
    const id = (b.levels[0] || [])[0];
    if (id) {
      door.userData = { kind: 'room', id }; body.userData = { kind: 'room', id };
      world.pickables.push(door, body);
      world.rooms[id] = { mesh: door, mat: dm, anchor: new THREE.Vector3(b.x, 2.4, b.z + b.d / 2), home: null };
    }
    const sh = new THREE.Shape(); sh.moveTo(-b.w / 2 - 0.6, 0); sh.lineTo(b.w / 2 + 0.6, 0); sh.lineTo(0, 2.6); sh.closePath();
    const rg = new THREE.ExtrudeGeometry(sh, { depth: b.d + 0.8, bevelEnabled: true, bevelSize: 0.18, bevelThickness: 0.18, bevelSegments: 3 });
    rg.translate(0, 0, -(b.d + 0.8) / 2);
    const roof = new THREE.Mesh(rg, mat(theme.site ? T.roofs[0] : T.roofs[(n + 2) % 3])); roof.position.y = 4.4; roof.castShadow = true; g.add(roof);
    label(b.label, 'blk in', g, new THREE.Vector3(0, 8.2, 0));
  }

  function inBox(x, z, pad) {
    return world.boxes.some((b) => Math.abs(x - b.x) < b.w / 2 + pad && Math.abs(z - b.z) < b.d / 2 + pad)
      || (world.court && x > world.court.x0 - pad && x < world.court.x1 + pad && z > world.court.z0 - pad && z < world.court.z1 + pad);
  }
  function tree(x, z, n) {
    if (!world.treeM) world.treeM = { trunk: mat(T.trunk), leaves: T.leaves.map((c) => mat(c)) };
    const g = new THREE.Group(); g.position.set(x, 0, z); scene.add(g);
    const trunk = new THREE.Mesh(new THREE.CylinderGeometry(0.18, 0.26, 1.6, 8), world.treeM.trunk); trunk.position.y = 0.8; trunk.castShadow = true; g.add(trunk);
    [[0, 2.5, 0, 1.25], [0.6, 3.2, 0.2, 0.9], [-0.55, 3.0, -0.3, 0.85]].forEach(([px, py, pz, r], k) => {
      const s = new THREE.Mesh(new THREE.SphereGeometry(r, 20, 16), world.treeM.leaves[(n + k) % 3]); s.position.set(px, py, pz); s.castShadow = true; g.add(s);
    });
  }
  function trees(g) {
    const hw = g.w / 2 - 2.2, hd = g.d / 2 - 2.2; let n = 0;
    for (let x = -hw; x <= hw; x += 5.5) for (const z of [-hd, hd]) if (!inBox(x, z, 1.5) && rnd() < 0.8) tree(x + (rnd() - 0.5), z, n++);
    for (let z = -hd + 5; z <= hd - 5; z += 5.5) for (const x of [-hw, hw]) if (!inBox(x, z, 1.5) && rnd() < 0.8) tree(x, z + (rnd() - 0.5), n++);
  }
  function clouds(g) {
    for (let c = 0; c < 4; c++) {
      const grp = new THREE.Group(); const m = mat('#ffffff', { roughness: 1 });
      [[0, 0, 0, 1.6], [1.6, -0.2, 0.3, 1.2], [-1.5, -0.3, -0.2, 1.1], [0.6, 0.7, -0.2, 1.1]].forEach(([x, y, z, r]) => {
        const s = new THREE.Mesh(new THREE.SphereGeometry(r, 18, 14), m); s.position.set(x, y, z); s.castShadow = true; grp.add(s);
      });
      grp.position.set(-g.w / 2 + c * (g.w / 3), 15 + (c % 2) * 3, -g.d / 2 + c * (g.d / 4)); scene.add(grp); world.clouds.push(grp);
    }
  }

  /* ---- the building site (Construction until the check is clean) ---- */
  function stripeTex(a, b) {
    const c = document.createElement('canvas'); c.width = 256; c.height = 32; const x = c.getContext('2d');
    x.fillStyle = a; x.fillRect(0, 0, 256, 32); x.fillStyle = b;
    for (let i = -1; i < 9; i++) { x.beginPath(); x.moveTo(i * 32, 32); x.lineTo(i * 32 + 16, 32); x.lineTo(i * 32 + 32, 0); x.lineTo(i * 32 + 16, 0); x.fill(); }
    const t = new THREE.CanvasTexture(c); t.colorSpace = THREE.SRGBColorSpace; return t;
  }
  function latticeTex(rx, ry) {
    const c = document.createElement('canvas'); c.width = c.height = 64; const x = c.getContext('2d');
    x.strokeStyle = '#ffc21a'; x.lineWidth = 7; x.strokeRect(3, 3, 58, 58); x.lineWidth = 5; x.beginPath(); x.moveTo(3, 61); x.lineTo(61, 3); x.stroke();
    const t = new THREE.CanvasTexture(c); t.colorSpace = THREE.SRGBColorSpace; t.wrapS = t.wrapT = THREE.RepeatWrapping; t.repeat.set(rx, ry); return t;
  }
  function site(doc) {
    const g = world.grounds, hw = g.w / 2 - 0.6, hd = g.d / 2 - 0.6;
    const boardM = mat('#ffffff', { map: stripeTex('#ff7a1a', '#ffffff') }), footM = mat('#4b4f5c');
    const run = (x0, z0, x1, z1) => {
      const len = Math.hypot(x1 - x0, z1 - z0), n = Math.max(1, Math.round(len / 3.6));
      for (let i = 0; i < n; i++) {
        const k = (i + 0.5) / n, x = x0 + (x1 - x0) * k, z = z0 + (z1 - z0) * k;
        if (Math.abs(x) < 3 && z > 0) continue;                       // the gate
        const b = rbox(3.3, 0.8, 0.14, boardM, 0.06); b.position.set(x, 0.75, z); b.rotation.y = Math.atan2(-(z1 - z0), x1 - x0); scene.add(b);
        for (const s of [-1.4, 1.4]) { const f = rbox(0.5, 0.3, 0.6, footM, 0.08); f.position.set(x + Math.cos(b.rotation.y) * s, 0.15, z - Math.sin(b.rotation.y) * s); scene.add(f); }
      }
    };
    run(-hw, hd, hw, hd); run(hw, hd, hw, -hd); run(hw, -hd, -hw, -hd); run(-hw, -hd, -hw, hd);
    const coneM = mat('#ff7a1a'), bandM = mat('#ffffff');
    [[-2.6, hd - 1], [2.6, hd - 1]].concat(world.court ? [[world.court.x0, world.court.z0], [world.court.x1, world.court.z1]] : []).forEach(([x, z]) => {
      const c = new THREE.Group(); c.position.set(x, 0, z); scene.add(c);
      const base = rbox(0.9, 0.12, 0.9, coneM, 0.05); base.position.y = 0.06; c.add(base);
      const cone = new THREE.Mesh(new THREE.ConeGeometry(0.34, 1.0, 18), coneM); cone.position.y = 0.6; cone.castShadow = true; c.add(cone);
      const band = new THREE.Mesh(new THREE.CylinderGeometry(0.2, 0.25, 0.16, 18), bandM); band.position.y = 0.62; c.add(band);
    });
    for (let i = 0; i < 6; i++) { const b = rbox(1.1, 0.5, 0.6, mat(['#e07a5f', '#d96b50'][i % 2]), 0.08); b.position.set(-hw + 3 + (i % 3) * 1.15, 0.25 + Math.floor(i / 3) * 0.5, -hd + 2); scene.add(b); }
    // tower crane at the north-east corner, its jib swinging over the site
    const crane = new THREE.Group(); crane.position.set(hw - 3, 0, -hd + 3); scene.add(crane);
    const latM = (rx, ry) => mat('#ffffff', { map: latticeTex(rx, ry), transparent: true, alphaTest: 0.5, side: THREE.DoubleSide });
    const foot = rbox(3.2, 0.6, 3.2, mat('#9aa0a8'), 0.1); foot.position.y = 0.3; crane.add(foot);
    const tall = Math.max(22, Math.min(40, H * Math.max(...(doc.buildings || []).map((b) => (b.levels || []).length), 3) + 12));
    const mast = new THREE.Mesh(new THREE.BoxGeometry(1.3, tall, 1.3), latM(1, Math.round(tall * 0.77))); mast.position.y = tall / 2 + 0.6; mast.castShadow = true; crane.add(mast);
    const jib = new THREE.Group(); jib.position.y = tall + 0.6; crane.add(jib); world.jib = jib;
    const arm = new THREE.Mesh(new THREE.BoxGeometry(28, 1.1, 1.1), latM(25, 1)); arm.position.x = 7; arm.castShadow = true; jib.add(arm);
    const cab = rbox(1.8, 1.6, 1.8, mat('#ffc21a'), 0.3); cab.position.set(0.2, -1.1, 1.3); jib.add(cab);
    const weight = rbox(3, 1.6, 1.8, mat('#8d939c'), 0.2); weight.position.set(-5.5, -0.9, 0); jib.add(weight);
    const peak = new THREE.Mesh(new THREE.ConeGeometry(0.9, 3.4, 4), latM(1, 3)); peak.position.y = 2.2; peak.rotation.y = Math.PI / 4; jib.add(peak);
    const trolley = new THREE.Group(); trolley.position.x = 15; jib.add(trolley); world.trolley = trolley;
    const cable = new THREE.Mesh(new THREE.CylinderGeometry(0.05, 0.05, 12, 6), mat('#3b3f48')); cable.position.y = -6; trolley.add(cable);
    const hook = rbox(0.9, 0.7, 0.9, mat('#ffc21a'), 0.2); hook.position.y = -12.4; trolley.add(hook);
  }
  function scaffold(g, b, roofY) {
    const sc = new THREE.Group(); g.add(sc); g = sc;
    const poleM = mat('#9aa6b2'), plankM = mat('#d9a35f'), HT = roofY + 1.6, off = 0.9;
    const hx = b.w / 2 + off, hz = b.d / 2 + off;
    const pole = (x, z) => { const p = new THREE.Mesh(new THREE.CylinderGeometry(0.09, 0.09, HT, 6), poleM); p.position.set(x, HT / 2, z); p.castShadow = true; g.add(p); };
    for (let x = -hx; x <= hx + 0.01; x += (2 * hx) / Math.max(1, Math.round((2 * hx) / 2.8))) { pole(x, hz); pole(x, -hz); }
    for (let z = -hz; z <= hz + 0.01; z += (2 * hz) / Math.max(1, Math.round((2 * hz) / 2.8))) { pole(hx, z); pole(-hx, z); }
    for (let y = 1.2; y < HT; y += H) {
      for (const [w, d, x, z] of [[2 * hx, 0.12, 0, hz], [2 * hx, 0.12, 0, -hz], [0.12, 2 * hz, hx, 0], [0.12, 2 * hz, -hx, 0]]) {
        const r = new THREE.Mesh(new THREE.BoxGeometry(w, 0.12, d), poleM); r.position.set(x, y + 1.0, z); g.add(r);
      }
      for (const [w, d, x, z] of [[2 * hx, 0.7, 0, hz - 0.35], [2 * hx, 0.7, 0, -hz + 0.35]]) {
        const pl = new THREE.Mesh(new THREE.BoxGeometry(w, 0.1, d), plankM); pl.position.set(x, y - 0.1, z); pl.receiveShadow = true; g.add(pl);
      }
    }
    decor(sc);
  }

  /* ---- walkers: free teachers, around the courtyard ---- */
  function walker(p, colour, i) {
    const before = mats.length;
    const g = new THREE.Group(); scene.add(g);
    const body = new THREE.Group(); g.add(body);
    const torso = new THREE.Mesh(new THREE.CapsuleGeometry(0.42, 0.55, 6, 14), mat(theme.site ? '#ff8a1f' : colour)); torso.position.y = 0.85; torso.castShadow = true; body.add(torso);
    const head = new THREE.Mesh(new THREE.SphereGeometry(0.55, 22, 18), mat(T.skin)); head.position.y = 1.95; head.castShadow = true; body.add(head);
    for (const s of [-1, 1]) { const eye = new THREE.Mesh(new THREE.SphereGeometry(0.07, 8, 8), mat('#2b2b3a')); eye.position.set(s * 0.19, 1.95, 0.5); body.add(eye); }
    if (theme.site) {
      const hat = new THREE.Mesh(new THREE.SphereGeometry(0.62, 22, 16, 0, Math.PI * 2, 0, Math.PI * 0.5), mat(colour)); hat.position.y = 2.08; body.add(hat);
      const brim = new THREE.Mesh(new THREE.CylinderGeometry(0.78, 0.78, 0.07, 24), mat(colour)); brim.position.set(0, 2.1, 0.08); body.add(brim);
      const band = new THREE.Mesh(new THREE.TorusGeometry(0.43, 0.06, 8, 24), mat('#fff6d8', { emissive: '#fff6d8', emissiveIntensity: 0.3 })); band.rotation.x = Math.PI / 2; band.position.y = 0.95; body.add(band);
    } else {
      const hair = new THREE.Mesh(new THREE.SphereGeometry(0.58, 22, 18, 0, Math.PI * 2, 0, Math.PI * 0.55), mat(i % 3 === 1 ? '#2b2b3a' : T.hair)); hair.position.y = 2.0; hair.rotation.x = -0.25; body.add(hair);
      const book = rbox(0.5, 0.62, 0.14, mat('#ffffff'), 0.05); book.position.set(0.45, 0.95, 0.25); book.rotation.y = -0.5; body.add(book);
    }
    body.traverse((o) => { if (o.isMesh) { o.userData = { kind: 'teacher', id: p.id }; world.pickables.push(o); } });
    const c = world.court || { x0: -8, x1: 8, z0: -6, z1: 6 }, inset = 1 + (i % 3) * 0.6;
    const pts = [[c.x0 + inset, c.z0 + inset], [0, c.z0 + inset], [c.x1 - inset, c.z0 + inset], [c.x1 - inset, 0],
      [c.x1 - inset, c.z1 - inset], [0, c.z1 - inset], [c.x0 + inset, c.z1 - inset], [c.x0 + inset, 0]].map(([x, z]) => new THREE.Vector3(x, 0.12, z));
    if (i % 2) pts.reverse();
    return { id: p.id, group: g, body, mats: mats.slice(before), curve: new THREE.CatmullRomCurve3(pts, true, 'catmullrom', 0.3), u: rnd(), speed: 0.006 + rnd() * 0.006 };
  }

  /* ---- what changes with the period ---- */
  function setOccupancy(byRoom, colourOf) {
    for (const [id, rec] of Object.entries(world ? world.rooms : {})) {
      const lesson = byRoom[id];
      if (rec.field) {                                         // a pitch keeps its paint; a lesson warms it
        const key = lesson ? lesson.event : '';
        if (rec.key !== key) { rec.key = key; rec.mat.emissive.set(lesson ? colourOf(lesson.colour) : '#000000'); rec.mat.emissiveIntensity = lesson ? 0.18 : 0; dirty = true; }
        continue;
      }
      const want = lesson ? colourOf(lesson.colour) : (rec.home ? T.home : T.winOff);
      const key = (lesson ? lesson.event : '') + want;
      if (rec.key === key) continue;
      rec.key = key;
      const from = rec.mat.color.clone(), to = new THREE.Color(want);
      const eFrom = rec.mat.emissive.clone(), eTo = new THREE.Color(lesson ? want : '#000000');
      tween({ dur: 0.35, update: (k) => { rec.mat.color.lerpColors(from, to, k); rec.mat.emissive.lerpColors(eFrom, eTo, k); rec.mat.emissiveIntensity = T.emissive; } });
      if (lesson) tween({ dur: 0.45, ease: ease.lin, update: (k) => { const s = 1 + Math.sin(k * Math.PI) * 0.18; rec.mesh.scale.set(s, s, s); } });
    }
  }
  function setPins(pins) {
    for (const o of world ? world.pins : []) { o.element.remove(); o.parent && o.parent.remove(o); }
    if (!world) return;
    world.pins = [];
    for (const p of pins) {
      const rec = world.rooms[p.roomId]; if (!rec) continue;
      const el = document.createElement('div'); el.className = 'pin';
      const head = document.createElement('div'); head.className = 'p'; head.appendChild(document.createElement('i'));
      const lbl = document.createElement('div'); lbl.className = 'lbl'; lbl.textContent = p.text;
      el.append(head, lbl);
      const o = new CSS2DObject(el); o.position.copy(rec.anchor).add(new THREE.Vector3(0, 1.4, 0)); scene.add(o); world.pins.push(o);
    }
    dirty = true;
  }
  function setWalkers(people, colourOfPerson) {
    if (!world) return;
    const keep = new Set(people.map((p) => p.id));
    world.walkers = world.walkers.filter((w) => {
      if (keep.has(w.id)) return true;
      w.group.traverse((o) => { if (o.geometry) o.geometry.dispose(); });
      scene.remove(w.group);
      for (const m of w.mats) { m.dispose(); const k = mats.indexOf(m); if (k >= 0) mats.splice(k, 1); }
      world.pickables = world.pickables.filter((m) => !(m.userData && m.userData.kind === 'teacher' && m.userData.id === w.id));
      return false;
    });
    const have = new Set(world.walkers.map((w) => w.id));
    people.forEach((p, i) => {
      if (have.has(p.id)) return;
      const w = walker(p, colourOfPerson(p), i);
      w.group.scale.setScalar(0.001);
      tween({ dur: 0.5, ease: ease.back, update: (k) => w.group.scale.setScalar(Math.max(0.001, k)) });
      world.walkers.push(w);
    });
    dirty = true;
  }
  function highlight(roomId) {
    if (world && world.mark) { world.mark.element.remove(); scene.remove(world.mark); world.mark = null; }
    const rec = roomId && world && world.rooms[roomId];
    if (!rec) { dirty = true; return; }
    const el = document.createElement('div'); el.className = 'campus-mark'; el.textContent = '▼';
    world.mark = new CSS2DObject(el); world.mark.position.copy(rec.anchor).add(new THREE.Vector3(0, 2.4, 0)); scene.add(world.mark);
    dirty = true;
  }
  function focusRoom(roomId) {
    const rec = roomId && world && world.rooms[roomId]; if (!rec) return;
    const from = controls.target.clone(), to = new THREE.Vector3(rec.anchor.x, 2, rec.anchor.z);
    const off = camera.position.clone().sub(controls.target);
    tween({ dur: 0.7, ease: ease.inout, update: (k) => { controls.target.lerpVectors(from, to, k); camera.position.copy(controls.target).add(off); } });
  }

  /* ---- picking: a click, not a drag ---- */
  const ray = new THREE.Raycaster(), ptr = new THREE.Vector2(); let down = null;
  renderer.domElement.addEventListener('pointerdown', (e) => { down = [e.clientX, e.clientY]; });
  renderer.domElement.addEventListener('pointerup', (e) => {
    if (!down || Math.hypot(e.clientX - down[0], e.clientY - down[1]) > 6 || !world || !picker) return;
    const r = renderer.domElement.getBoundingClientRect();
    ptr.set(((e.clientX - r.left) / r.width) * 2 - 1, -((e.clientY - r.top) / r.height) * 2 + 1);
    ray.setFromCamera(ptr, camera);
    const hit = ray.intersectObjects(scene.children, true).find((h) => h.object.isMesh && h.object.visible && !h.object.userData.nopick);
    const u = hit && hit.object.userData;
    if (u && u.id && (u.kind === 'room' || u.kind === 'teacher')) picker({ kind: u.kind, id: u.id });
  });

  /* ---- the loop: on demand; walkers and tweens keep it going, every other frame when only walkers move ---- */
  function loop(now) {
    raf = active ? requestAnimationFrame(loop) : 0;
    if (!host.clientWidth || !host.clientHeight) { last = now; return; }     // out of sight: nothing drawn
    const dt = Math.min((now - (last || now)) / 1000, 0.1); last = now;
    budget(dt);
    sizeTo(false);
    const busy = tweens.length > 0;
    stepTweens(dt);
    if (controls.update()) dirty = true;
    const walking = world && world.walkers.length > 0;
    if (world) {
      for (const w of world.walkers) {
        w.u = (w.u + w.speed * dt) % 1;
        const p = w.curve.getPointAt(w.u), q = w.curve.getPointAt((w.u + 0.002) % 1);
        w.group.position.copy(p); w.group.lookAt(q.x, p.y, q.z);
        w.body.position.y = Math.abs(Math.sin(now / 1000 * 9 + w.u * 40)) * 0.18;
      }
      for (const c of world.clouds) { c.position.x += dt * 0.8; if (c.position.x > world.grounds.w / 2 + 10) c.position.x = -world.grounds.w / 2 - 10; }
      if (world.jib) { world.jib.rotation.y = 0.9 + Math.sin(now / 1000 * 0.45) * 0.75; world.trolley.position.x = 12 + Math.sin(now / 1000 * 0.8) * 3; }
    }
    frame++;
    const animate = walking || (world && (world.clouds.length || world.jib));
    if (dirty || busy || (animate && frame % 2 === 0)) {
      dirty = false;
      if (scene) { renderer.render(scene, camera); labels.render(scene, camera); }
    }
  }

  return {
    build(doc, look, finished) {
      const prev = world && world.grounds, g = doc.grounds || {};
      build(doc, look, finished);
      if (!prev || prev.w !== g.w || prev.d !== g.d) fitted = false;     // keep the user's view when the school is the same size
      sizeTo(true);
    },
    setOccupancy, setPins, setWalkers, highlight, focusRoom,
    onPick(fn) { picker = fn; },
    roomAnchor(id) { return world && world.rooms[id] ? world.rooms[id].anchor : null; },
    resize() { sizeTo(true); },
    setActive(on) {
      active = !!on;
      if (active && !raf) { last = 0; dirty = true; raf = requestAnimationFrame(loop); }
      if (!active && raf) { cancelAnimationFrame(raf); raf = 0; }
    },
    applyQuality,
    dispose() { this.setActive(false); clear(); controls.dispose(); renderer.dispose(); renderer.forceContextLoss(); renderer.domElement.remove(); labels.domElement.remove(); },
  };
}
