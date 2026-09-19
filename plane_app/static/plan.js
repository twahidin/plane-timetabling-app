// The Plan tab: requirements, staff, divisions/bands and rules tables read from and written to
// the curriculum plan document (docs/superpowers/specs/2026-09-19-curriculum-plan-design.md §7).
// Mirrors chat.js's draft-table convention: click a cell, save on blur, PATCH the one field that
// changed. No raw-markup injection with data; node()/textContent only.
(function () {
  'use strict';

  const el = (id) => document.getElementById(id);
  const api = async (path, opts = {}) => {
    const r = await fetch(path, { headers: { 'Content-Type': 'application/json' }, ...opts });
    if (!r.ok) throw new Error((await r.json().catch(() => ({}))).detail || r.statusText);
    return r.json();
  };
  const node = (tag, cls, text) => {
    const n = document.createElement(tag);
    if (cls) n.className = cls;
    if (text != null) n.textContent = text;
    return n;
  };
  // Notes are chat.js's shared strip; fall back to writing #notes directly if chat.js has not
  // exported showNotes (e.g. this file loaded standalone).
  function notify(cls, text) {
    if (typeof window.showNotes === 'function') { window.showNotes([], [], [{ cls, text }]); return; }
    const box = el('notes');
    if (!box) return;
    box.textContent = '';
    box.appendChild(node('div', 'note ' + cls, text));
  }

  let plan = null;
  let issues = [];

  // Past ten, a list of issues in a toast is a wall rather than a list of things to fix; the
  // server caps what it sends the same way (plan/issues.py capped()).
  const ISSUE_LIMIT = 10;
  function capped(texts) {
    const all = texts || [];
    return all.length <= ISSUE_LIMIT ? all : all.slice(0, ISSUE_LIMIT).concat([`and ${all.length - ISSUE_LIMIT} more`]);
  }

  // ---------- tabs ----------
  function showTab(which) {
    const isPlan = which === 'plan';
    el('tab-draft').classList.toggle('on', !isPlan);
    el('tab-plan').classList.toggle('on', isPlan);
    el('tab-draft').setAttribute('aria-selected', String(!isPlan));
    el('tab-plan').setAttribute('aria-selected', String(isPlan));
    el('draft-panel').hidden = isPlan;
    el('plan').hidden = !isPlan;
    try { localStorage.setItem('plane.intakeTab', which); } catch (e) { /* private mode, etc. */ }
    if (isPlan) loadPlan();
  }
  el('tab-draft').addEventListener('click', () => showTab('draft'));
  el('tab-plan').addEventListener('click', () => showTab('plan'));

  // ---------- parse / format ----------
  function textParse(s) { return s.trim(); }
  function showText(v) { return v == null ? '' : String(v); }
  function parseTextOrNull(s) { const t = s.trim(); return t === '' ? null : t; }
  function parseIntStrict(s) {
    const t = s.trim();
    if (!/^-?\d+$/.test(t)) throw new Error('must be a whole number');
    return parseInt(t, 10);
  }
  function parseIntOrNull(s) { const t = s.trim(); return t === '' ? null : parseIntStrict(t); }
  function parseFloatStrict(s) {
    const t = s.trim();
    if (t === '' || Number.isNaN(Number(t))) throw new Error('must be a number');
    return Number(t);
  }
  function parseList(s) { return s.split(',').map((x) => x.trim()).filter(Boolean); }
  function showList(v) { return (v || []).join(', '); }
  // Same "0-8" / windows "0-3; 5-8" convention as chat.js's draft-table avail parsing.
  function parseAvail(text) {
    const s = text.trim();
    if (s === '') return null;
    return s.split(';').map((part) => {
      const nums = part.trim().split(/[\s,–-]+/).filter(Boolean).map(Number);
      if (nums.length !== 2 || nums.some((n) => !Number.isInteger(n))) {
        throw new Error('avail must be "0-8" or windows "0-3; 5-8"');
      }
      return nums;
    });
  }
  function showAvail(v) {
    if (!Array.isArray(v) || !v.length) return '';
    const windows = Array.isArray(v[0]) ? v : [v];
    return windows.map((w) => w.join('-')).join('; ');
  }
  // "1×2, 3×6" <-> {"1": 2, "3": 6}. Always emits all four lengths (0 for the rest) so a
  // PATCH replaces the whole lessons object rather than leaving a stale count behind: model.py's
  // apply_patch merges a dict-valued field one level deep, so an omitted key would keep its old value.
  function parseLessons(text) {
    const out = { '1': 0, '2': 0, '3': 0, '4': 0 };
    text.split(',').map((p) => p.trim()).filter(Boolean).forEach((part) => {
      const m = part.match(/^(\d+)\s*[×x]\s*(\d+)$/i);
      if (!m || !(m[1] in out)) throw new Error('lessons must look like "1×2, 3×6" (lengths 1-4)');
      out[m[1]] = Number(m[2]);
    });
    return out;
  }
  function showLessons(v) {
    return Object.entries(v || {}).filter(([, n]) => n).map(([k, n]) => `${k}×${n}`).join(', ');
  }
  function showJSON(v) { return JSON.stringify(v == null ? [] : v); }
  function parseJSON(text) {
    let v;
    try { v = JSON.parse(text.trim() === '' ? '[]' : text); }
    catch (e) { throw new Error('pinned must be valid JSON, a list of pins'); }
    if (!Array.isArray(v)) throw new Error('pinned must be a JSON list');
    return v;
  }
  function getPath(obj, key) {
    return key.split('.').reduce((o, k) => (o == null ? o : o[k]), obj);
  }

  // ---------- editable cells ----------
  function buildPatch(section, id, field, value) {
    if (section === 'rules') return { rules: { [field]: value } };
    const dot = field.indexOf('.');
    if (dot !== -1) {
      const outer = field.slice(0, dot), inner = field.slice(dot + 1);
      return { [section]: { [id]: { [outer]: { [inner]: value } } } };
    }
    return { [section]: { [id]: { [field]: value } } };
  }

  function flashSaved(td) {
    if (!td) return;
    td.classList.add('saved');
    setTimeout(() => td.classList.remove('saved'), 1200);
  }

  // The same cell after its table was rebuilt: section, id and field are the cell's identity.
  function findCell({ section, id, field }) {
    const idPart = id == null ? '' : `[data-id="${id}"]`;
    return document.querySelector(`td[data-section="${section}"]${idPart}[data-field="${field}"]`);
  }

  async function commitPlanCell(td, parseFn) {
    const text = td.textContent;
    if (text === td.dataset.orig) return;
    td.classList.remove('saved', 'failed'); td.title = '';
    let value;
    try { value = parseFn(text); }
    catch (e) { td.classList.add('failed'); td.title = e.message; notify('error', e.message); return; }
    const patch = buildPatch(td.dataset.section, td.dataset.id || null, td.dataset.field, value);
    try {
      const res = await api('/api/plan', { method: 'PATCH', body: JSON.stringify({ patch }) });
      plan = res.plan; issues = res.issues || [];
      td.dataset.orig = text;
      renderIssues();
      // The server normalises what it stored (periods recomputed from lessons, a sorted list, a
      // dropped item), so the edited table is re-read from the plan it returned rather than left
      // showing what was typed — unless the user has already clicked into another cell of the same
      // table, where rebuilding it would throw away what they are typing.
      const table = td.closest('table');
      const busy = table && document.activeElement && table.contains(document.activeElement);
      if (busy) { flashSaved(td); } else { renderSection(td.dataset.section); flashSaved(findCell(td.dataset)); }
    } catch (e) {
      td.classList.add('failed'); td.title = e.message;
      notify('error', `${td.dataset.id || td.dataset.section}.${td.dataset.field}: ${e.message}`);
    }
  }

  function editableTd(section, id, field, text, parseFn) {
    const td = node('td', null, text);
    td.contentEditable = 'true'; td.spellcheck = false;
    td.dataset.section = section;
    if (id != null) td.dataset.id = id;
    td.dataset.field = field;
    td.dataset.orig = td.textContent;
    td.addEventListener('keydown', (ev) => {
      if (ev.key === 'Enter') { ev.preventDefault(); td.blur(); }
      if (ev.key === 'Escape') { td.textContent = td.dataset.orig; td.blur(); }
    });
    td.addEventListener('blur', () => commitPlanCell(td, parseFn));
    return td;
  }

  // ---------- tables ----------
  const REQ_COLUMNS = [
    { key: 'dept', label: 'dept', parse: textParse, show: showText },
    { key: 'level', label: 'level', parse: textParse, show: showText },
    { key: 'subject', label: 'subject', parse: textParse, show: showText },
    { key: 'periods', label: 'periods', parse: parseIntStrict, show: showText },
    { key: 'lessons', label: 'lessons', parse: parseLessons, show: showLessons },
    { key: 'grouping', label: 'grouping', parse: textParse, show: showText },
    { key: 'classes', label: 'classes', parse: parseList, show: showList },
    { key: 'teachers', label: 'teachers', parse: parseList, show: showList },
    { key: 'venue.kind', label: 'venue kind', parse: parseTextOrNull, show: showText },
    { key: 'venue.room', label: 'venue room', parse: parseTextOrNull, show: showText },
  ];
  const STAFF_COLUMNS = [
    { key: 'name', label: 'name', parse: textParse, show: showText },
    { key: 'short', label: 'short', parse: textParse, show: showText },
    { key: 'dept', label: 'dept', parse: textParse, show: showText },
    { key: 'load_factor', label: 'load_factor', parse: parseFloatStrict, show: showText },
    { key: 'avail', label: 'avail', parse: parseAvail, show: showAvail },
    { key: 'max_periods_day', label: 'max_periods_day', parse: parseIntOrNull, show: showText },
  ];

  function renderKeyedTable(table, section, columns, rows, idField) {
    table.textContent = '';
    const thead = node('thead'), htr = node('tr');
    [idField].concat(columns.map((c) => c.label)).forEach((h) => htr.appendChild(node('th', null, h)));
    thead.appendChild(htr); table.appendChild(thead);
    const tbody = node('tbody');
    (rows || []).forEach((row) => {
      const tr = node('tr');
      tr.appendChild(node('td', 'id', row[idField]));
      columns.forEach((c) => {
        const text = c.show(getPath(row, c.key));
        tr.appendChild(editableTd(section, row[idField], c.key, text, c.parse));
      });
      tbody.appendChild(tr);
    });
    table.appendChild(tbody);
  }

  function renderDivisions(table, divisions, bands) {
    table.textContent = '';
    const thead = node('thead'), htr = node('tr');
    ['id', 'classes', 'bands'].forEach((h) => htr.appendChild(node('th', null, h)));
    thead.appendChild(htr); table.appendChild(thead);
    const tbody = node('tbody');
    (divisions || []).forEach((d) => {
      const tr = node('tr');
      tr.appendChild(node('td', 'id', d.id));
      tr.appendChild(editableTd('divisions', d.id, 'classes', showList(d.classes), parseList));
      const divBands = (bands || []).filter((b) => b.division === d.id);
      const bandsText = divBands.map((b) => `${b.id}: ${showList(b.options)}`).join('; ');
      tr.appendChild(node('td', null, bandsText));
      tbody.appendChild(tr);
    });
    table.appendChild(tbody);
  }

  function renderRules(table, rules) {
    table.textContent = '';
    const thead = node('thead'), htr = node('tr');
    ['edge_subjects', 'pinned'].forEach((h) => htr.appendChild(node('th', null, h)));
    thead.appendChild(htr); table.appendChild(thead);
    const tbody = node('tbody'), tr = node('tr');
    tr.appendChild(editableTd('rules', null, 'edge_subjects', showList((rules || {}).edge_subjects), parseList));
    tr.appendChild(editableTd('rules', null, 'pinned', showJSON((rules || {}).pinned), parseJSON));
    tbody.appendChild(tr);
    table.appendChild(tbody);
  }

  function renderIssues() {
    const box = el('plan-issues');
    box.textContent = '';
    const blocks = issues.filter((i) => i.level === 'block');
    const warns = issues.filter((i) => i.level === 'warn');
    const summary = node('div', 'plan-issues-summary');
    summary.appendChild(node('span', 'chip bad', `${blocks.length} blocking`));
    summary.appendChild(node('span', 'chip warn', `${warns.length} warning${warns.length === 1 ? '' : 's'}`));
    box.appendChild(summary);
    if (issues.length) {
      const list = node('ul', 'plan-issue-list');
      issues.forEach((i) => {
        const li = node('li', i.level);
        li.title = i.where;
        li.textContent = i.text;
        list.appendChild(li);
      });
      box.appendChild(list);
    }
    el('plan-generate').disabled = blocks.length > 0;
  }

  function renderSection(section) {
    if (section === 'requirements') renderKeyedTable(el('plan-requirements'), 'requirements', REQ_COLUMNS, plan.requirements, 'id');
    else if (section === 'staff') renderKeyedTable(el('plan-staff'), 'staff', STAFF_COLUMNS, plan.staff, 'id');
    else if (section === 'divisions' || section === 'bands') renderDivisions(el('plan-divisions'), plan.divisions, plan.bands);
    else if (section === 'rules') renderRules(el('plan-rules'), plan.rules);
  }

  function renderAll() {
    ['requirements', 'staff', 'divisions', 'rules'].forEach(renderSection);
    renderIssues();
  }

  // ---------- load / upload / generate ----------
  async function loadPlan() {
    let data;
    try { data = await api('/api/plan'); }
    catch (e) { notify('error', e.message); return; }
    plan = data.plan; issues = data.issues || [];
    renderAll();
  }
  window.loadPlan = loadPlan;

  const planUpload = el('plan-upload');
  async function uploadPlan(fileList) {
    const files = Array.from(fileList || []);
    if (!files.length) return;
    const csvs = files.filter((f) => /\.csv$/i.test(f.name));
    const main = files.find((f) => !/\.csv$/i.test(f.name));
    if (!main) { notify('error', 'Pick a workbook (.xlsx); .csv files are read as sizes alongside it.'); planUpload.value = ''; return; }
    const fd = new FormData();
    fd.append('file', main, main.name);
    csvs.forEach((f) => fd.append('sizes', f, f.name));
    planUpload.disabled = true;
    try {
      const r = await fetch('/api/plan/upload', { method: 'POST', body: fd });
      const body = await r.json().catch(() => ({}));
      if (!r.ok) { notify('error', body.detail || r.statusText); return; }
      plan = body.plan; issues = body.issues || [];
      renderAll();
      notify('good', body.note || 'Workbook imported.');
    } catch (e) { notify('error', e.message); }
    finally { planUpload.disabled = false; planUpload.value = ''; }
  }
  planUpload.addEventListener('change', () => uploadPlan(planUpload.files));

  el('plan-generate').addEventListener('click', async () => {
    const btn = el('plan-generate'); btn.disabled = true;
    try {
      const r = await fetch('/api/plan/generate', { method: 'POST', headers: { 'Content-Type': 'application/json' } });
      const body = await r.json().catch(() => ({}));
      if (r.status === 409) {
        notify('error', `${body.detail || 'blocking issues'}: ${capped(body.blocks).join('; ')}`);
        return;
      }
      if (!r.ok) { notify('error', body.detail || r.statusText); return; }
      issues = body.issues || [];
      const s = body.summary || {};
      const warnText = (s.warnings || []).length ? ' · ' + capped(s.warnings).join('; ') : '';
      notify('good', `Generated: ${s.teachers || 0} teachers, ${s.groups || 0} groups, ${s.bands || 0} bands, ${s.events || 0} events.${warnText}`);
      showTab('draft');
      if (window.loadDraft) await window.loadDraft();
    } catch (e) { notify('error', e.message); }
    finally { renderIssues(); }
  });

  let initialTab = 'draft';
  try { initialTab = localStorage.getItem('plane.intakeTab') === 'plan' ? 'plan' : 'draft'; } catch (e) { /* ignore */ }
  showTab(initialTab);
})();
