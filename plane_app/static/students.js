// The Students section on the Requirements tab (spec docs/superpowers/specs/2026-10-16-students-design.md
// §1): the timetable's students with a search and a class filter, each with the subjects they take as
// chips; add, edit and remove; Import (the Intake drop does the reading); Remove all; and the "To check"
// list. A student with a level and no class yet (from a level-only file) is listed after the classes, under
// a heading "<level> · no class yet", with a class-filter choice of its own. Admin only: /api/me says who is asking, a department account never has the section shown nor a
// student route called. Every name, ID and subject is set with textContent, never as HTML. A message
// names a student by ID, never by name. Loads on the Requirements tab (shell.js's plane:tab event) and
// after a reading is applied (plane:readings) or a students reading is applied or undone (plane:students).
(function () {
  'use strict';

  const el = (id) => document.getElementById(id);
  const node = (tag, cls, text) => {
    const n = document.createElement(tag);
    if (cls) n.className = cls;
    if (text != null) n.textContent = text;
    return n;
  };
  const button = (cls, text, onClick) => {
    const b = node('button', cls, text);
    b.type = 'button';
    if (onClick) b.addEventListener('click', onClick);
    return b;
  };
  async function api(path, opts = {}) {
    const r = await fetch(path, { headers: { 'Content-Type': 'application/json' }, ...opts });
    const data = await r.json().catch(() => ({}));
    if (!r.ok) throw new Error(typeof data.detail === 'string' ? data.detail : r.statusText);
    return data;
  }
  const fmt = (n) => Number(n).toLocaleString('en-US');
  const plural = (n, one, many) => `${fmt(n)} ${n === 1 ? one : many}`;
  const swallow = () => {};                 // a failed click has already said why in the message line

  const PAGE = 200;                         // rows drawn at a time: a school has a thousand or more
  const PROBLEMS_SHOWN = 50;

  let admin = false;
  let students = [], summary = { count: 0, patterns: 0, to_check: 0 }, problems = [];
  let classes = [];                         // the plan's class codes (the Add form's choices)
  let query = '', klass = '', shown = PAGE;
  let subjects = {};                        // 'division|grouping' -> subject, as /api/students gives it (for the chips)
  let tab = '';                             // the tab showing (shell.js's plane:tab); readings reload only on Requirements
  let formState = null;                     // { isNew, pickable: Map id -> take } while the edit form is open
  let generation = 0, loading = Promise.resolve();

  const say = (text, cls) => {
    const m = el('students-msg');
    m.textContent = text || '';
    m.className = 'students-msg' + (cls ? ' ' + cls : '');
  };
  const same = (a, b) => String(a).replace(/\s+/g, '').toLowerCase() === String(b).replace(/\s+/g, '').toLowerCase();
  const takeKey = (t) => t.division + '|' + t.grouping;
  const NO_CLASS = ' · no class yet';                       // "Sec 3 · no class yet"
  const LEVEL_PREFIX = 'level:';                            // the class filter's value for one level's class-less students
  const noClass = (s) => !String(s.class || '').trim();
  const levelOf = (s) => String(s.level || '').trim();
  const byLevel = (a, b) => levelOf(a).localeCompare(levelOf(b), undefined, { numeric: true, sensitivity: 'base' });

  // "Physics 3SCIA": the subject beside the option code, once the options are known
  function chipText(t) {
    const subject = subjects[takeKey(t)];
    return !subject || same(subject, t.grouping) ? t.grouping : subject + ' ' + t.grouping;
  }
  const groupText = (g) => (same(g.subject, g.grouping) ? g.grouping : g.subject + ' ' + g.grouping);

  // The options a class can take: asked only when the edit form opens or its class changes
  const fetchOptions = (cls) => api('/api/students/options?class=' + encodeURIComponent(cls));

  // ---------- the list ----------
  function matching() {
    const q = query.trim().toLowerCase();
    const level = klass.indexOf(LEVEL_PREFIX) === 0 ? klass.slice(LEVEL_PREFIX.length) : null;
    return students.filter((s) => (!klass || (level !== null ? noClass(s) && levelOf(s) === level : s.class === klass))
      && (!q || s.name.toLowerCase().indexOf(q) !== -1 || s.id.toLowerCase().indexOf(q) !== -1));
  }

  function chips(s) {
    const cell = node('td', 'student-chips');
    cell.setAttribute('data-label', 'Subjects');
    const placed = new Set(s.placed_by_app || []);
    s.takes.forEach((t) => {
      const isPlaced = placed.has(t.grouping);
      const c = node('span', 'student-chip' + (isPlaced ? ' placed' : ''), chipText(t));
      c.setAttribute('title', isPlaced ? 'placed by the app' : t.division);
      if (isPlaced) c.setAttribute('aria-label', chipText(t) + ', placed by the app');
      cell.appendChild(c);
    });
    return cell;
  }

  function row(s) {
    const tr = node('tr');
    [['ID', s.id, 'id'], ['Name', s.name, 'student-name-cell'], ['Class', noClass(s) ? '' : s.class, '']].forEach(([label, text, cls]) => {
      const td = node('td', cls, text);
      td.setAttribute('data-label', label);
      tr.appendChild(td);
    });
    tr.appendChild(chips(s));
    const act = node('td', 'student-actions');
    const edit1 = button('btn ghost', 'Edit', () => { edit(s.id).catch(swallow); });
    edit1.setAttribute('aria-label', 'Edit ' + s.id);
    const remove1 = button('btn ghost', 'Remove', () => { removeOne(s.id).catch(swallow); });
    remove1.setAttribute('aria-label', 'Remove ' + s.id);
    act.appendChild(edit1);
    act.appendChild(remove1);
    tr.appendChild(act);
    return tr;
  }

  // The heading above one level's students who have no class yet
  function groupHead(level) {
    const tr = node('tr', 'students-group');
    const th = node('th', '', (level || 'No level') + NO_CLASS);
    th.colSpan = 5;
    tr.appendChild(th);
    return tr;
  }

  function renderProblems() {
    const box = el('students-problems');
    box.textContent = '';
    if (!problems.length) return;
    box.appendChild(node('h3', '', 'To check'));
    const list = node('div', 'students-problem-list');
    problems.slice(0, PROBLEMS_SHOWN).forEach((p) => {
      const item = node('div', 'students-problem');
      item.appendChild(node('span', 'students-problem-text', p.text));
      if (p.detail) item.appendChild(node('span', 'students-problem-detail', String(p.detail).trim()));
      if (p.student && students.some((s) => s.id === p.student)) {
        item.appendChild(button('btn ghost', 'Edit', () => { edit(p.student).catch(swallow); }));
      }
      list.appendChild(item);
    });
    if (problems.length > PROBLEMS_SHOWN) list.appendChild(node('div', 'students-problem', `and ${problems.length - PROBLEMS_SHOWN} more`));
    box.appendChild(list);
  }

  function render() {
    el('students-head').textContent = `${plural(summary.count, 'student', 'students')} · ${plural(summary.patterns, 'choice pattern', 'choice patterns')} · ${fmt(summary.to_check)} to check`
      + (summary.no_class ? ` · ${fmt(summary.no_class)} with no class yet` : '');
    const table = el('students-table');
    table.textContent = '';
    const found = matching();
    if (!found.length) {
      table.appendChild(node('caption', 'students-empty', students.length ? 'No student matches.' : 'No students yet. Import a list or add one.'));
    } else {
      const head = node('tr');
      ['ID', 'Name', 'Class', 'Subjects', ''].forEach((h) => head.appendChild(node('th', '', h)));
      const thead = node('thead');
      thead.appendChild(head);
      table.appendChild(thead);
      const body = node('tbody');
      // the classes first, then each level's students with no class yet under their own heading
      const ordered = found.filter((s) => !noClass(s)).concat(found.filter(noClass).sort(byLevel));
      let heading = null;
      ordered.slice(0, shown).forEach((s) => {
        if (noClass(s) && heading !== levelOf(s)) {
          heading = levelOf(s);
          body.appendChild(groupHead(heading));
        }
        body.appendChild(row(s));
      });
      table.appendChild(body);
    }
    const more = el('students-more');
    const rest = found.length - shown;
    more.hidden = rest <= 0;
    more.textContent = rest > 0 ? `Show ${Math.min(PAGE, rest)} more (${fmt(rest)} left)` : '';
    el('students-clear').disabled = !students.length;
    renderProblems();
  }

  function fillClassFilter() {
    const sel = el('students-class');
    const all = Array.from(new Set(classes.concat(students.map((s) => s.class)))).filter(Boolean);
    const waiting = Array.from(new Set(students.filter(noClass).map(levelOf))).sort((a, b) => byLevel({ level: a }, { level: b }));
    sel.textContent = '';
    const first = node('option', '', 'All classes');
    first.value = '';
    sel.appendChild(first);
    all.forEach((c) => { const o = node('option', '', c); o.value = c; sel.appendChild(o); });
    waiting.forEach((lv) => { const o = node('option', '', (lv || 'No level') + NO_CLASS); o.value = LEVEL_PREFIX + lv; sel.appendChild(o); });
    if (klass && all.indexOf(klass) === -1 && !waiting.some((lv) => LEVEL_PREFIX + lv === klass)) klass = '';
    sel.value = klass;
  }

  function filter(q, cls) {
    query = q || '';
    klass = cls || '';
    shown = PAGE;
    el('students-search').value = query;
    el('students-class').value = klass;
    render();
  }

  // ---------- loading ----------
  async function doLoad() {
    const mine = ++generation;
    try {
      const data = await api('/api/students');
      let plan = null;
      try { plan = await api('/api/plan'); } catch (e) { plan = null; }
      if (mine !== generation) return;
      students = data.students || [];
      summary = data.summary || { count: students.length, patterns: 0, to_check: 0 };
      problems = data.problems || [];
      classes = plan && plan.plan ? (plan.plan.classes || []).map((c) => c.code) : [];
      subjects = data.subjects || {};
      fillClassFilter();
      render();
    } catch (e) {
      if (mine === generation) say(e.message, 'bad');
    }
  }
  function load() {                          // `ready` (below) is settled first, so a department account never loads
    loading = ready.then(() => (admin ? doLoad() : null));
    return loading;
  }

  // ---------- add, edit, remove ----------
  async function send(path, method, record) {
    try {
      return await api(path, { method, body: record ? JSON.stringify(record) : undefined });
    } catch (e) {
      say(e.message, 'bad');
      throw e;
    }
  }

  async function add(record) {
    await send('/api/students', 'POST', record);
    say(`Added ${record.id}.`, 'ok');
    await load();
  }
  async function update(id, record) {
    await send('/api/students/' + encodeURIComponent(id), 'PUT', record);
    say(`Saved ${id}.`, 'ok');
    await load();
  }
  async function remove(id) {
    await send('/api/students/' + encodeURIComponent(id), 'DELETE');
    say(`Removed ${id}.`, 'ok');
    await load();
  }
  async function removeOne(id) {            // asks first, naming the ID only
    if (!confirm(`Remove ${id}?`)) return;
    await remove(id);
  }
  async function removeAll() {
    const n = students.length;
    if (!n) return;
    if (!confirm(`Remove all ${fmt(n)} students? This empties the list for this timetable.`)) return;
    await send('/api/students', 'DELETE');
    say(`Removed all ${fmt(n)} students.`, 'ok');
    closeForm();
    await load();
  }

  // ---------- the edit form ----------
  function closeForm() {
    formState = null;
    const f = el('students-form');
    f.textContent = '';
    f.hidden = true;
  }

  function collectTakes() {
    const f = el('students-form');
    const out = [];
    const add1 = (id) => { const t = formState.pickable.get(id); if (t && !out.some((o) => takeKey(o) === takeKey(t))) out.push({ division: t.division, grouping: t.grouping }); };
    f.querySelectorAll('.student-band').forEach((sel) => { if (sel.value) add1(sel.value); });
    f.querySelectorAll('.student-option').forEach((box) => { if (box.checked) add1(box.value); });
    return out;
  }

  // One select per band (a student takes at most one option in a band), a checkbox for each option
  // outside the bands, and a checked box for a subject taken that the class is no longer offered.
  async function fillPicker(cls, takes) {
    const picker = el('students-form').querySelector('.student-picker');
    const state = formState;
    const mine = state.fillId = (state.fillId || 0) + 1;       // a slower answer for an earlier class is dropped
    let bands = [];
    try { bands = cls ? await fetchOptions(cls) : []; } catch (e) { say(e.message, 'bad'); }
    if (formState !== state || state.fillId !== mine) return;
    picker.textContent = '';
    state.pickable = new Map();
    const have = new Set(takes.map(takeKey));
    const offered = new Set();
    bands.forEach((b) => {
      b.groups.forEach((g) => { state.pickable.set(g.id, { division: g.division, grouping: g.grouping }); offered.add(g.division + '|' + g.grouping); });
      if (b.band == null) {
        b.groups.forEach((g) => {
          const label = node('label', 'student-pick');
          const box = node('input', 'student-option');
          box.type = 'checkbox';
          box.value = g.id;
          box.checked = have.has(g.division + '|' + g.grouping);
          label.appendChild(box);
          label.appendChild(node('span', '', groupText(g)));
          picker.appendChild(label);
        });
        return;
      }
      const label = node('label', 'student-pick');
      label.appendChild(node('span', 'student-pick-name', b.label || 'Band'));    // never the band's raw id
      const sel = node('select', 'student-band');
      const none = node('option', '', 'none');
      none.value = '';
      sel.appendChild(none);
      b.groups.forEach((g) => { const o = node('option', '', groupText(g)); o.value = g.id; sel.appendChild(o); });
      const picked = b.groups.find((g) => have.has(g.division + '|' + g.grouping));
      sel.value = picked ? picked.id : '';
      label.appendChild(sel);
      picker.appendChild(label);
    });
    takes.filter((t) => !offered.has(takeKey(t))).forEach((t, i) => {
      const id = 'old:' + i;
      state.pickable.set(id, t);
      const label = node('label', 'student-pick');
      const box = node('input', 'student-option');
      box.type = 'checkbox';
      box.value = id;
      box.checked = true;
      label.appendChild(box);
      label.appendChild(node('span', '', t.grouping + ' (not offered to this class)'));
      picker.appendChild(label);
    });
    if (!picker.textContent) picker.appendChild(node('span', 'students-empty', cls ? 'This class has no optional subjects.' : 'Choose a class first.'));
  }

  async function openForm(student) {
    const s = student || { id: '', name: '', class: classes[0] || '', takes: [] };
    formState = { isNew: !student, pickable: new Map(), level: levelOf(s) };   // a level with no class yet is kept until a class is chosen
    const f = el('students-form');
    f.textContent = '';
    f.hidden = false;
    const field = (label, input) => {
      const l = node('label', 'student-field');
      l.appendChild(node('span', 'student-field-name', label));
      l.appendChild(input);
      f.appendChild(l);
      return input;
    };
    const idIn = field('ID', node('input', 'student-id'));
    idIn.value = s.id;
    idIn.disabled = !!student;               // an ID can't change; remove and add instead
    const nameIn = field('Name', node('input', 'student-name'));
    nameIn.value = s.name;
    const clsSel = field('Class', node('select', 'student-class'));
    const known = classes.slice();
    if (s.class && known.indexOf(s.class) === -1) known.push(s.class);
    const blank = node('option', '', 'choose');
    blank.value = '';
    clsSel.appendChild(blank);
    known.forEach((c) => { const o = node('option', '', c); o.value = c; clsSel.appendChild(o); });
    clsSel.value = s.class || '';
    f.appendChild(node('div', 'student-picker'));
    const actions = node('div', 'student-form-actions');
    actions.appendChild(button('btn primary', student ? 'Save' : 'Add student', () => { save().catch(swallow); }));
    actions.appendChild(button('btn ghost', 'Cancel', closeForm));
    f.appendChild(actions);
    clsSel.addEventListener('change', () => { fillPicker(clsSel.value, collectTakes()).catch(swallow); });
    await fillPicker(s.class || '', s.takes);
    if (f.scrollIntoView) f.scrollIntoView({ block: 'nearest' });
  }

  async function edit(id) {
    const s = students.find((x) => x.id === id);
    if (!s) return;
    await openForm(s);
  }

  async function save() {
    if (!formState) return;
    const f = el('students-form');
    const record = { id: f.querySelector('.student-id').value.trim(), name: f.querySelector('.student-name').value.trim(),
      class: f.querySelector('.student-class').value, takes: collectTakes() };
    if (!record.class && formState.level) record.level = formState.level;
    if (formState.isNew) await add(record); else await update(record.id, record);
    closeForm();
  }

  // ---------- wiring ----------
  function importStudents() {
    if (window.showTab) window.showTab('project');       // the Intake drop is on the Project tab
    const target = el('drop') || el('intake');
    if (target && target.scrollIntoView) target.scrollIntoView({ behavior: 'smooth', block: 'center' });
  }

  function bind() {
    el('students-search').addEventListener('input', () => filter(el('students-search').value, klass));
    el('students-class').addEventListener('change', () => filter(query, el('students-class').value));
    el('students-add').addEventListener('click', () => { say(''); openForm(null).catch(swallow); });
    el('students-import').addEventListener('click', importStudents);
    el('students-clear').addEventListener('click', () => { removeAll().catch(swallow); });
    el('students-more').addEventListener('click', () => { shown += PAGE; render(); });
  }

  // Only the admin has the section: a department account is not shown it and no student route is called.
  const ready = api('/api/me').then((me) => {
    admin = !!me && me.role === 'admin';
    if (!admin) return;
    el('students').hidden = false;
    bind();
  }).catch(() => { admin = false; });

  document.addEventListener('plane:tab', (ev) => {
    tab = (ev.detail && ev.detail.key) || '';
    if (tab === 'requirements') load();                   // also catches a reading applied while elsewhere
  });
  // a students reading applied or undone (files.js) or any reading applied in the chat: reload while showing
  ['plane:readings', 'plane:students'].forEach((ev) => document.addEventListener(ev, () => { if (tab === 'requirements') load(); }));

  window.Students = {
    ready, load, loaded: () => loading,
    filter, add, edit, save, removeOne, removeAll, render,
  };
})();
