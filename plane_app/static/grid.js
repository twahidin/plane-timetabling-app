(function () {
  'use strict';
  // The Timetable tab: the printouts' grid on the page, one person / class / option group / room at a
  // time, problems marked on their cells, a click selecting the lesson for the Selected card (through
  // model.js's selectEvent) — spec docs/superpowers/specs/2026-09-20-plain-language-view-design.md §3.
  const el = (id) => document.getElementById(id);
  const esc = (s) => String(s == null ? '' : s).replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
  const api = async (path) => {
    const r = await fetch(path);
    if (!r.ok) throw new Error((await r.json().catch(() => ({}))).detail || r.statusText);
    return r.json();
  };
  const store = { get: (k) => { try { return localStorage.getItem(k); } catch (e) { return null; } },
                  set: (k, v) => { try { localStorage.setItem(k, v); } catch (e) { /* private mode */ } } };

  // kind -> the /api/print/targets key and the vocabulary key of its label
  const KINDS = [
    { kind: 'teacher', key: 'teachers', label: () => Words.word('person', { cap: true }) },
    { kind: 'class', key: 'classes', label: () => Words.word('group', { cap: true }) },
    { kind: 'group', key: 'groups', label: () => Words.word('group', { cap: true }) + ' option' },
    { kind: 'room', key: 'rooms', label: () => Words.word('venue', { cap: true }) },
  ];
  let targets = null;                 // last /api/print/targets response
  let current = { kind: 'teacher', id: null };
  let selected = null;
  let gen = 0;                        // overlapping reloads: only the newest may touch the DOM
  let loadError = null;               // a /api/print/targets failure worth showing (not "nothing built yet")

  // What stands in for the grid when there is nothing to draw.
  function emptyText() {
    if (loadError) return loadError;
    if (targets && el('grid-filter').value.trim() && list(current.kind).length) return 'Nothing matches.';
    return 'No timetable yet.';
  }

  function list(kind) {
    const k = KINDS.find((x) => x.kind === kind);
    const raw = (targets && k && targets[k.key]) || [];
    return raw.map((t) => (typeof t === 'string' ? { id: t, name: t } : t));
  }
  function fillKinds() {
    const sel = el('grid-kind');
    sel.innerHTML = KINDS.filter((k) => list(k.kind).length).map((k) => `<option value="${k.kind}">${esc(k.label())}</option>`).join('');
    if (![...sel.options].some((o) => o.value === current.kind)) current.kind = sel.options.length ? sel.options[0].value : 'teacher';
    sel.value = current.kind;
  }
  function fillTargets() {
    const q = el('grid-filter').value.trim().toLowerCase();
    const items = list(current.kind).filter((t) => !q || t.name.toLowerCase().includes(q));
    const sel = el('grid-target');
    sel.innerHTML = items.map((t) => `<option value="${esc(t.id)}">${esc(t.name)}</option>`).join('');
    if (!items.some((t) => t.id === current.id)) current.id = items.length ? items[0].id : null;
    if (current.id != null) sel.value = current.id;
  }
  function remember() { if (current.id != null) store.set('plane.gridTarget', current.kind + '/' + current.id); }

  function cellHtml(c) {
    if (c.kind === 'continued') return '';
    const bad = c.bad && c.bad.length;
    const cls = [c.kind, bad ? 'bad' : '', c.events && c.events.includes(selected) ? 'on' : ''].filter(Boolean).join(' ');
    // hover shows the whole cell (the cell itself clamps long text) and, first, what is wrong with it
    const tip = [].concat(bad ? c.bad : [], c.text ? [c.text] : [], c.sub ? [c.sub] : []).join('\n');
    const attrs = `class="${cls}"` + (c.span > 1 ? ` colspan="${c.span}"` : '') + (c.events && c.events.length ? ` data-events="${esc(c.events.join(' '))}"` : '')
      + (tip ? ` title="${esc(tip)}"` : '');
    if (c.kind === 'rest') return `<td ${attrs}><span class="s">${esc(c.text || 'rest')}</span></td>`;
    if (c.kind === 'lesson' || c.kind === 'booking') return `<td ${attrs}><div class="t">${esc(c.text)}</div><div class="s">${esc(c.sub)}</div></td>`;
    return `<td ${attrs}></td>`;
  }
  // Cell size: "fit" squeezes every column into the page width; a number is each column's width in
  // pixels, and the grid scrolls sideways under a sticky day column and time row. The − / + buttons
  // step through STEPS; dragging the edge of any time heading sets any width in between.
  const STEPS = [70, 90, 120, 160, 220, 300];
  const MIN_COL = 45, MAX_COL = 400;
  const clampCol = (w) => Math.max(MIN_COL, Math.min(MAX_COL, Math.round(w)));
  let zoom = (() => { const z = store.get('plane.gridZoom'); const n = Number(z); return z && z !== 'fit' && n >= MIN_COL && n <= MAX_COL ? n : 'fit'; })();
  function applyZoom() {
    const box = el('grid');
    const fit = zoom === 'fit';
    box.classList.toggle('zoomed', !fit);
    if (!fit) box.style.setProperty('--tt-col', zoom + 'px'); else box.style.removeProperty('--tt-col');
    const t = box.querySelector('table.tt');
    if (t) t.style.width = fit ? '' : `calc(6em + ${t.dataset.cols} * ${zoom}px)`;
    el('grid-zoom-fit').classList.toggle('on', fit);
    el('grid-zoom-out').disabled = fit;
    el('grid-zoom-in').disabled = zoom !== 'fit' && zoom >= STEPS[STEPS.length - 1];
    store.set('plane.gridZoom', String(zoom));
  }
  // The next step up or down from wherever the width is now (a dragged width sits between steps);
  // stepping down from the narrowest step goes back to Fit.
  function stepZoom(d) {
    if (zoom === 'fit') { if (d > 0) zoom = STEPS[0]; }
    else if (d > 0) zoom = STEPS.find((w) => w > zoom) || STEPS[STEPS.length - 1];
    else zoom = [...STEPS].reverse().find((w) => w < zoom) || 'fit';
    applyZoom();
  }
  // Dragging the right edge of any time heading stretches every column to match; double-clicking an
  // edge goes back to Fit. Starts from the column's width on screen, so Fit can be stretched too.
  function startDrag(ev, th) {
    ev.preventDefault();
    const x0 = ev.clientX, w0 = th.getBoundingClientRect().width;
    document.body.classList.add('tt-dragging');
    const move = (e) => { zoom = clampCol(w0 + e.clientX - x0); applyZoom(); };
    const up = () => {
      document.removeEventListener('pointermove', move);
      document.removeEventListener('pointerup', up);
      document.body.classList.remove('tt-dragging');
    };
    document.addEventListener('pointermove', move);
    document.addEventListener('pointerup', up);
  }
  el('grid-zoom-out').addEventListener('click', () => stepZoom(-1));
  el('grid-zoom-in').addEventListener('click', () => stepZoom(1));
  el('grid-zoom-fit').addEventListener('click', () => { zoom = 'fit'; applyZoom(); });

  function renderGrid(g) {
    const box = el('grid');
    box.innerHTML = `<div class="tt-head"><span class="tt-title">${esc(g.title)}</span> <span class="tt-sub">${esc(g.subtitle)}</span></div>` +
      `<div class="tt-scroll"><table class="tt" data-cols="${g.slots.length}"><thead><tr><th class="day"></th>${g.slots.map((s) => `<th>${esc(s)}<span class="tt-grip" title="Drag to stretch the columns; double-click to fit"></span></th>`).join('')}</tr></thead><tbody>` +
      g.days.map((d) => `<tr${d.off ? ' class="off"' : ''}><th class="day">${esc(d.label)}</th>${d.cells.map(cellHtml).join('')}</tr>`).join('') +
      '</tbody></table></div>';
    applyZoom();
    box.querySelectorAll('.tt-grip').forEach((grip) => {
      grip.addEventListener('pointerdown', (ev) => startDrag(ev, grip.parentElement));
      grip.addEventListener('dblclick', () => { zoom = 'fit'; applyZoom(); });
    });
    box.querySelectorAll('td[data-events]').forEach((td) => td.addEventListener('click', () => {
      const first = td.dataset.events.split(' ')[0];
      if (window.selectEvent) window.selectEvent(first);
    }));
  }
  async function render() {
    const my = ++gen;
    const box = el('grid');
    if (current.id == null) { box.innerHTML = `<p class="empty">${esc(emptyText())}</p>`; return; }
    const date = el('grid-date').value;
    let g;
    try { g = await api(`/api/grid/${current.kind}/${encodeURIComponent(current.id)}` + (date ? `?view=week&date=${encodeURIComponent(date)}` : '')); }
    catch (e) { if (my === gen) box.innerHTML = `<p class="empty">${esc(e.message)}</p>`; return; }
    if (my !== gen) return;
    renderGrid(g);
  }

  window.gridHighlight = (eventId) => {
    selected = eventId;
    el('grid').querySelectorAll('td[data-events]').forEach((td) => td.classList.toggle('on', !!eventId && td.dataset.events.split(' ').includes(eventId)));
  };
  window.gridReload = async () => {
    const my = ++gen;
    let got = null, err = null;
    try { got = await api('/api/print/targets'); }
    catch (e) { err = e; }
    if (my !== gen) return;                 // a newer reload owns the DOM: leave its targets alone
    targets = got;
    loadError = err && !/no live timetable/i.test(err.message) ? err.message : null;
    if (!targets) { current.id = null; fillKinds(); fillTargets(); await render(); return; }
    const saved = store.get('plane.gridTarget');
    if (saved && current.id == null) { const [k, ...rest] = saved.split('/'); current = { kind: k, id: rest.join('/') }; }
    fillKinds(); fillTargets(); remember();
    await render();
  };

  // Stage tabs: the grid is the default; the 3D model is the expert view. The canvas is sized from
  // its box, so showing it again needs a resize.
  window.showStage = (which) => {
    const grid = which !== 'model';
    el('grid-view').hidden = !grid; el('model-view').hidden = grid;
    el('stage-grid').classList.toggle('on', grid); el('stage-model').classList.toggle('on', !grid);
    el('stage-grid').setAttribute('aria-selected', String(grid)); el('stage-model').setAttribute('aria-selected', String(!grid));
    store.set('plane.stageTab', grid ? 'grid' : 'model');
    if (!grid && window.resizeModel) window.resizeModel();
  };
  el('stage-grid').addEventListener('click', () => window.showStage('grid'));
  el('stage-model').addEventListener('click', () => window.showStage('model'));
  el('grid-kind').addEventListener('change', () => { current.kind = el('grid-kind').value; current.id = null; el('grid-filter').value = ''; fillTargets(); remember(); render(); });
  el('grid-filter').addEventListener('input', () => { fillTargets(); remember(); render(); });
  el('grid-target').addEventListener('change', () => { current.id = el('grid-target').value; remember(); render(); });
  el('grid-date').addEventListener('change', render);
  el('grid-print').addEventListener('click', () => { if (current.id != null && window.openPrint) window.openPrint(current.kind, current.id); });
  window.showStage(store.get('plane.stageTab') === 'model' ? 'model' : 'grid');
  // Load on our own: model.js calls gridReload() too, but it never gets there when the 3D library
  // is missing — the Timetable tab must fill either way. The gen guard makes the extra fetch harmless.
  window.gridReload();
})();
