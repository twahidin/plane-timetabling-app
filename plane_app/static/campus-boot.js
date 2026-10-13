/* The campus loader (spec 2026-10-11-campus-looks-design.md §2.1, §2.5). Classic: the planes view only,
   and nothing of the campus is fetched. Toy town / Construction: a Campus · Planes switch (this browser's
   choice in localStorage 'plane.view3d'), the campus modules imported on first use, kept in step with the
   page (resize, selection, new data, look). If the campus cannot run here, back to planes with a note.
   The setup tabs (spec 2026-10-13-setup-campus §4): outside Classic the campus always shows there (no
   switch: the planes need a timetable), in that tab's mode — ctl.setMode(tab), or 'build' elsewhere. */
(function () {
  const KEY = 'plane.view3d';
  const root = document.documentElement;
  const $ = (id) => document.getElementById(id);
  let ctl = null, loading = null, broken = false, imported = false, pending = null, held = [], toastTimer = 0, tab = null;
  const SETUP = new Set(['project', 'manpower', 'locations', 'requirements', 'constraints']);
  const onSetup = () => SETUP.has(tab);
  const modeNow = () => (onSetup() ? tab : 'build');

  const look = () => root.dataset.look || 'classic';
  function chosen() {
    if (look() === 'classic' || broken) return 'planes';
    if (onSetup()) return 'campus';
    try { return localStorage.getItem(KEY) === 'planes' ? 'planes' : 'campus'; } catch (e) { return 'campus'; }
  }
  function remember(mode) { try { localStorage.setItem(KEY, mode); } catch (e) { /* this page only */ } }
  function note(text) { const n = $('campus-note'); if (n) { n.textContent = text; n.hidden = !text; } }

  function ensure() {
    if (ctl) return Promise.resolve(ctl);
    if (!loading) {
      loading = import('campus/main')
        .then((m) => { imported = true; return m.mount($('campus-view'), { look: look(), mode: modeNow(), select: (id) => window.selectEvent && window.selectEvent(id), hold: (d) => hold(d) }); })
        .then((c) => (ctl = c));
    }
    return loading;
  }

  function apply() {
    const mv = $('model-view'), sw = $('campus-switch'), cv = $('campus-view');
    if (!mv || !sw || !cv) return;
    const mode = chosen();
    sw.hidden = look() === 'classic' || broken || onSetup();
    mv.classList.toggle('setup-on', onSetup() && look() !== 'classic');
    sw.querySelectorAll('button').forEach((b) => {
      b.classList.toggle('on', b.dataset.mode3d === mode);
      b.setAttribute('aria-pressed', String(b.dataset.mode3d === mode));
    });
    mv.classList.toggle('campus-on', mode === 'campus');
    cv.hidden = mode !== 'campus';
    if (mode === 'campus') {
      ensure().then((c) => {
        if (chosen() !== 'campus') return;                    // the user moved on while it loaded
        note(''); c.setLook(look()); c.setActive(!document.hidden); c.setMode(modeNow()); c.resize();
        if (pending) { c.highlightEvent(pending); pending = null; }
        flush();
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
    } else if (ctl) { ctl.setMode(modeNow()); ctl.setActive(false); }
    if (window.resizeModel) requestAnimationFrame(() => window.resizeModel());
  }

  const r0 = window.resizeModel;
  window.resizeModel = function () { if (r0) r0(); if (ctl) ctl.resize(); };
  const g0 = window.gridHighlight;
  window.gridHighlight = function (id) { if (g0) g0(id); if (ctl) ctl.highlightEvent(id || null); else pending = id || null; };

  let ready = false;                                            // the look is final only once the page has loaded
  document.addEventListener('DOMContentLoaded', () => {
    ready = true;
    const sw = $('campus-switch');
    if (sw) sw.addEventListener('click', (e) => {
      const b = e.target.closest('button[data-mode3d]');
      if (!b) return;
      remember(b.dataset.mode3d); apply();
    });
    apply();
  });
  /* The journey on the campus (spec §3.2): play each step when the campus is on screen; otherwise hold the
     latest of each step (one per stage, in the order they last came) and offer to show it. */
  const onScreen = () => !onSetup() && chosen() === 'campus' && ctl && $('campus-view') && $('campus-view').clientWidth > 0 && !document.hidden;
  function hold(d) { held = held.filter((h) => h.stage !== d.stage); held.push(d); }
  function flush() {
    if (!held.length || !onScreen()) return;
    const list = held; held = []; hideToast();
    list.forEach((d) => ctl.play(d));
  }
  function hideToast() { const t = $('journey-toast'); if (t) t.hidden = true; clearTimeout(toastTimer); }
  function toast() {
    let t = $('journey-toast');
    if (!t) {
      t = document.createElement('div'); t.id = 'journey-toast'; t.className = 'journey-toast'; t.setAttribute('role', 'status');
      const b = document.createElement('button'); b.type = 'button'; b.className = 'btn'; b.textContent = 'Watch it on the campus ▶';
      b.addEventListener('click', () => { hideToast(); remember('campus'); if (window.showTab) window.showTab('build'); apply(); });
      t.appendChild(b); document.body.appendChild(t);
    }
    t.hidden = false; clearTimeout(toastTimer); toastTimer = setTimeout(hideToast, 8000);
  }
  document.addEventListener('plane:journey', (e) => {
    const d = e.detail || {};
    if (!d.stage || look() === 'classic' || broken) return;
    if (onScreen()) { ctl.play(d); return; }
    hold(d);
    // nothing to watch with animations Off: the step is still held, so the campus refreshes when it shows
    const off = window.planeMotion && window.planeMotion.level() === 'off';
    if (d.stage !== 'solving' && !off) toast();
  });
  document.addEventListener('plane:tab', (e) => {
    const was = modeNow();
    tab = (e.detail && e.detail.key) || null;
    if (!ready || look() === 'classic') return;                // Classic: nothing of the campus, on any tab
    if (was !== modeNow() || onSetup()) apply();               // campus on a setup tab, or the user's choice back
    requestAnimationFrame(flush);
  });
  let shownTimetable = null;
  document.addEventListener('plane:journey-state', (e) => {      // another timetable: its held steps belong to the old one
    const id = e.detail && e.detail.timetable;
    if (shownTimetable && id && id !== shownTimetable) { held = []; hideToast(); }
    if (id) shownTimetable = id;
  });

  document.addEventListener('plane:look', () => { if (look() === 'classic') { held = []; hideToast(); } apply(); });
  document.addEventListener('plane:solid', () => { if (ctl) ctl.reload().catch(() => {}); });
  document.addEventListener('visibilitychange', () => { if (ctl) ctl.setActive(!document.hidden && chosen() === 'campus'); flush(); });
})();
