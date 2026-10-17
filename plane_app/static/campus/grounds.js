/* The ground in the campus's courtyard matches the timetable's type (brief 2026-10-14-grounds §2), from the
   approved mockup (docs/design/2026-10-13-setup-campus/grounds-mockup.html, build() per kind):
     education  the sports field, as before (scene.js's own fieldTexture; no pieces);
     health     a garden: a lawn, four flower-bed planters, two benches and a helipad (white ring and H) at one end;
     business   a paved plaza with a grid of joints, a fountain (basin, water, jet) and four planters with trees;
     sports     a running track (an oval with lane lines) round a pitch (centre line and circle), two tennis courts beside.
   areaLook says which an area draws: only the courtyard's room-less area takes the kind (rooms keep their name).
   Pure and Three-free (Node-testable): scene.js paints the flat markings with paintGround on a canvas laid over the
   area and builds each of groundPieces' pieces as a mesh. Everything is laid out in the area's own frame (the area
   centred on 0, `w` along x, `d` along z) and scaled to it, so nothing leaves the area place() gave it. Colours come
   by role from PALETTE: Construction draws the site's, and the clean-check handover turns them into Toy town's. */

export const KINDS = ['education', 'health', 'business', 'sports'];
const LABELS = { education: 'Field', health: 'Garden', business: 'Plaza', sports: 'Track' };
/* the Ground menu's names (ground-menu.js), each ending in its LABELS word */
export const GROUND_NAMES = { education: 'School field', health: 'Hospital garden', business: 'Business plaza', sports: 'Sports track' };
export const SHAPES = ['box', 'cyl', 'sphere', 'ico'];
const ROLES_FIXED = { planter: '#c08a5c', bench: '#b07a52', benchLeg: '#8a5a3a', pad: '#d8d4cc', basin: '#d9d2c4', pot: '#c9a27e', leaves: '#6fcf6f', net: '#ffffff' };
export const PALETTE = {
  toy: Object.assign({ field: '#7fd06a', line: '#ffffff', lawn: '#9ad97c', paving: '#efe6d8', paveLine: '#e0d4c2', track: '#e8836a',
                       court: '#5aa9ff', water: '#7fd3ff', bed: '#ff9bb8', bed2: '#ffd35a' }, ROLES_FIXED),
  site: Object.assign({ field: '#dcb37c', line: '#fff8ea', lawn: '#e9c88f', paving: '#d9cdb8', paveLine: '#c8b9a0', track: '#c99a72',
                        court: '#b9a98f', water: '#a9c4cf', bed: '#c9a07a', bed2: '#d7b98a' }, ROLES_FIXED),
};
const TOP = 0.16;                                    // the area's slab top: pieces stand on it

/* The document's ground kind: one of KINDS, anything else the field. */
export function groundKind(doc) {
  const k = doc && typeof doc === 'object' ? doc.ground : null;
  return KINDS.includes(k) ? k : 'education';
}
export function groundLabel(kind) { return LABELS[kind] || LABELS.education; }

/* What one area draws and its label. Only the courtyard's own area (no room) takes the kind: a garden, plaza or
   track (education's stays the unlabelled field it always was). An area that is one of the timetable's rooms keeps
   the room's name and today's field, except that in a sports timetable a room named like a court is a plain
   court. draw: education (the field) | health | business | sports | court. */
const COURT = /\bcourts?\b/i;
export function areaLook(kind, area) {
  const a = area && typeof area === 'object' ? area : {};
  const k = KINDS.includes(kind) ? kind : 'education';
  if (!a.room) return { draw: k, label: k === 'education' ? null : groundLabel(k) };
  const name = String(a.name || a.room);
  return { draw: k === 'sports' && COURT.test(name) ? 'court' : 'education', label: name };
}

/* ---- layouts shared by the paint (in canvas pixels) and the pieces (in metres): linear in w and d ---- */
function healthLayout(w, d) {
  const p = Math.min(0.3 * w, 0.6 * d), padX = w / 2 - p / 2 - 0.04 * w;   // the helipad's square, at the +x end
  const gx0 = -w / 2 + 0.04 * w, gx1 = padX - p / 2 - 0.04 * w;            // the garden, the rest
  return { p, padX, gx: (gx0 + gx1) / 2, gw: gx1 - gx0 };
}
function courtLayout(w, d) {                         // one court filling a court room's area, long along x
  const L = 0.86 * w, W = Math.min(L / 2.1, 0.8 * d);
  return { L, W };
}
function sportsLayout(w, d) {
  const m = 0.03 * Math.min(w, d), cw = 0.3 * w;                           // the courts' strip at the +x end
  const L = cw - 2 * m, Wc = Math.min(L / 2.1, d / 2 - 2 * m);             // a court: L along x, Wc along z
  return { m, cw, court: { x: w / 2 - cw / 2, L, W: Wc, zs: [-d / 4, d / 4] },
           track: { x: -cw / 2, A: (w - cw) / 2 - m, B: d / 2 - m } };
}

