/* The look (spec docs/superpowers/specs/2026-10-11-campus-looks-design.md §1.1). The server renders the
   school's look into <html data-look data-school-look>; this browser's own choice lives in localStorage
   'plane.look' (absent = follow the school). The menu lists the three looks once, the school's marked
   "(school default)"; choosing that one clears this browser's choice, so it follows the school again.
   When storage is blocked the choice lasts for this page only. */
(function () {
  const VALID = new Set(['classic', 'toy', 'site']);
  const NAMES = { classic: 'Classic', toy: 'Toy town', site: 'Construction' };
  const KEY = 'plane.look';
  const root = document.documentElement;
  let session = null;                      // the choice when localStorage throws

  const school = () => (VALID.has(root.dataset.schoolLook) ? root.dataset.schoolLook : 'classic');
  function own() {
    try { const v = localStorage.getItem(KEY); return VALID.has(v) ? v : session; } catch (e) { return session; }
  }
  function apply() {
    const look = own() || school();
    if (root.dataset.look !== look) root.dataset.look = look;
    document.querySelectorAll('select.look-pick').forEach((s) => {
      s.querySelectorAll('option').forEach((o) => {
        if (NAMES[o.value]) o.textContent = NAMES[o.value] + (o.value === school() ? ' (school default)' : '');
      });
      s.value = look;
    });
    document.dispatchEvent(new CustomEvent('plane:look', { detail: { look } }));
  }
  function choose(v) {
    try {
      if (VALID.has(v) && v !== school()) localStorage.setItem(KEY, v); else localStorage.removeItem(KEY);
      session = null;
    } catch (e) { session = VALID.has(v) && v !== school() ? v : null; }
    apply();
  }
  window.planeLook = { current: () => root.dataset.look, choose, refresh: apply, LOOKS: Object.keys(NAMES) };
  document.addEventListener('DOMContentLoaded', () => {
    document.querySelectorAll('select.look-pick').forEach((s) => s.addEventListener('change', () => choose(s.value)));
    apply();
  });
  window.addEventListener('storage', (e) => { if (e.key === KEY || e.key === null) apply(); });

  // animations (spec §3.4): Full, Reduced or Off, per browser; the default follows the device's reduce-motion setting
  const MOTIONS = new Set(['full', 'reduced', 'off']), MKEY = 'plane.motion';
  let motionSession = null;
  function motion() {
    try { const v = localStorage.getItem(MKEY); if (MOTIONS.has(v)) return v; } catch (e) { if (motionSession) return motionSession; }
    return window.matchMedia && window.matchMedia('(prefers-reduced-motion: reduce)').matches ? 'reduced' : 'full';
  }
  function setMotion(v) {
    if (!MOTIONS.has(v)) return;
    try { localStorage.setItem(MKEY, v); } catch (e) { motionSession = v; }
    document.querySelectorAll('select.motion-pick').forEach((s) => { s.value = v; });
    document.dispatchEvent(new CustomEvent('plane:motion', { detail: { level: v } }));
  }
  window.planeMotion = { level: motion, set: setMotion };
  document.addEventListener('DOMContentLoaded', () => {
    document.querySelectorAll('select.motion-pick').forEach((s) => { s.value = motion(); s.addEventListener('change', () => setMotion(s.value)); });
  });
})();
