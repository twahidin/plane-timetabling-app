// The Option blocks card on the Requirements tab (spec docs/superpowers/specs/2026-10-17-option-blocks-design.md
// §1): per year group (a level, or chosen classes) the subject combinations with their places, the limits and
// each subject's periods and room kind; the checks; placing the students by merit and choice, with overrides;
// building the blocks on the engine (progress, Stop, a time limit); the proposal, one column per block, with a
// teacher picker per set; Apply and Undo, each asked first in words that say what changes. Admin only:
// /api/me says who is asking, a department account never has the card shown nor a blocks route called.
// Every code, name, line, reason, warning and teacher is set with textContent, never as HTML. Loads on the
// Requirements tab (shell.js's plane:tab event); the run is polled only while that tab and the card show.
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
  const option = (value, text) => { const o = node('option', '', text); o.value = value; return o; };
  async function api(path, opts = {}) {
    const r = await fetch(path, { headers: { 'Content-Type': 'application/json' }, ...opts });
    const data = await r.json().catch(() => ({}));
    if (!r.ok) {
      const e = new Error(typeof data.detail === 'string' ? data.detail : (r.statusText || 'The request failed'));
      e.status = r.status;
      throw e;
    }
    return data;
  }
  const fmt = (n) => Number(n).toLocaleString('en-US');
  const plural = (n, one, many) => `${fmt(n)} ${n === 1 ? one : many}`;
  const swallow = () => {};                 // a failed click has already said why in the message line
  const enc = encodeURIComponent;
  const natural = (a, b) => a.localeCompare(b, 'en', { numeric: true, sensitivity: 'base' }) || (a < b ? -1 : a > b ? 1 : 0);
  const num = (v) => { const s = String(v == null ? '' : v).trim(); return /^\d+$/.test(s) ? Number(s) : s; };
  const splitCodes = (text) => Array.from(new Set(String(text).split(',').map((s) => s.trim()).filter(Boolean)));

  const PAGE = 200;                         // placement lines drawn at a time: a year can hold a thousand or more
  const DONE = ['optimal', 'feasible'];
  const ENDED = DONE.concat(['infeasible', 'cancelled', 'failed']);
  const CLASSES = 'classes';                // the picker's "chosen classes" entry
  const TIME_MIN = 10, TIME_MAX = 300;
  const POLL_TRIES = 3;                     // failed polls in a row before the card says so and stops

  let admin = false, tab = '', generation = 0, loading = Promise.resolve();
  let years = { levels: [], stored: [] };
  let staff = [], planClasses = [], planSubjects = [];
  let year = '';                            // the year key showing
  let data = null;                          // GET / PUT /api/blocks/setup's answer for that year
  let draft = null, dirty = false;          // the combinations, limits and subjects as edited
  let checks = [];
  let placement = null, shown = PAGE;       // POST /api/blocks/place's answer (with names, for the page only)
  let run = null, startedHere = 0;          // GET /api/blocks/run's answer; null when there is none
  let pollTimer = null;
  let announcing = false;                   // our own plane:students event is not a reason to reload

  const say = (text, cls) => {
    const m = el('blocks-msg');
    m.textContent = text || '';
    m.className = 'blocks-msg' + (cls ? ' ' + cls : '');
  };
  const running = () => !!run && !!run.status && ENDED.indexOf(run.status) === -1;
  const yearLabel = (key) => {
    const [kind, rest] = [key.split(':')[0], key.slice(key.indexOf(':') + 1)];
    if (kind === 'level') return 'Level ' + rest;
    return rest.split(',').join(', ');
  };
  const overrides = () => (data && data.setup && data.setup.overrides) || {};
  const savedCodes = () => ((data && data.setup && data.setup.combinations) || []).map((c) => c.code);

  // ---------- the year group picker ----------
  function fillYears() {
    const sel = el('blocks-year');
    sel.textContent = '';
    const keys = [];
    years.levels.forEach((l) => { keys.push(l.key); sel.appendChild(option(l.key, `Level ${l.level} (${(l.classes || []).join(', ')})`)); });
    years.stored.filter((s) => keys.indexOf(s.key) === -1).forEach((s) => {
      keys.push(s.key);
      sel.appendChild(option(s.key, s.key.indexOf('level:') === 0 ? yearLabel(s.key) : `${yearLabel(s.key)} (chosen classes)`));
    });
    if (year && year !== CLASSES && keys.indexOf(year) === -1) { keys.push(year); sel.appendChild(option(year, `${yearLabel(year)} (chosen classes)`)); }
    sel.appendChild(option(CLASSES, 'Chosen classes…'));
    if (!year || keys.indexOf(year) === -1) {
      const stored = years.stored.find((s) => keys.indexOf(s.key) !== -1);
      year = stored ? stored.key : (keys[0] || '');
    }
    sel.value = year || CLASSES;
    el('blocks-classes-pick').hidden = !!year;
  }

  function knownClasses() {
    const all = planClasses.slice();
    years.levels.forEach((l) => (l.classes || []).forEach((c) => { if (all.indexOf(c) === -1) all.push(c); }));
    return all;
  }

  // "3b, 3A" -> the level they fill ("level:3") or "classes:3A,3B", as the server's year_key would make it
  function useClasses() {
    const known = knownClasses();
    const given = el('blocks-classes').value.split(/[,\s]+/).map((s) => s.trim()).filter(Boolean);
    const picked = [];
    for (const g of given) {
      const code = known.find((c) => c === g) || known.find((c) => c.toLowerCase() === g.toLowerCase());
      if (!code) { say(`The class ${g} isn't in the plan.`, 'bad'); return Promise.resolve(); }
      if (picked.indexOf(code) === -1) picked.push(code);
    }
    if (!picked.length) { say('Give one or more class codes.', 'bad'); return Promise.resolve(); }
    picked.sort();
    const level = years.levels.find((l) => (l.classes || []).length === picked.length
      && picked.every((c) => l.classes.indexOf(c) !== -1));
    return selectYear(level ? level.key : 'classes:' + picked.join(','));
  }

  // ---------- the setup editor ----------
  function resetDraft() {
    const s = (data && data.setup) || {};
    draft = {
      combinations: (s.combinations || []).map((c) => ({ code: c.code, name: c.name || '', subjects: (c.subjects || []).slice(), places: c.places })),
      limits: Object.assign({}, s.limits || {}),
      subjects: JSON.parse(JSON.stringify(s.subjects || {})),
    };
    dirty = false;
  }
  function markDirty() {
    dirty = true;
    updateButtons();
  }
  function subjectCodes() {
    const out = [];
    draft.combinations.forEach((c) => c.subjects.forEach((s) => { if (out.indexOf(s) === -1) out.push(s); }));
    return out;
  }

  function renderCombinations() {
    const table = el('blocks-combinations');
    table.textContent = '';
    if (!draft.combinations.length) {
      table.appendChild(node('caption', 'blocks-empty', 'No combinations yet. Add one, or fill the Combinations sheet of the workbook.'));
      return;
    }
    const head = node('tr');
    // each column named (blocks-c-*) so the desktop layout can keep Code, Places and Remove narrow
    const COLS = ['code', 'name', 'subjects', 'places', 'act'];
    ['Code', 'Name', 'Subjects (codes, separated by commas)', 'Places', ''].forEach((h, i) => head.appendChild(node('th', 'blocks-c-' + COLS[i], h)));
    const thead = node('thead');
    thead.appendChild(head);
    table.appendChild(thead);
    const body = node('tbody');
    draft.combinations.forEach((c, i) => {
      const tr = node('tr');
      const cell = (label, input, col) => {
        const td = node('td', 'blocks-c-' + col);
        td.setAttribute('data-label', label);
        td.appendChild(input);
        tr.appendChild(td);
        return td;
      };
      const field = (cls, value, label, onInput, type) => {
        const input = node('input', cls);
        if (type) input.type = type;
        input.value = value == null ? '' : String(value);
        input.setAttribute('aria-label', label);
        input.addEventListener('input', () => { onInput(input.value); markDirty(); });
        return input;
      };
      const tag = () => c.code || `row ${i + 1}`;
      cell('Code', field('blocks-code', c.code, 'Combination code', (v) => { c.code = v; }), 'code');
      cell('Name', field('blocks-name', c.name, `Name of ${tag()}`, (v) => { c.name = v; }), 'name');
      const subj = field('blocks-subj', c.subjects.join(', '), `Subjects of ${tag()}`, (v) => { c.subjects = splitCodes(v); });
      subj.addEventListener('change', () => { c.subjects = splitCodes(subj.value); markDirty(); renderSubjects(); });
      const wrap = node('div', 'blocks-subj-cell');        // the input, then the plan's subjects under it
      wrap.appendChild(subj);
      cell('Subjects', wrap, 'subjects');
      if (planSubjects.length) {                 // the plan's subjects, or type a code
        const add = node('select', 'blocks-subj-add');
        add.setAttribute('aria-label', `Add a subject of the plan to ${tag()}`);
        add.appendChild(option('', '+ plan subject'));
        planSubjects.forEach((s) => add.appendChild(option(s, s)));
        add.value = '';
        add.addEventListener('change', () => {
          if (add.value && c.subjects.indexOf(add.value) === -1) c.subjects.push(add.value);
          subj.value = c.subjects.join(', ');
          add.value = '';
          markDirty();
          renderSubjects();
        });
        wrap.appendChild(add);
      }
      cell('Places', field('blocks-places', c.places, `Places in ${tag()}`, (v) => { c.places = v; }, 'number'), 'places');
      const act = node('td', 'blocks-c-act blocks-row-actions');
      const remove = button('btn ghost', 'Remove', () => {
        draft.combinations.splice(draft.combinations.indexOf(c), 1);
        markDirty();
        renderCombinations();
        renderSubjects();
      });
      remove.setAttribute('aria-label', 'Remove ' + tag());
      act.appendChild(remove);
      tr.appendChild(act);
      body.appendChild(tr);
    });
    table.appendChild(body);
  }

  function renderLimits() {
    [['blocks-n', 'blocks'], ['blocks-min', 'min_size'], ['blocks-max', 'max_size']].forEach(([id, k]) => {
      const v = draft.limits[k];
      el(id).value = v == null ? '' : String(v);
    });
  }

  // Each combination subject: the school's own value in the box, the plan's (what applies when blank) as its hint
  function renderSubjects() {
    const table = el('blocks-subjects');
    table.textContent = '';
    const codes = subjectCodes();
    if (!codes.length) return;
    const head = node('tr');
    ['Subject', 'Name', 'Department', 'Periods a cycle', 'Room kind'].forEach((h) => head.appendChild(node('th', '', h)));
    const thead = node('thead');
    thead.appendChild(head);
    table.appendChild(thead);
    const body = node('tbody');
    const meta = (data && data.subjects) || {};
    codes.forEach((code) => {
      const tr = node('tr');
      const first = node('td', 'blocks-subject-code', code);
      first.setAttribute('data-label', 'Subject');
      tr.appendChild(first);
      const given = draft.subjects[code] || {};
      const plan = meta[code] || {};
      [['name', 'Name', 'blocks-sname'], ['dept', 'Department', 'blocks-dept'], ['periods', 'Periods a cycle', 'blocks-periods'],
        ['room_kind', 'Room kind', 'blocks-room']].forEach(([k, label, cls]) => {
        const td = node('td');
        td.setAttribute('data-label', label);
        const input = node('input', cls);
        if (k === 'periods') input.type = 'number';
        input.value = given[k] == null ? '' : String(given[k]);
        if (plan[k] != null && plan[k] !== '') input.setAttribute('placeholder', String(plan[k]));
        input.setAttribute('aria-label', `${label} of ${code}`);
        input.addEventListener('input', () => {
          draft.subjects[code] = Object.assign({}, draft.subjects[code] || {}, { [k]: input.value });
          markDirty();
        });
        td.appendChild(input);
        tr.appendChild(td);
      });
      body.appendChild(tr);
    });
    table.appendChild(body);
  }

  function setupBody(overrideMap) {
    const codes = draft.combinations.map((c) => String(c.code || '').trim().toLowerCase());
    const kept = {};
    let dropped = 0;
    Object.keys(overrideMap).forEach((sid) => {
      if (codes.indexOf(String(overrideMap[sid]).toLowerCase()) !== -1) kept[sid] = overrideMap[sid];
      else dropped++;
    });
    const subjects = {};
    subjectCodes().forEach((code) => {
      const given = draft.subjects[code] || {};
      const item = {};
      ['name', 'dept', 'room_kind'].forEach((k) => { const v = String(given[k] == null ? '' : given[k]).trim(); if (v) item[k] = v; });
      const p = String(given.periods == null ? '' : given.periods).trim();
      if (p) item.periods = num(p);
      if (Object.keys(item).length) subjects[code] = item;
    });
    return {
      body: {
        year,
        combinations: draft.combinations.map((c) => ({ code: String(c.code || '').trim(), name: String(c.name || '').trim(),
          subjects: c.subjects.slice(), places: num(c.places) })),
        limits: { blocks: num(draft.limits.blocks), min_size: num(draft.limits.min_size), max_size: num(draft.limits.max_size) },
        subjects,
        overrides: kept,
      },
      dropped,
    };
  }

  async function send(path, method, body) {
    try {
      return await api(path, { method, body: body ? JSON.stringify(body) : undefined });
    } catch (e) {
      say(e.message, 'bad');
      throw e;
    }
  }

  // keepShown: an override's save and re-place keep the rows drawn so far (the student stays in view)
  async function put(overrideMap, keepShown) {
    const { body, dropped } = setupBody(overrideMap);
    const answer = await send('/api/blocks/setup', 'PUT', body);
    data = answer;
    resetDraft();
    checks = answer.checks || [];
    placement = null;                        // the server drops a placement made before: place again
    if (!keepShown) shown = PAGE;
    renderSetup();
    return dropped;
  }

  async function save() {
    if (!year || !draft) return;
    const had = !!placement;
    const dropped = await put(overrides());
    const parts = ['Saved.'];
    if (dropped) parts.push(`${plural(dropped, 'override', 'overrides')} to a removed combination cleared.`);
    if (had) parts.push('Place the students again.');
    say(parts.join(' '), 'ok');
  }

  // ---------- checks and placing ----------
  function renderChecks() {
    const box = el('blocks-checks');
    box.textContent = '';
    if (!checks.length) {
      box.appendChild(node('p', 'blocks-empty', placement ? 'No problems found.' : 'Place the students to check their merit ranks and choices.'));
      return;
    }
    const list = node('ul', 'blocks-check-list');
    checks.forEach((c) => {
      const li = node('li', 'blocks-check ' + (c.blocking ? 'blocking' : 'warning'));
      li.appendChild(node('span', 'blocks-check-tag', c.blocking ? 'Fix' : 'Note'));
      li.appendChild(node('span', 'blocks-check-text', c.text));
      list.appendChild(li);
    });
    box.appendChild(list);
    if (checks.some((c) => c.blocking)) box.appendChild(node('p', 'blocks-hint', 'Fix the checks marked Fix before building the blocks.'));
  }

  function placementIds() {
    if (!placement) return [];
    const ids = Object.keys(placement.names || {});
    Object.keys(placement.lines || {}).forEach((id) => { if (!(id in (placement.names || {}))) ids.push(id); });
    return ids.sort(natural);
  }

  function overridePicker(sid) {
    const sel = node('select', 'blocks-override');
    const fixed = overrides()[sid] || '';
    sel.setAttribute('aria-label', 'Override the placement of ' + sid);
    sel.appendChild(option('', fixed ? 'clear' : 'by merit'));
    const codes = savedCodes();
    codes.forEach((code) => sel.appendChild(option(code, code)));
    if (fixed && codes.indexOf(fixed) === -1) sel.appendChild(option(fixed, fixed));
    sel.value = fixed;
    sel.addEventListener('change', () => { setOverride(sid, sel.value).catch(swallow); });
    return sel;
  }

  function renderPlacement() {
    const table = el('blocks-lines');
    table.textContent = '';
    const more = el('blocks-more');
    const unplaced = el('blocks-unplaced');
    unplaced.textContent = '';
    const ids = placementIds();
    if (!placement) {
      table.appendChild(node('caption', 'blocks-empty', 'Not placed yet.'));
      more.hidden = true;
      more.textContent = '';
      return;
    }
    if (!ids.length) {
      table.appendChild(node('caption', 'blocks-empty', 'This year group has no students. Import them on the Students list above.'));
    } else {
      const head = node('tr');
      ['ID', 'Name', 'Placement', 'Override'].forEach((h) => head.appendChild(node('th', '', h)));
      const thead = node('thead');
      thead.appendChild(head);
      table.appendChild(thead);
      const body = node('tbody');
      const fixed = overrides();
      ids.slice(0, shown).forEach((sid) => {
        const tr = node('tr', fixed[sid] ? 'overridden' : '');
        [['ID', sid, 'id'], ['Name', (placement.names || {})[sid] || '', 'blocks-student-name'],
          ['Placement', (placement.lines || {})[sid] || '', 'blocks-line']].forEach(([label, text, cls]) => {
          const td = node('td', cls, text);
          td.setAttribute('data-label', label);
          tr.appendChild(td);
        });
        const td = node('td', 'blocks-override-cell');
        td.setAttribute('data-label', 'Override');
        td.appendChild(overridePicker(sid));
        tr.appendChild(td);
        body.appendChild(tr);
      });
      table.appendChild(body);
    }
    const rest = ids.length - shown;
    more.hidden = rest <= 0;
    more.textContent = rest > 0 ? `Show ${fmt(Math.min(PAGE, rest))} more (${fmt(rest)} left)` : '';
    const out = placement.unplaced || [];
    if (out.length) {
      unplaced.appendChild(node('h4', '', `Unplaced (${fmt(out.length)}): none of their choices had room`));
      const list = node('ul', 'blocks-unplaced-list');
      out.slice().sort(natural).slice(0, PAGE).forEach((sid) => {
        const name = (placement.names || {})[sid];
        list.appendChild(node('li', '', name ? `${sid} · ${name}` : sid));
      });
      if (out.length > PAGE) list.appendChild(node('li', 'blocks-empty', `and ${fmt(out.length - PAGE)} more`));
      unplaced.appendChild(list);
      unplaced.appendChild(node('p', 'blocks-hint', 'Place an unplaced student by hand with the Override picker in their line.'));
    }
  }

  // The Place button (and an override, with keepShown): the only callers of POST place, the one that writes
  async function placeStudents(keepShown) {
    if (!year || !draft) return;
    if (dirty) await save();
    const answer = await send('/api/blocks/place', 'POST', { year });
    placement = answer;
    checks = answer.checks || [];
    if (!keepShown) shown = PAGE;
    renderPlacement();
    renderChecks();
    updateButtons();
    say(`Placed ${plural(Object.keys(answer.placed || {}).length, 'student', 'students')}; ${fmt((answer.unplaced || []).length)} unplaced.`, 'ok');
  }

  async function setOverride(sid, code) {
    if (!year || !draft) return;
    const next = Object.assign({}, overrides());
    if (code) next[sid] = code; else delete next[sid];
    await put(next, true);
    await placeStudents(true);
    say(code ? `${sid} is placed in ${code} by hand.` : `${sid} is placed by merit and choice again.`, 'ok');
  }

  const place = () => placeStudents(false);

  // ---------- building, the proposal, Apply and Undo ----------
  function staffFor(subject) {
    const dept = ((data && data.subjects && data.subjects[subject]) || {}).dept;
    return staff.filter((p) => !dept || p.dept === dept);
  }

  function setCard(s) {
    const card = node('div', 'blocks-set');
    card.appendChild(node('div', 'blocks-set-name', `${s.name} · ${plural(s.size, 'student', 'students')}`));
    const label = node('label', 'blocks-teacher');
    label.appendChild(node('span', 'blocks-field-name', 'Teacher'));
    const sel = node('select', 'blocks-teacher-pick');
    sel.setAttribute('aria-label', 'Teacher for ' + s.name);
    sel.appendChild(option('', 'no teacher'));
    const people = staffFor(s.subject);
    people.forEach((p) => sel.appendChild(option(p.id, p.name || p.id)));
    if (s.teacher && !people.some((p) => p.id === s.teacher)) sel.appendChild(option(s.teacher, s.teacher_name || s.teacher));
    sel.value = s.teacher || '';
    sel.disabled = !run || !run.can_apply;
    sel.addEventListener('change', () => { setTeacher(s.subject, s.set, sel.value).catch(swallow); });
    label.appendChild(sel);
    card.appendChild(label);
    if (s.teacher && s.suggested) card.appendChild(node('span', 'blocks-suggested', '(suggested)'));
    if ((s.warnings || []).length) {
      const list = node('ul', 'blocks-warnings');
      s.warnings.forEach((w) => list.appendChild(node('li', 'blocks-warning', w)));
      card.appendChild(list);
    }
    return card;
  }

  function renderProposal() {
    const box = el('blocks-proposal');
    box.textContent = '';
    const p = run && run.proposal;
    if (!p) return;
    const sum = p.summary || {};
    const line = [plural(sum.sets || 0, 'set', 'sets'), plural(sum.students || 0, 'student', 'students'), `${fmt(sum.unplaced || 0)} unplaced`];
    if (!run.can_apply) line.push('applied');
    box.appendChild(node('p', 'blocks-summary', line.join(' · ')));
    const cols = node('div', 'blocks-columns');
    (p.blocks || []).forEach((column, i) => {
      const col = node('div', 'blocks-column');
      col.appendChild(node('h4', '', `Block ${i + 1}`));
      if (!column.length) col.appendChild(node('p', 'blocks-empty', 'No sets in this block.'));
      column.forEach((s) => col.appendChild(setCard(s)));
      cols.appendChild(col);
    });
    box.appendChild(cols);
  }

  function progressText() {
    if (!run) return '';
    if (running()) {
      const from = startedHere || (run.started ? run.started * 1000 : Date.now());
      const secs = Math.max(0, Math.round((Date.now() - from) / 1000));
      return `Building the blocks… ${fmt(secs)} s of ${fmt(run.time_limit || TIME_MAX)} s`;
    }
    return {
      optimal: 'Built: the fewest sets these limits allow.',
      feasible: 'Built within the time limit: good blocks, not proven the fewest sets.',
      infeasible: "These limits can't be met:",
      cancelled: 'Stopped. Nothing has changed.',
      failed: 'The run failed:',
    }[run.status] || '';
  }

  function renderRun() {
    el('blocks-progress').textContent = progressText();
    el('blocks-stop').hidden = !running();
    const reasons = el('blocks-reasons');
    reasons.textContent = '';
    if (run && !running() && (run.reasons || []).length && !run.proposal) {
      const list = node('ul', 'blocks-reason-list');
      run.reasons.forEach((r) => list.appendChild(node('li', '', r)));
      reasons.appendChild(list);
    }
    renderProposal();
    const apply = el('blocks-apply');
    apply.hidden = !(run && run.proposal);
    apply.disabled = !(run && run.can_apply);
    el('blocks-undo').hidden = !canUndo();
    updateButtons();
  }

  // Undo for the year showing: the run's own word, or the years list's (the last Apply's Undo outlives a run
  // for another year, which the run of this year then cannot show)
  function canUndo() {
    if (!year) return false;
    if (run && run.can_undo) return true;
    const entry = years.levels.concat(years.stored).find((y) => y.key === year);
    return !!(entry && entry.can_undo);
  }
  async function reloadYears() {
    try {
      const y = await api('/api/blocks/years');
      years = { levels: y.levels || [], stored: y.stored || [] };
    } catch (e) { /* the button follows the run's own word until the next load */ }
    renderRun();
  }

  function updateButtons() {
    const placed = placement ? Object.keys(placement.placed || {}).length : 0;
    const blocked = checks.some((c) => c.blocking);
    el('blocks-build').disabled = !year || dirty || !placed || blocked || running();
    el('blocks-build').title = dirty ? 'Save the changes and place the students first'
      : !placement ? 'Place the students first' : blocked ? 'Fix the checks first' : !placed ? 'Nobody is placed' : '';
    el('blocks-place').disabled = !year || running();
    el('blocks-save').disabled = !year;
    el('blocks-add').disabled = !year;
  }

  const canPoll = () => tab === 'requirements' && !el('option-blocks').hidden;
  function stopPolling() {
    if (pollTimer) clearTimeout(pollTimer);
    pollTimer = null;
  }
  function startPolling() {
    stopPolling();
    const mine = generation, which = year;
    let failures = 0;                        // in a row: a passing blip (a 502 mid-run) is tried again
    const tick = async () => {
      pollTimer = null;
      if (mine !== generation || !canPoll()) return;
      try {
        const answer = await api('/api/blocks/run?year=' + enc(which));
        if (mine !== generation) return;
        run = answer;
        failures = 0;
      } catch (e) {
        if (mine !== generation) return;
        if (e.status === 404) run = null;
        else if (++failures < POLL_TRIES) {
          if (canPoll()) pollTimer = setTimeout(tick, self.pollMs * 2 ** failures);
          return;
        } else { say(e.message, 'bad'); renderRun(); return; }          // reopening the tab tries again
      }
      if (!running()) startedHere = 0;
      renderRun();
      if (running() && canPoll()) pollTimer = setTimeout(tick, self.pollMs);
    };
    pollTimer = setTimeout(tick, self.pollMs);
  }

  async function build() {
    if (!year) return;
    const t = Number(String(el('blocks-time').value).trim());
    if (!Number.isInteger(t) || t < TIME_MIN || t > TIME_MAX) {
      say(`The time limit must be a whole number of seconds from ${TIME_MIN} to ${TIME_MAX}.`, 'bad');
      return;
    }
    await send('/api/blocks/run', 'POST', { year, time_limit: t });
    startedHere = Date.now();
    run = { year, status: 'running', reasons: [], time_limit: t, proposal: null, can_apply: false,
      can_undo: !!(run && run.can_undo) };
    say('');
    renderRun();
    startPolling();
  }

  async function stop() {
    if (!year || !running()) return;
    try {
      await send('/api/blocks/cancel', 'POST', { year });
      say('Stopping…');
    } finally {
      startPolling();                        // the next poll shows the run stopped (or finished meanwhile)
    }
  }

  async function setTeacher(subject, set, teacher) {
    if (!year) return;
    try {
      run = await send('/api/blocks/teachers', 'PUT', { year, subject, set: Number(set), teacher: teacher || null });
    } finally {
      renderRun();                           // a refusal puts the picker back as it was
    }
  }

  const bandCount = (p) => (p.blocks || []).filter((c) => c.length).length;

  function announce() {
    if (typeof window.loadPlan === 'function') window.loadPlan();      // the plan tables and the board follow
    announcing = true;
    try { document.dispatchEvent(new CustomEvent('plane:students')); } finally { announcing = false; }
  }

  async function apply() {
    if (!year || !run || !run.proposal || !run.can_apply) return;
    const p = run.proposal, sum = p.summary || {};
    const sets = plural(sum.sets || 0, 'set', 'sets'), bands = plural(bandCount(p), 'band', 'bands');
    if (!confirm(`Apply writes ${sets} and ${bands} for ${yearLabel(year)} and puts ${plural(sum.students || 0, 'student', 'students')} in their sets. Undo restores the plan and the students. It replaces any sets and bands this year group got from an earlier Apply.`)) return;
    const answer = await send('/api/blocks/apply', 'POST', { year });
    const before = p;
    run = answer;
    renderRun();
    const parts = [`Applied: ${sets} and ${bands} for ${yearLabel(year)}.`];
    (answer.teachers_dropped || []).forEach((d) => {
      const s = (before.blocks || []).flat().find((x) => x.subject === d.subject && x.set === d.set);
      const who = staff.find((x) => x.id === d.teacher);
      const name = who && who.name && who.name !== d.teacher ? `${who.name} (${d.teacher})` : d.teacher;
      parts.push(`${s ? s.name : d.subject} has no teacher: ${name} has left or changed department.`);
    });
    say(parts.join(' '), (answer.teachers_dropped || []).length ? 'bad' : 'ok');
    announce();
    await reloadYears();                     // this Apply's Undo replaces any other year's
  }

  async function undo() {
    if (!canUndo()) return;
    if (!confirm("Undo removes these sets and bands and restores the students' sets as they were.")) return;
    await send('/api/blocks/undo', 'POST', { year });
    say('Undone: the plan and the students are as they were.', 'ok');
    announce();
    const mine = generation;
    await reloadYears();
    if (mine === generation) await loadRun(mine);
  }

  // ---------- loading ----------
  function renderSetup() {
    const has = !!year && !!data;
    el('blocks-setup').hidden = !has;
    el('blocks-head').textContent = has
      ? `${yearLabel(year)} · ${plural((data.classes || []).length, 'class', 'classes')} · ${plural(draft.combinations.length, 'combination', 'combinations')}`
      : '';
    if (!has) {                              // nothing of the last year group's form stays behind
      ['blocks-combinations', 'blocks-subjects', 'blocks-checks', 'blocks-lines', 'blocks-unplaced', 'blocks-reasons',
        'blocks-proposal'].forEach((id) => { el(id).textContent = ''; });
      el('blocks-more').hidden = true;
      return;
    }
    renderCombinations();
    renderLimits();
    renderSubjects();
    renderChecks();
    renderPlacement();
    updateButtons();
  }

  async function loadRun(mine) {
    let answer = null;
    try {
      answer = await api('/api/blocks/run?year=' + enc(year));
    } catch (e) {
      if (e.status !== 404) { if (mine === generation) say(e.message, 'bad'); }
    }
    if (mine !== generation) return;
    run = answer;
    startedHere = 0;
    renderRun();
    if (running()) startPolling();
  }

  async function openYear(mine) {
    data = null; draft = null; placement = null; checks = []; run = null; shown = PAGE;
    renderRun();
    renderSetup();                           // the last year's form goes at once, even if this load fails
    if (!year || year === CLASSES) return;
    const answer = await api('/api/blocks/setup?year=' + enc(year));
    if (mine !== generation) return;
    data = answer;
    resetDraft();
    checks = answer.checks || [];
    renderSetup();
    if (answer.setup && answer.setup.placement) {            // read as stored, with names: opening writes nothing
      try {
        const placed = await api('/api/blocks/place?year=' + enc(year));
        if (mine !== generation) return;
        placement = placed;
        checks = placed.checks || [];
      } catch (e) {
        if (mine !== generation) return;
        if (e.status !== 404) say(e.message, 'bad');      // the setup that loaded stays; Place makes it again
      }
      renderChecks();
      renderPlacement();
      updateButtons();
    }
    await loadRun(mine);
  }

  async function doLoad() {
    const mine = ++generation;
    stopPolling();
    try {
      const [y, plan] = await Promise.all([api('/api/blocks/years'), api('/api/plan').catch(() => null)]);
      if (mine !== generation) return;
      years = { levels: y.levels || [], stored: y.stored || [] };
      const p = (plan && plan.plan) || {};
      staff = (p.staff || []).map((s) => ({ id: s.id, name: s.name || s.id, dept: s.dept || '' }));
      planClasses = (p.classes || []).map((c) => c.code);
      planSubjects = Array.from(new Set((p.requirements || []).map((r) => String(r.subject || '').trim())
        .filter((s) => s && s.indexOf(',') === -1 && s.length <= 40))).sort(natural);
      fillYears();
      if (!year) {
        renderSetup();
        say('Give the plan’s classes a level, or choose classes, to set up option blocks.');
        return;
      }
      await openYear(mine);
    } catch (e) {
      if (mine === generation) say(e.message, 'bad');
    }
  }
  function load() {                          // `ready` is settled first, so a department account never loads
    loading = ready.then(() => (admin ? doLoad() : null));
    return loading;
  }
  // The tab shown again, or a reading applied: everything reloads, unless the setup has unsaved edits; then
  // only the run is read again (and polled while it is going), so the edits are not lost.
  function refresh() {
    if (!dirty || !year) return load();
    loading = ready.then(() => (admin ? loadRun(generation) : null));
    return loading;
  }

  function selectYear(key) {
    loading = ready.then(async () => {
      if (!admin) return;
      const mine = ++generation;
      stopPolling();
      year = key;
      say('');
      fillYears();
      try {
        await openYear(mine);
      } catch (e) {
        if (mine === generation) say(e.message, 'bad');
      }
    });
    return loading;
  }

  // ---------- wiring ----------
  function bind() {
    const sel = el('blocks-year');
    sel.addEventListener('change', () => {
      if (sel.value === CLASSES) {
        el('blocks-classes-pick').hidden = false;
        if (el('blocks-classes').focus) el('blocks-classes').focus();
        return;
      }
      if (dirty && !confirm('Leave this year group without saving its changes?')) { sel.value = year; return; }
      el('blocks-classes-pick').hidden = true;
      selectYear(sel.value).catch(swallow);
    });
    el('blocks-classes-use').addEventListener('click', () => {
      if (dirty && !confirm('Leave this year group without saving its changes?')) return;
      useClasses().catch(swallow);
    });
    el('blocks-add').addEventListener('click', () => {
      if (!draft) return;
      draft.combinations.push({ code: '', name: '', subjects: [], places: 0 });
      markDirty();
      renderCombinations();
    });
    [['blocks-n', 'blocks'], ['blocks-min', 'min_size'], ['blocks-max', 'max_size']].forEach(([id, k]) => {
      el(id).addEventListener('input', () => { if (draft) { draft.limits[k] = el(id).value; markDirty(); } });
    });
    el('blocks-save').addEventListener('click', () => { save().catch(swallow); });
    el('blocks-place').addEventListener('click', () => { place().catch(swallow); });
    el('blocks-more').addEventListener('click', () => { shown += PAGE; renderPlacement(); });
    el('blocks-build').addEventListener('click', () => { build().catch(swallow); });
    el('blocks-stop').addEventListener('click', () => { stop().catch(swallow); });
    el('blocks-apply').addEventListener('click', () => { apply().catch(swallow); });
    el('blocks-undo').addEventListener('click', () => { undo().catch(swallow); });
  }

  // Only the admin has the card: a department account is not shown it and no blocks route is called.
  const ready = api('/api/me').then((me) => {
    admin = !!me && me.role === 'admin';
    if (!admin) return;
    el('option-blocks').hidden = false;
    bind();
  }).catch(() => { admin = false; });

  document.addEventListener('plane:tab', (ev) => {
    tab = (ev.detail && ev.detail.key) || '';
    if (tab === 'requirements') refresh();                // also resumes polling a run still going
    else stopPolling();
  });
  // a workbook's Combinations sheet or a students reading applied: reload while showing
  ['plane:readings', 'plane:students'].forEach((name) => document.addEventListener(name, () => {
    if (!announcing && tab === 'requirements') refresh();
  }));

  const self = {
    ready, load, loaded: () => loading, pollMs: 1500,
    selectYear, save, place, setOverride, build, stop, setTeacher, apply, undo, stopPolling,
  };
  window.OptionBlocks = self;
})();