/* The 3D pieces of a kind on an area {w, d}: [{part, shape, role, x, y, z, …size}] in the area's frame, y the
   centre's height; box {w, h, d}, cyl {r (bottom, the wider), rt, h}, sphere and ico {r}; opacity optional. */
export function groundPieces(kind, area) {
  const w = Math.max(0.5, Number(area && area.w) || 16), d = Math.max(0.5, Number(area && area.d) || 11);
  const s = Math.min(1.5, Math.max(0.3, Math.min(w / 16, d / 11)));       // heights and small sizes follow the area
  const out = [];
  const box = (part, role, x, y, z, bw, bh, bd) => out.push({ part, shape: 'box', role, x, y, z, w: bw, h: bh, d: bd });
  if (kind === 'health') {
    const { gx, gw } = healthLayout(w, d), bw = 0.36 * gw, bd = Math.min(0.2 * d, 0.6 * bw), ph = 0.45 * s;
    for (const dx of [-0.25 * gw, 0.25 * gw]) for (const z of [-0.3 * d, 0.3 * d]) {
      const x = gx + dx;
      box('planter', 'planter', x, TOP + ph / 2, z, bw, ph, bd);
      const r = Math.min(0.16 * bd, bw / 12);
      for (let k = 0; k < 5; k++) out.push({ part: 'flower', shape: 'sphere', role: k % 2 ? 'bed' : 'bed2', x: x - bw / 2 + (bw * (k + 0.5)) / 5, y: TOP + ph + r * 0.6, z, r });
    }
    const bl = 0.22 * gw, bdp = Math.min(0.5 * s, 0.3 * d), lh = 0.4 * s;
    for (const dx of [-0.25 * gw, 0.25 * gw]) {
      const x = gx + dx;
      box('bench', 'bench', x, TOP + lh + 0.06 * s, 0, bl, 0.12 * s, bdp);
      for (const e of [-1, 1]) box('bench-leg', 'benchLeg', x + e * (bl / 2 - 0.08 * s), TOP + lh / 2, 0, 0.12 * s, lh, bdp);
    }
  } else if (kind === 'business') {
    const rb = 0.22 * Math.min(w, d), bh = 0.8 * s, jh = 2.4 * s;
    out.push({ part: 'basin', shape: 'cyl', role: 'basin', x: 0, y: TOP + bh / 2, z: 0, r: rb, rt: 0.95 * rb, h: bh });
    out.push({ part: 'water', shape: 'cyl', role: 'water', x: 0, y: TOP + bh, z: 0, r: 0.88 * rb, rt: 0.88 * rb, h: 0.16 * s });
    out.push({ part: 'jet', shape: 'cyl', role: 'water', x: 0, y: TOP + bh + jh / 2, z: 0, r: 0.15 * rb, rt: 0.09 * rb, h: jh, opacity: 0.75 });
    const pp = Math.min(0.09 * w, 0.14 * d), ph = 0.9 * s;
    for (const ex of [-1, 1]) for (const ez of [-1, 1]) {
      const x = ex * (w / 2 - 0.06 * w - pp / 2), z = ez * (d / 2 - 0.06 * d - pp / 2);
      box('planter', 'pot', x, TOP + ph / 2, z, pp, ph, pp);
      out.push({ part: 'tree', shape: 'ico', role: 'leaves', x, y: TOP + ph + 0.5 * pp, z, r: 0.55 * pp });
    }
  } else if (kind === 'court') {
    const c = courtLayout(w, d), nh = 0.45 * s;
    box('net', 'net', 0, TOP + nh / 2, 0, Math.min(0.06 * s, c.L / 4), nh, c.W);
  } else if (kind === 'sports') {
    const { court } = sportsLayout(w, d), nh = 0.45 * s;
    for (const z of court.zs) box('net', 'net', court.x, TOP + nh / 2, z, Math.min(0.06 * s, court.L / 4), nh, court.W);
  }
  return out;
}

/* The flat markings of a kind on a W × H canvas laid over the whole area (W along x, H along z, the same
   proportions as the area). P is a PALETTE; bare (Construction before the check) adds the site's tyre tracks. */
