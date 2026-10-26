// The Classes card on the Requirements tab (spec docs/superpowers/specs/2026-10-20-csv-pack-design.md §3): per
// level the expected students, class size and naming; Propose, which forms the classes and suggests a teacher
// for each requirement; the classes with their sizes and students, at most 200 rows at a time; the requirements
// with a teacher picker and warnings each; Apply and Undo, each asked first in words that say what changes; a
// banner when a formed level's requirements or students have changed since. Admin only: /api/me says who is
// asking, a department account never has the card shown nor a classes route called. Every code, name, warning
// and teacher is set with textContent, never as HTML. Loads on the Requirements tab (shell.js's plane:tab
// event); opening the card only reads (GET), nothing is written until Save, Propose, a teacher, Apply or Undo,
// or an option picked for a deployment conflict and "Yes, do this" pressed (spec docs/superpowers/specs/
// 2026-10-25-deployment-file-design.md goal 3: the conflicts above the requirements, one block each, the
// recommended option first and checked; a settled one is a single line, "kept").
(function () {
  'use strict';

  const el = (id) => document.getElementById(id);
  const node = (tag, cls, text) => {
    const n = document.createElement(tag);
    if (cls) n.className = cls;
    if (text != null) n.textContent = text;
    return n;
  };
  const option = (value, text) => { const o = node('option', '', text); o.value = value; return o; };
  async function api(path, opts = {}) {
    const r = await fetch(path, { headers: { 'Content-Type': 'application/json' }, ...opts });
    const data = await r.json().catch(() => ({}));
    if (!r.ok) {
      const e = new Error(typeof data.detail === 'string' ? data.detail : (r.statusText || 'The request failed'));
      e.status = r.status;
      e.stale = (r.headers && r.headers.get('X-Stale')) || '';     // a stale pick: "version" or "facts"
      throw e;
    }
    return data;
  }
  const fmt = (n) => Number(n).toLocaleString('en-US');
  const plural = (n, one, many) => `${fmt(n)} ${n === 1 ? one : many}`;
  const swallow = () => {};                 // a failed click has already said why in the message line
  const enc = encodeURIComponent;
  const num = (v) => { const s = String(v == null ? '' : v).trim(); return /^\d+$/.test(s) ? Number(s) : s; };

  const PAGE = 200;                         // rows drawn at a time: a level can hold several hundred students
  const STALE = 'Requirements or students changed since these classes were formed — propose again';

  let admin = false, tab = '', generation = 0, loading = Promise.resolve();
  let levels = [];                          // GET /api/classes/levels' answer
  let staff = [];
  let level = '';                           // the level key showing
  let proposal = null;                      // the proposal's view (with names, for the page only)
  let dirty = false;                        // the level's fields edited and not saved
  let shown = PAGE, shownReqs = PAGE;
  let announcing = false;                   // our own plane:students event is not a reason to reload
  let settling = false;                     // a conflict's option is being sent: every "Yes, do this" waits
  let picks = {};                           // conflict id -> the option id picked and not yet sent

  const say = (text, cls) => {
    const m = el('classes-msg');
    m.textContent = text || '';
    m.className = 'classes-msg' + (cls ? ' ' + cls : '');
  };
  const current = () => levels.find((l) => l.level === level) || null;
  const levelName = () => { const l = current(); return l ? (l.name || l.level) : level; };
  // a Runs-with group (Mother Tongue) spans every class: its grouping code (3CHINA) says which group it is
  const runsWith = (q) => q.grouping && q.grouping !== 'class' && q.grouping !== (q.classes || []).join('+');
  const reqLabel = (q) => `${q.subject} · ${runsWith(q) ? q.grouping : (q.classes || []).join(' + ')}`;
  const studentsOf = (p) => (p.classes || []).reduce((n, c) => n + (c.students || []).length, 0);
  const staffName = (id) => {
    const who = staff.find((p) => p.id === id);
    return who && who.name && who.name !== id ? `${who.name} (${id})` : id;
  };
  const moreText = (rest) => (rest > 0 ? `Show ${fmt(Math.min(PAGE, rest))} more (${fmt(rest)} left)` : '');
  const head = (table, names) => {
    const tr = node('tr');
    names.forEach((h) => tr.appendChild(node('th', '', h)));
    const thead = node('thead');
    thead.appendChild(tr);
    table.appendChild(thead);
  };
  const cell = (tr, label, text, cls) => {
    const td = node('td', cls || '', text);
    td.setAttribute('data-label', label);
    tr.appendChild(td);
    return td;
  };

  // ---------- the level picker and its fields ----------
  function levelText(l) {
    const parts = [l.name || l.level];
    if (l.formed) parts.push(l.stale ? 'formed, changed since' : 'formed');
    else if (l.proposed) parts.push('proposed');
    return parts.join(' · ');
  }

  function fillLevels() {
    const sel = el('classes-level');
    sel.textContent = '';
    levels.forEach((l) => sel.appendChild(option(l.level, levelText(l))));
    if (!level || !current()) {
      const proposed = levels.find((l) => l.proposed);
      level = proposed ? proposed.level : (levels[0] ? levels[0].level : '');
    }
    sel.value = level;
  }

  function fillFields() {
    const l = current();
    if (!l) return;
    el('classes-expected').value = l.students_expected == null ? '' : String(l.students_expected);
    el('classes-size').value = l.class_size == null ? '' : String(l.class_size);
    el('classes-naming').value = l.naming || '';
    el('classes-count').value = proposal && proposal.count ? String(proposal.count) : '';
    dirty = false;
  }

  function renderLevel() {
    const l = current();
    el('classes-setup').hidden = !l;
    const stale = el('classes-stale');
    stale.hidden = !(l && l.stale);
    stale.textContent = l && l.stale ? STALE : '';
    if (!l) { el('classes-head').textContent = ''; return; }
    const formed = (l.classes || []).length;
    el('classes-head').textContent = `${l.name || l.level}` + (formed ? ` · ${plural(formed, 'class', 'classes')} formed` : '');
    el('classes-students').textContent = l.students
      ? `${plural(l.students, 'student', 'students')} of this level on the student list: the classes are formed from it.`
      : 'No students of this level on the student list: the classes are sized from the expected number.';
  }

  function fieldsBody() {
    const expected = String(el('classes-expected').value).trim();
    return { students_expected: expected === '' ? null : num(expected), class_size: num(el('classes-size').value),
      naming: String(el('classes-naming').value).trim() };
  }

  async function send(path, method, body) {
    try {
      return await api(path, { method, body: body ? JSON.stringify(body) : undefined });
    } catch (e) {
      say(e.message, 'bad');
      throw e;
    }
  }

  async function save() {
    if (!level) return;
    const answer = await send('/api/classes/levels/' + enc(level), 'PUT', fieldsBody());
    const i = levels.findIndex((l) => l.level === level);
    if (i !== -1) levels[i] = answer;
    dirty = false;
    renderLevel();
    say(proposal ? 'Saved. Propose again to form the classes from it.' : 'Saved.', 'ok');
  }

  // ---------- the proposal ----------
  function renderClasses() {
    const table = el('classes-classes');
    table.textContent = '';
    const p = proposal;
    if (!(p.classes || []).length) {
      table.appendChild(node('caption', 'classes-empty', 'No classes: give the expected number of students, or import the student list.'));
      return;
    }
    head(table, ['Class', 'Students']);
    const body = node('tbody');
    p.classes.forEach((c) => {
      const tr = node('tr');
      cell(tr, 'Class', c.code, 'classes-code');
      cell(tr, 'Students', fmt(c.size || 0));
      body.appendChild(tr);
    });
    table.appendChild(body);
  }

  function renderRoster() {
    const table = el('classes-roster');
    table.textContent = '';
    const more = el('classes-more');
    const lines = [];
    (proposal.classes || []).forEach((c) => (c.students || []).forEach((sid) => lines.push([c.code, sid])));
    if (!lines.length) {
      if ((proposal.classes || []).length) {
        table.appendChild(node('caption', 'classes-empty', 'No student list for this level: the classes are sized from the expected number.'));
      }
      more.hidden = true;
      more.textContent = '';
      return;
    }
    head(table, ['Class', 'ID', 'Name']);
    const body = node('tbody');
    const names = proposal.names || {};
    lines.slice(0, shown).forEach(([code, sid]) => {
      const tr = node('tr');
      cell(tr, 'Class', code, 'classes-code');
      cell(tr, 'ID', sid, 'id');
      cell(tr, 'Name', names[sid] || '', 'classes-name');
      body.appendChild(tr);
    });
    table.appendChild(body);
    const rest = lines.length - shown;
    more.hidden = rest <= 0;
    more.textContent = moreText(rest);
  }

  function teacherPicker(q, i) {
    const sel = node('select', 'classes-teacher-pick');
    sel.setAttribute('aria-label', 'Teacher for ' + reqLabel(q));
    sel.appendChild(option('', 'no teacher'));
    const people = staff.filter((p) => !q.dept || p.dept === q.dept);
    people.forEach((p) => sel.appendChild(option(p.id, p.name || p.id)));
    if (q.teacher && !people.some((p) => p.id === q.teacher)) {     // one of another department, as named
      const who = staff.find((p) => p.id === q.teacher);
      sel.appendChild(option(q.teacher, (who && who.name) || q.teacher));
    }
    sel.value = q.teacher || '';
    sel.disabled = !proposal.can_apply;
    sel.addEventListener('change', () => { setTeacher(i, sel.value).catch(swallow); });
    return sel;
  }

  function renderReqs() {
    const table = el('classes-reqs');
    table.textContent = '';
    const more = el('classes-reqs-more');
    const reqs = proposal.requirements || [];
    if (!reqs.length) {
      if ((proposal.classes || []).length) {
        table.appendChild(node('caption', 'classes-empty', 'No Class or Combined subjects for this level in requirements.csv.'));
      }
      more.hidden = true;
      more.textContent = '';
      return;
    }
    head(table, ['Requirement', 'Periods', 'Students', 'Teacher', 'Warnings']);
    const body = node('tbody');
    reqs.slice(0, shownReqs).forEach((q, i) => {
      const tr = node('tr', (q.warnings || []).length ? 'warned' : '');
      cell(tr, 'Requirement', reqLabel(q), 'classes-req');
      cell(tr, 'Periods', fmt(q.periods || 0));
      cell(tr, 'Students', fmt(q.size || 0));
      const td = cell(tr, 'Teacher', null, 'classes-teacher');
      td.appendChild(teacherPicker(q, i));
      if (q.teacher && q.suggested) td.appendChild(node('span', 'classes-suggested', '(suggested)'));
      const notes = cell(tr, 'Warnings', null, 'classes-notes');
      if ((q.warnings || []).length) {
        const list = node('ul', 'classes-warning-list');
        q.warnings.forEach((w) => list.appendChild(node('li', 'classes-warning', w)));
        notes.appendChild(list);
      }
      body.appendChild(tr);
    });
    table.appendChild(body);
    const rest = reqs.length - shownReqs;
    more.hidden = rest <= 0;
    more.textContent = moreText(rest);
  }

  // ---------- the deployment conflicts ----------
  function conflictBlock(c, n) {
    const options = c.options || [];
    const block = node('fieldset', 'classes-conflict');
    block.appendChild(node('legend', 'classes-conflict-text', c.text));
    const chosen = options.some((o) => o.id === picks[c.id]) ? picks[c.id] : (options[0] || {}).id;
    options.forEach((o) => {
      const label = node('label', 'classes-option');
      const radio = node('input');
      radio.type = 'radio';
      radio.name = 'classes-conflict-' + n;
      radio.value = o.id;
      radio.checked = o.id === chosen;
      radio.addEventListener('change', () => { picks[c.id] = o.id; });
      label.appendChild(radio);
      label.appendChild(node('span', 'classes-option-text', o.text));
      if (o.recommended) {
        label.appendChild(document.createTextNode(' '));                // the name reads "… recommended", not "…recommended"
        label.appendChild(node('span', 'classes-recommended', 'recommended'));
      }
      block.appendChild(label);
    });
    const yes = node('button', 'btn primary', 'Yes, do this');
    yes.type = 'button';
    yes.disabled = settling;
    yes.addEventListener('click', () => {
      const on = Array.from(block.querySelectorAll('input')).find((r) => r.checked);
      settle(options.find((o) => on && o.id === on.value) || options[0], c).catch(swallow);
    });
    block.appendChild(yes);
    return block;
  }

  function renderConflicts() {
    const box = el('classes-conflicts');
    box.textContent = '';
    const list = (proposal && proposal.conflicts) || [];
    box.hidden = !list.length;
    if (!list.length) return;
    box.appendChild(node('h3', 'classes-step', 'Conflicts'));
    list.forEach((c, n) => {
      if (c.settled) box.appendChild(node('p', 'classes-conflict-kept', `${c.text} — kept`));
      else if ((c.options || []).length) box.appendChild(conflictBlock(c, n));
    });
  }

  // The school's yes to one option, sent with the version of the proposal it was picked from, so a yes sent twice
  // (or from an older page) is refused. Nothing is sent before this; the answer is the level's new proposal. A 409
  // (the conflict changed, or was already dealt with) shows the server's words and then looks again, so a stale
  // view never leaves the school stuck: stale by "version" (the stored proposal is current, this page's copy is
  // older, say another tab changed a teacher) reads the stored proposal again; otherwise ("facts": the level's
  // requirements or students changed) it proposes the level again with its count, as Propose does, keeping the
  // teachers the school chose by hand (keep).
  async function settle(o, c) {
    if (settling || !level || !proposal) return;
    const mine = level;
    settling = true;
    renderConflicts();                       // the buttons go grey while the request runs: a double click sends once
    try {
      let answer, done = true;
      try {
        answer = await api('/api/classes/conflicts/apply', { method: 'POST',
          body: JSON.stringify({ level: mine, conflict: c.id, option: o.id, version: proposal.version }) });
      } catch (e) {
        say(e.message, 'bad');
        if (e.status !== 409) return;
        done = false;
        if (e.stale === 'version') {
          answer = await send('/api/classes/proposal?level=' + enc(mine), 'GET');
        } else {
          const count = String(el('classes-count').value).trim();
          answer = await send('/api/classes/propose', 'POST',
            { level: mine, count: count === '' ? null : num(count), keep: true });
        }
      }
      if (mine !== level) return;            // another level was opened meanwhile: its page is not this answer's
      proposal = answer;
      picks = {};
      if (done) say(`Done: ${o.text}`, 'ok');
    } finally {
      settling = false;
      if (mine === level) {
        el('classes-count').value = proposal && proposal.count ? String(proposal.count) : '';
        renderProposal();
      } else {
        renderConflicts();                   // the other level's buttons were drawn grey while this one ran
      }
    }
  }

  function renderProposal() {
    const box = el('classes-proposal');
    const p = proposal;
    box.hidden = !p;
    if (!p) {
      ['classes-summary', 'classes-warnings', 'classes-conflicts', 'classes-classes', 'classes-roster', 'classes-reqs'].forEach((id) => { el(id).textContent = ''; });
      el('classes-conflicts').hidden = true;
      el('classes-more').hidden = true;
      el('classes-reqs-more').hidden = true;
    } else {
      const classes = (p.classes || []).length, line = [plural(classes, 'class', 'classes'),
        plural(studentsOf(p), 'student', 'students'), plural((p.requirements || []).length, 'requirement', 'requirements')];
      if (!p.can_apply && classes) line.push('applied');
      el('classes-summary').textContent = line.join(' · ');
      const warnings = el('classes-warnings');
      warnings.textContent = '';
      (p.warnings || []).forEach((w) => warnings.appendChild(node('li', 'classes-warning', w)));
      renderConflicts();
      renderClasses();
      renderRoster();
      renderReqs();
    }
    const apply = el('classes-apply');
    apply.hidden = !p;
    apply.disabled = !(p && p.can_apply);
    el('classes-undo').hidden = !canUndo();
  }

  function canUndo() {
    if (!level) return false;
    if (proposal && proposal.can_undo) return true;
    const l = current();
    return !!(l && l.can_undo);
  }

  function render() {
    renderLevel();
    renderProposal();
  }

  async function propose() {
    if (!level) return;
    if (dirty) await save();
    const count = String(el('classes-count').value).trim();
    const answer = await send('/api/classes/propose', 'POST', { level, count: count === '' ? null : num(count) });
    proposal = answer;
    shown = PAGE;
    shownReqs = PAGE;
    const l = current();
    if (l) l.proposed = true;
    render();
    say(`Proposed ${plural((answer.classes || []).length, 'class', 'classes')} and ${plural((answer.requirements || []).length, 'requirement', 'requirements')} for ${levelName()}. Nothing is written until Apply.`, 'ok');
  }

  async function setTeacher(index, teacher) {
    if (!level || !proposal) return;
    try {
      proposal = await send('/api/classes/teacher', 'PUT', { level, index: Number(index), teacher: teacher || null });
    } finally {
      renderProposal();                      // a refusal puts the picker back as it was
    }
  }

  function announce() {
    if (typeof window.loadPlan === 'function') window.loadPlan();      // the plan tables and the board follow
    announcing = true;
    try { document.dispatchEvent(new CustomEvent('plane:students')); } finally { announcing = false; }
  }

  async function reloadLevels() {
    try {
      const answer = await api('/api/classes/levels');
      levels = answer.levels || [];
      fillLevels();
    } catch (e) { /* Undo and the banner follow the last word until the next load */ }
    render();
  }

  async function apply() {
    if (!level || !proposal || !proposal.can_apply) return;
    const p = proposal, name = levelName();
    const classes = plural((p.classes || []).length, 'class', 'classes');
    const reqs = plural((p.requirements || []).length, 'requirement', 'requirements');
    if (!confirm(`Apply writes ${classes} and ${reqs} for ${name} and puts ${plural(studentsOf(p), 'student', 'students')} in their classes. It replaces any classes this level got from an earlier Apply. Undo restores the plan and the students.`)) return;
    const answer = await send('/api/classes/apply', 'POST', { level });
    const before = p;
    proposal = answer;
    renderProposal();
    const dropped = answer.teachers_dropped || [];
    const parts = [`Applied: ${classes} and ${reqs} for ${name}.`];
    dropped.forEach((d) => {
      const q = (before.requirements || [])[d.index];
      const label = q ? reqLabel(q) : reqLabel({ subject: d.subject, classes: d.classes || [] });
      parts.push(`${label} has no teacher: ${staffName(d.teacher)} has left or changed department.`);
    });
    say(parts.join(' '), dropped.length ? 'bad' : 'ok');
    announce();
    await reloadLevels();
  }

  async function undo() {
    if (!canUndo()) return;
    if (!confirm('Undo removes these classes and requirements and restores the students as they were.')) return;
    await send('/api/classes/undo', 'POST', { level });
    say('Undone: the plan and the students are as they were.', 'ok');
    announce();
    const mine = generation;
    await reloadLevels();
    if (mine === generation) await loadProposal(mine);
  }

  // ---------- loading ----------
  async function loadProposal(mine) {
    const l = current();
    let answer = null;
    if (l && l.proposed) {                   // read as stored, with names: opening writes nothing
      try {
        answer = await api('/api/classes/proposal?level=' + enc(level));
      } catch (e) {
        if (mine === generation && e.status !== 404) say(e.message, 'bad');
      }
    }
    if (mine !== generation) return;
    proposal = answer;
    renderProposal();
  }

  async function openLevel(mine) {
    proposal = null;
    picks = {};
    shown = PAGE;
    shownReqs = PAGE;
    render();                                // the last level's proposal goes at once, even if this load fails
    fillFields();
    if (!level) return;
    await loadProposal(mine);
    if (mine === generation) el('classes-count').value = proposal && proposal.count ? String(proposal.count) : '';
  }

  async function doLoad() {
    const mine = ++generation;
    try {
      const [answer, plan] = await Promise.all([api('/api/classes/levels'), api('/api/plan').catch(() => null)]);
      if (mine !== generation) return;
      levels = answer.levels || [];
      const p = (plan && plan.plan) || {};
      staff = (p.staff || []).map((s) => ({ id: s.id, name: s.name || s.id, dept: s.dept || '' }));
      fillLevels();
      if (!level) {
        proposal = null;
        render();
        say('Give requirements.csv to list the levels, then form their classes here.');
        return;
      }
      await openLevel(mine);
    } catch (e) {
      if (mine === generation) say(e.message, 'bad');
    }
  }
  function load() {                          // `ready` is settled first, so a department account never loads
    loading = ready.then(() => (admin ? doLoad() : null));
    return loading;
  }
  // The tab shown again, or a reading applied: everything reloads, unless the fields have unsaved edits.
  function refresh() {
    if (dirty) return loading;
    return load();
  }

  function selectLevel(key) {
    loading = ready.then(async () => {
      if (!admin) return;
      const mine = ++generation;
      level = key;
      say('');
      fillLevels();
      try {
        await openLevel(mine);
      } catch (e) {
        if (mine === generation) say(e.message, 'bad');
      }
    });
    return loading;
  }

  // ---------- wiring ----------
  function bind() {
    const sel = el('classes-level');
    sel.addEventListener('change', () => {
      if (dirty && !confirm('Leave this level without saving its changes?')) { sel.value = level; return; }
      selectLevel(sel.value).catch(swallow);
    });
    ['classes-expected', 'classes-size', 'classes-naming'].forEach((id) => {
      el(id).addEventListener('input', () => { dirty = true; });
    });
    el('classes-save').addEventListener('click', () => { save().catch(swallow); });
    el('classes-propose').addEventListener('click', () => { propose().catch(swallow); });
    el('classes-more').addEventListener('click', () => { shown += PAGE; renderRoster(); });
    el('classes-reqs-more').addEventListener('click', () => { shownReqs += PAGE; renderReqs(); });
    el('classes-apply').addEventListener('click', () => { apply().catch(swallow); });
    el('classes-undo').addEventListener('click', () => { undo().catch(swallow); });
  }

  // Only the admin has the card: a department account is not shown it and no classes route is called.
  const ready = api('/api/me').then((me) => {
    admin = !!me && me.role === 'admin';
    if (!admin) return;
    el('form-classes').hidden = false;
    bind();
  }).catch(() => { admin = false; });

  document.addEventListener('plane:tab', (ev) => {
    tab = (ev.detail && ev.detail.key) || '';
    if (tab === 'requirements') refresh();
  });
  // a requirements.csv or a student list read, or option blocks applied: reload while showing
  ['plane:readings', 'plane:students'].forEach((name) => document.addEventListener(name, () => {
    if (!announcing && tab === 'requirements') refresh();
  }));

  const self = {
    ready, load, loaded: () => loading,
    selectLevel, save, propose, setTeacher, apply, undo,
  };
  window.ClassesCard = self;
})();
