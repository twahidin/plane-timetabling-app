/* The campus loader (spec 2026-10-11-campus-looks-design.md §2.1, §2.5). Classic: the planes view only,
   and nothing of the campus is fetched. Toy town / Construction: a Campus · Planes switch (this browser's
   choice in localStorage 'plane.view3d'), the campus modules imported on first use, kept in step with the
   page (resize, selection, new data, look). If the campus cannot run here, back to planes with a note. */
(function () {
  const KEY = 'plane.view3d';
  const root = document.documentElement;
  const $ = (id) => document.getElementById(id);
  let ctl = null, loading = null, broken = false, imported = false, pending = null;

  const look = () => root.dataset.look || 'classic';
  function chosen() {
    if (look() === 'classic' || broken) return 'planes';
    try { return localStorage.getItem(KEY) === 'planes' ? 'planes' : 'campus'; } catch (e) { return 'campus'; }
  }
  function remember(mode) { try { localStorage.setItem(KEY, mode); } catch (e) { /* this page only */ } }
  function note(text) { const n = $('campus-note'); if (n) { n.textContent = text; n.hidden = !text; } }

  function ensure() {
    if (ctl) return Promise.resolve(ctl);
    if (!loading) {
      loading = import('campus/main')
        .then((m) => { imported = true; return m.mount($('campus-view'), { look: look(), select: (id) => window.selectEvent && window.selectEvent(id) }); })
        .then((c) => (ctl = c));
    }
    return loading;
  }

  function apply() {
    const mv = $('model-view'), sw = $('campus-switch'), cv = $('campus-view');
    if (!mv || !sw || !cv) return;
    const mode = chosen();
    sw.hidden = look() === 'classic' || broken;
    sw.querySelectorAll('button').forEach((b) => {
      b.classList.toggle('on', b.dataset.mode3d === mode);
      b.setAttribute('aria-pressed', String(b.dataset.mode3d === mode));
    });
    mv.classList.toggle('campus-on', mode === 'campus');
    cv.hidden = mode !== 'campus';
    if (mode === 'campus') {
      ensure().then((c) => {
        if (chosen() !== 'campus') return;                    // the user moved on while it loaded
        note(''); c.setLook(look()); c.setActive(!document.hidden); c.resize();
        if (pending) { c.highlightEvent(pending); pending = null; }
      }).catch((err) => {
        console.warn('campus unavailable', err);
        loading = null;
        // the browser cannot run it (no import maps, no WebGL): planes for the rest of this page;
        // anything else (the data did not load): planes for now, Campus retries when chosen again
        if (!imported || /webgl/i.test(String(err && err.message))) {
          broken = true;
          note('The campus view could not start in this browser, so the planes view is shown.');
        } else note('The campus could not load just now, so the planes view is shown. Choose Campus to try again.');
        mv.classList.remove('campus-on'); cv.hidden = true;
        if (broken) apply();
        else if (window.resizeModel) requestAnimationFrame(() => window.resizeModel());
      });
    } else if (ctl) ctl.setActive(false);
    if (window.resizeModel) requestAnimationFrame(() => window.resizeModel());
  }

  const r0 = window.resizeModel;
  window.resizeModel = function () { if (r0) r0(); if (ctl) ctl.resize(); };
  const g0 = window.gridHighlight;
  window.gridHighlight = function (id) { if (g0) g0(id); if (ctl) ctl.highlightEvent(id || null); else pending = id || null; };

  document.addEventListener('DOMContentLoaded', () => {
    const sw = $('campus-switch');
    if (sw) sw.addEventListener('click', (e) => {
      const b = e.target.closest('button[data-mode3d]');
      if (!b) return;
      remember(b.dataset.mode3d); apply();
    });
    apply();
  });
  document.addEventListener('plane:look', apply);
  document.addEventListener('plane:solid', () => { if (ctl) ctl.reload().catch(() => {}); });
  document.addEventListener('visibilitychange', () => { if (ctl) ctl.setActive(!document.hidden && chosen() === 'campus'); });
})();