export function paintGround(ctx, kind, W, H, P, bare) {
  const X = (u) => u + W / 2, Z = (v) => v + H / 2;                      // the area's frame → canvas pixels
  const rect = (role, u, v, w, h) => { ctx.fillStyle = P[role]; ctx.fillRect(X(u - w / 2), Z(v - h / 2), w, h); };
  const tracks = () => {
    if (!bare) return;
    ctx.strokeStyle = 'rgba(120,80,40,.16)'; ctx.lineWidth = Math.max(4, W / 50);
    for (let i = 0; i < 4; i++) {
      const x0 = (W * (i + 0.5)) / 4;
      ctx.beginPath(); ctx.moveTo(x0, H); ctx.bezierCurveTo(Math.min(W, x0 + W / 8), H * 0.65, Math.max(0, x0 - W / 12), H * 0.3, Math.min(W, x0 + W / 10), 0); ctx.stroke();
    }
  };
  if (kind === 'health') {
    ctx.fillStyle = P.lawn; ctx.fillRect(0, 0, W, H);
    for (let i = 0; i < 8; i++) { ctx.fillStyle = i % 2 ? 'rgba(255,255,255,.07)' : 'rgba(0,0,0,.03)'; ctx.fillRect((i * W) / 8, 0, W / 8, H); }
    tracks();
    const { p, padX } = healthLayout(W, H);
    rect('pad', padX, 0, p, p);
    ctx.strokeStyle = P.line; ctx.lineWidth = Math.max(2, 0.06 * p);
    ctx.beginPath(); ctx.arc(X(padX), Z(0), 0.37 * p, 0, Math.PI * 2); ctx.stroke();
    for (const e of [-1, 1]) rect('line', padX + e * 0.13 * p, 0, 0.07 * p, 0.42 * p);
    rect('line', padX, 0, 0.26 * p, 0.07 * p);
  } else if (kind === 'business') {
    ctx.fillStyle = P.paving; ctx.fillRect(0, 0, W, H);
    tracks();
    const j = W / 11;
    ctx.strokeStyle = P.paveLine; ctx.lineWidth = Math.max(1.5, W / 220);
    for (let x = j; x < W; x += j) { ctx.beginPath(); ctx.moveTo(x, 0); ctx.lineTo(x, H); ctx.stroke(); }
    for (let y = j; y < H; y += j) { ctx.beginPath(); ctx.moveTo(0, y); ctx.lineTo(W, y); ctx.stroke(); }
  } else if (kind === 'court') {
    ctx.fillStyle = P.lawn; ctx.fillRect(0, 0, W, H);
    tracks();
    const c = courtLayout(W, H);
    rect('court', 0, 0, c.L, c.W);
    ctx.strokeStyle = P.line; ctx.lineWidth = Math.max(1.5, W / 200);
    ctx.strokeRect(X(-c.L / 2), Z(-c.W / 2), c.L, c.W);
    ctx.beginPath(); ctx.moveTo(X(-c.L / 2), Z(0)); ctx.lineTo(X(c.L / 2), Z(0)); ctx.stroke();
    ctx.beginPath(); ctx.moveTo(X(0), Z(-c.W / 2)); ctx.lineTo(X(0), Z(c.W / 2)); ctx.stroke();   // the net
  } else if (kind === 'sports') {
    ctx.fillStyle = P.lawn; ctx.fillRect(0, 0, W, H);
    tracks();
    const { court, track: t } = sportsLayout(W, H), lane = 0.24 * Math.min(t.A, t.B);
    const oval = (a, b) => { ctx.beginPath(); ctx.ellipse(X(t.x), Z(0), Math.max(1, a), Math.max(1, b), 0, 0, Math.PI * 2); };
    ctx.fillStyle = P.track; oval(t.A, t.B); ctx.fill();
    ctx.fillStyle = P.field; oval(t.A - lane, t.B - lane); ctx.fill();
    ctx.strokeStyle = P.line; ctx.lineWidth = Math.max(1, W / 300);
    for (let k = 1; k < 4; k++) { oval(t.A - (lane * k) / 4, t.B - (lane * k) / 4); ctx.stroke(); }
    const hx = ((t.A - lane) / Math.SQRT2) * 0.9, hz = ((t.B - lane) / Math.SQRT2) * 0.9;   // the pitch, inside the oval
    ctx.lineWidth = Math.max(1.5, W / 160);
    ctx.strokeRect(X(t.x - hx), Z(-hz), 2 * hx, 2 * hz);
    ctx.beginPath(); ctx.moveTo(X(t.x), Z(-hz)); ctx.lineTo(X(t.x), Z(hz)); ctx.stroke();
    ctx.beginPath(); ctx.arc(X(t.x), Z(0), 0.3 * Math.min(hx, hz), 0, Math.PI * 2); ctx.stroke();
    for (const v of court.zs) {
      rect('court', court.x, v, court.L, court.W);
      ctx.strokeStyle = P.line; ctx.lineWidth = Math.max(1, W / 260);
      ctx.strokeRect(X(court.x - court.L / 2), Z(v - court.W / 2), court.L, court.W);
      ctx.beginPath(); ctx.moveTo(X(court.x - court.L / 2), Z(v)); ctx.lineTo(X(court.x + court.L / 2), Z(v)); ctx.stroke();
      ctx.beginPath(); ctx.moveTo(X(court.x), Z(v - court.W / 2)); ctx.lineTo(X(court.x), Z(v + court.W / 2)); ctx.stroke();   // the net
    }
  }
}
