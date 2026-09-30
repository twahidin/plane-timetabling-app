(function () {
  'use strict';
  // The shell (spec docs/superpowers/specs/2026-09-29-nocturne-redesign-design.md §2): the numbered tabs,
  // the Assistant panel, the 3D view's dock and the full-screen views. Every other script keeps its own
  // elements; this one only shows, hides and moves them.
  const el = (id) => document.getElementById(id);
  const store = {
    get: (k) => { try { return localStorage.getItem(k); } catch (e) { return null; } },
    set: (k, v) => { try { localStorage.setItem(k, v); } catch (e) { /* private mode */ } },
  };
  const TABS = ['project', 'manpower', 'locations', 'requirements', 'constraints', 'build', 'consolidation',
    'summary', 'relief', 'custom'];
  const SETUP = TABS.slice(0, 7);                                   // tabs 02–08
  const DOCKS = { build: 'dock-build', consolidation: 'dock-consolidation', summary: 'dock-summary' };
  let current = null;
  let userMoved = false;                                            // a tab chosen before the first load settles

  const resize = () => { if (window.resizeModel) requestAnimationFrame(() => window.resizeModel()); };

  // ---------- tabs ----------
  function showTab(key) {
    if (!TABS.includes(key)) key = 'project';
    document.querySelectorAll('.expanded').forEach((x) => expand(x.id, false));   // never strand a full-screen view
    current = key;
    TABS.forEach((k) => {
      const panel = el('tab-' + k), btn = el('tabbtn-' + k);
      if (panel) panel.hidden = k !== key;
      if (btn) {
        btn.classList.toggle('on', k === key);
        btn.setAttribute('aria-selected', String(k === key));
        btn.tabIndex = k === key ? 0 : -1;                          // one tab in the Tab order; arrows move within
      }
    });
    store.set('plane.tab', key);
    if (location.hash.slice(1) !== key) history.replaceState(null, '', '#' + key);
    const dock = DOCKS[key] && el(DOCKS[key]);
    const view = el('model-view'), card = el('selected-card');
    if (dock && view && view.parentElement !== dock) {
      dock.appendChild(view);
      if (card) dock.appendChild(card);
    }
    if (dock) resize();
    const ctx = el('assistant-context'), btn = el('tabbtn-' + key);
    if (ctx) ctx.textContent = btn ? btn.dataset.label || '' : '';
    document.dispatchEvent(new CustomEvent('plane:tab', { detail: { key } }));
  }
  function go(key) {                                                // a user's choice: one history entry per tab
    userMoved = true;
    if (location.hash.slice(1) === key) showTab(key); else location.hash = key;
  }
  window.showTab = go;
  TABS.forEach((k) => { const b = el('tabbtn-' + k); if (b) b.addEventListener('click', () => go(k)); });

  // The WAI-ARIA tabs pattern: Left/Right move to the previous/next visible tab and open it,
  // Home/End jump to the first/last. Settings is a link: arrows focus it and Enter follows it.
  function onTabKey(ev) {
    const moves = { 'ArrowRight': 1, 'ArrowLeft': -1, 'Home': 'first', 'End': 'last' };
    if (!(ev.key in moves)) return;
    const tabs = [...document.querySelectorAll('.appbar-tabs [role="tab"]')].filter((b) => !b.hidden);
    const at = tabs.indexOf(document.activeElement);
    if (at < 0 || !tabs.length) return;
    ev.preventDefault();
    const m = moves[ev.key];
    const next = m === 'first' ? 0 : m === 'last' ? tabs.length - 1 : (at + m + tabs.length) % tabs.length;
    const b = tabs[next];
    b.focus();
    if (b.dataset.tab) go(b.dataset.tab);
  }
  const bar = document.querySelector('.appbar-tabs');
  if (bar) bar.addEventListener('keydown', onTabKey);
  window.addEventListener('hashchange', () => {
    const k = location.hash.slice(1);
    if (TABS.includes(k) && k !== current) showTab(k);
  });

  // ---------- Assistant panel ----------
  function applyAssistant(open) {                                   // shows or hides; never writes storage
    el('assistant').hidden = !open;
    el('assistant-rail').hidden = open;
    el('assistant-collapse').setAttribute('aria-expanded', String(open));
    el('assistant-rail').setAttribute('aria-expanded', String(open));
    document.body.classList.toggle('assistant-open', open);
    if (open) {
      const log = el('chat-log'); if (log) log.scrollTop = log.scrollHeight;
      if (window.loadWizard) window.loadWizard().catch(() => {});
    }
    resize();
  }
  function setAssistant(open) {                                     // the user's choice: remembered
    store.set('plane.assistant', open ? 'open' : 'closed');
    applyAssistant(open);
  }
  // ---------- Assistant width ----------
  // A share of the window, a third by default: ¼ · ⅓ · ⅔ in the panel's header, or drag its left edge to
  // anything between a quarter and two thirds (double-click the edge for a third). Remembered per browser.
  // Narrow screens overlay the panel at a fixed width instead (app.css).
  const MIN_W = 0.25, MAX_W = 2 / 3, DEFAULT_W = 1 / 3;
  const clampW = (f) => Math.min(MAX_W, Math.max(MIN_W, f));
  let width = (() => { const n = Number(store.get('plane.assistantWidth')); return n >= MIN_W && n <= MAX_W ? n : DEFAULT_W; })();
  function applyWidth(f, save) {
    width = clampW(f);
    document.documentElement.style.setProperty('--assistant-w', (width * 100).toFixed(3) + 'vw');
    document.querySelectorAll('.assistant-size [data-size]').forEach((b) => {
      const on = Math.abs(Number(b.dataset.size) - width) < 0.01;
      b.classList.toggle('on', on);
      b.setAttribute('aria-pressed', String(on));
    });
    const grip = el('assistant-grip');
    if (grip) grip.setAttribute('aria-valuenow', String(Math.round(width * 100)));
    if (save) store.set('plane.assistantWidth', String(width));
    resize();
  }
  document.querySelectorAll('.assistant-size [data-size]').forEach((b) =>
    b.addEventListener('click', () => applyWidth(Number(b.dataset.size), true)));
  const grip = el('assistant-grip');
  if (grip) {
    grip.addEventListener('pointerdown', (ev) => {
      ev.preventDefault();
      document.body.classList.add('assistant-dragging');
      const move = (e) => applyWidth((window.innerWidth - e.clientX) / window.innerWidth, false);
      const up = () => {
        document.removeEventListener('pointermove', move);
        document.removeEventListener('pointerup', up);
        document.body.classList.remove('assistant-dragging');
        applyWidth(width, true);
      };
      document.addEventListener('pointermove', move);
      document.addEventListener('pointerup', up);
    });
    grip.addEventListener('dblclick', () => applyWidth(DEFAULT_W, true));
    grip.addEventListener('keydown', (ev) => {                        // the handle is focusable: arrows resize
      const step = { 'ArrowLeft': 0.05, 'ArrowRight': -0.05 }[ev.key];
      if (step === undefined) return;
      ev.preventDefault();
      applyWidth(width + step, true);
    });
  }
  applyWidth(width, false);

  el('assistant-collapse').addEventListener('click', () => setAssistant(false));
  el('assistant-rail').addEventListener('click', () => setAssistant(true));

  // The old intake tabs' entry point, kept for chat.js, wizard.js and plan.js.
  window.showIntakeTab = (which) => {
    if (which === 'draft') go('build');
    else if (which === 'plan') go('requirements');
    else setAssistant(true);                                        // 'assistant', 'wizard' and anything else
  };

  // ---------- hide setup tabs 2–8 ----------
  function setHideSetup(on) {
    document.body.classList.toggle('hide-setup', on);
    SETUP.forEach((k) => { const b = el('tabbtn-' + k); if (b) b.hidden = on; });
    const box = el('hide-setup'); if (box) box.checked = on;
    store.set('plane.hideSetup', on ? '1' : '0');
  }
  const hideBox = el('hide-setup');
  if (hideBox) hideBox.addEventListener('change', () => setHideSetup(hideBox.checked));

  // ---------- full screen: the 3D view and the grid ----------
  function expand(id, on) {
    const box = el(id); if (!box) return;
    box.classList.toggle('expanded', on);
    document.body.classList.toggle('has-expanded', !!document.querySelector('.expanded'));
    box.querySelectorAll('[data-expand]').forEach((b) => {
      const t = on ? 'Close full screen' : 'Full screen';
      b.title = t; b.setAttribute('aria-label', t);
    });
    if (id === 'model-view') resize();
  }
  document.querySelectorAll('[data-expand]').forEach((b) => b.addEventListener('click', () => {
    const box = el(b.dataset.expand); if (box) expand(box.id, !box.classList.contains('expanded'));
  }));
  document.addEventListener('keydown', (ev) => {
    if (ev.key !== 'Escape' || document.querySelector('dialog[open]')) return;
    document.querySelectorAll('.expanded').forEach((x) => expand(x.id, false));
  });

  // ---------- the timetable's name in the bar ----------
  function syncName() {
    const sel = el('timetable'), tag = el('tt-current'); if (!sel || !tag) return;
    const opt = sel.selectedOptions && sel.selectedOptions[0];
    tag.textContent = opt ? opt.textContent : '';
    tag.title = tag.textContent;
    tag.hidden = !tag.textContent;
  }
  const ttSel = el('timetable');
  if (ttSel) {
    ttSel.addEventListener('change', syncName);
    new MutationObserver(syncName).observe(ttSel, { childList: true, subtree: true, attributes: true });
  }
  syncName();

  // ---------- start ----------
  const stored = store.get('plane.assistant');
  applyAssistant(stored ? stored === 'open' : window.innerWidth >= 1000);   // the width default is not saved
  setHideSetup(store.get('plane.hideSetup') === '1');
  const fromHash = location.hash.slice(1), fromStore = store.get('plane.tab');
  if (TABS.includes(fromHash)) showTab(fromHash);
  else if (TABS.includes(fromStore)) showTab(fromStore);
  else {
    showTab('project');
    // First visit on this browser: land on the Summary when there is a timetable to look at.
    fetch('/api/solid').then((r) => (r.ok ? r.json() : null)).then((s) => {
      if (s && s.organisation && !userMoved && current === 'project') showTab('summary');
    }).catch(() => {});
  }
})();
