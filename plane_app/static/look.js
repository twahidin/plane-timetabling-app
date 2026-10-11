/* The look (spec docs/superpowers/specs/2026-10-11-campus-looks-design.md §1.1). The server renders the
   school's look into <html data-look data-school-look>; this browser's own choice lives in localStorage
   'plane.look' (absent = follow the school). When storage is blocked the choice lasts for this page only. */
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
      const first = s.querySelector('option[value="school"]');
      if (first) first.textContent = 'School default (' + NAMES[school()] + ')';
      s.value = own() || 'school';
    });
    document.dispatchEvent(new CustomEvent('plane:look', { detail: { look } }));
  }
  function choose(v) {
    try {
      if (VALID.has(v)) localStorage.setItem(KEY, v); else localStorage.removeItem(KEY);
      session = null;
    } catch (e) { session = VALID.has(v) ? v : null; }
    apply();
  }
  window.planeLook = { current: () => root.dataset.look, choose, refresh: apply, LOOKS: Object.keys(NAMES) };
  document.addEventListener('DOMContentLoaded', () => {
    document.querySelectorAll('select.look-pick').forEach((s) => s.addEventListener('change', () => choose(s.value)));
    apply();
  });
  window.addEventListener('storage', (e) => { if (e.key === KEY || e.key === null) apply(); });
})();
