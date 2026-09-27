// The deployment board in the Plan tab (spec docs/superpowers/specs/2026-09-26-deployment-board-design.md
// §5): one department and level of the plan as subject rows by class columns, each cell showing
// who teaches it; a teacher tray below (board-tray.js). Every change posts to a /api/plan/board
// route and re-renders from the board it returns; a refusal shows on the cell it was about.
// DOM built with createElement/textContent; innerHTML only ever clears a container.
(function () {
  'use strict';

  const el = (id) => document.getElementById(id);
  // the department page (templates/department.html): one department, no Lock department, Submit
  const DEPT = document.body.dataset.mode === 'department';
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
  const store = {
    get(key) { try { return localStorage.getItem(key); } catch (e) { return null; } },
    set(key, value) { try { if (value == null) localStorage.removeItem(key); else localStorage.setItem(key, value); } catch (e) { /* ignore */ } },
  };
  function notify(cls, text) {
    if (typeof window.showNotes === 'function') { window.showNotes([], [], [{ cls, text }]); return; }
    const box = el('notes');
    if (box) { box.textContent = ''; box.appendChild(node('div', 'note ' + cls, text)); }
  }
  const openDialog = (dlg) => { if (typeof dlg.showModal === 'function') { if (!dlg.open) dlg.showModal(); } else dlg.setAttribute('open', ''); };
  const closeDialog = (dlg) => { if (dlg.open && typeof dlg.close === 'function') dlg.close(); else dlg.removeAttribute('open'); };

  let board = null;
  // the level (and, on the admin page, the department) on screen, remembered across visits: the
  // department page keeps its own key, so a head of department and the timetabler sharing a browser
  // never move each other's board
  const LEVEL_KEY = DEPT ? 'plane.deptBoardLevel' : 'plane.boardLevel';
  let want = { dept: DEPT ? null : store.get('plane.boardDept'), level: store.get(LEVEL_KEY) };
  let selected = null;            // a teacher picked up by clicking their card
  let pending = null;             // { cell, teacher }: a drop on a filled cell waiting for Replace / Co-teach / Split…
  const cellErrors = new Map();   // cell key -> the refusal shown on that cell until the next change
  let refusedHover = null;        // a locked cell a drag is over: a drop there never fires, so dragend says why
  let lastOver = null;            // the cell element the drag was last over (dragend in Firefox has no position)
  const LOCKED = 'Locked: unlock it before changing who teaches it.';

  async function request(method, path, body) {
    const r = await fetch(path, { method, headers: { 'Content-Type': 'application/json' },
      body: body === undefined ? undefined : JSON.stringify(body) });
    const data = await r.json().catch(() => ({}));
    const detail = typeof data.detail === 'string' ? data.detail : r.statusText;
    return { ok: r.ok, status: r.status, data, detail };
  }

  // Every board request takes the next number; a response older than the one on screen is dropped,
  // so a slow load cannot paint over a newer change. One change is in flight at a time.
  let seq = 0;
  let shownSeq = 0;
  let busy = false;
  let reloadAfter = null;         // a load asked for while a change was saving: { dept, level }, run after it
  const SAVING = 'Saving the last change: try again in a moment.';

  // ---------- load ----------
  async function loadBoard() {
    // while a change is saving, a load would be numbered after it and could land first, dropping the
    // change's own response: remember it and load once the change has been shown
    if (busy) { reloadAfter = { ...want }; return; }
    const n = ++seq;
    const query = (p) => { const q = new URLSearchParams(p).toString(); return '/api/plan/board' + (q ? '?' + q : ''); };
    const tries = [];
    if (want.dept && want.level) tries.push({ dept: want.dept, level: want.level });
    else if (DEPT && want.level) tries.push({ level: want.level });      // the department is theirs anyway
    if (want.dept) tries.push({ dept: want.dept });
    tries.push({});
    let res = null;
    for (const t of tries) {           // a remembered department or level the plan no longer has: fall back
      res = await request('GET', query(t));
      if (res.status !== 404) break;
    }
    if (!res.ok) { if (n > shownSeq) notify('error', `Board: ${res.detail}`); return; }
    show(res.data, n);
  }
  window.loadBoard = () => loadBoard().catch((e) => notify('error', `Board: ${e.message}`));

  function show(data, n, opts = {}) {
    if (n <= shownSeq) return;
    shownSeq = n;
    board = data;
    want = { dept: data.dept, level: data.level };
    if (!DEPT) store.set('plane.boardDept', data.dept);
    store.set(LEVEL_KEY, data.level);
    if (selected && !(data.tray || []).some((t) => t.id === selected)) selected = null;
    if (readOnly()) { selected = null; pending = null; }        // submitted: nothing picked up
    render();
    if (window.showPlanIssues) {
      window.showPlanIssues(data.issues || [], !(data.departments || []).length && !(data.tray || []).length,
        data.issue_counts);
    }
    // the cell editor refreshes after its own changes; after any other it keeps unsaved shares
    if (el('board-cell-dialog').open) fillCellDialog(false, null, !opts.dialog && dialogDirty);
  }

  // A board change (a POST to `path`): the body plus the department and level on screen, so the
  // same board comes back. opts.cell shows a refusal on that cell; opts.quiet leaves it to the
  // caller; opts.dialog marks a change made from the cell editor. Refused while another is saving.
  async function change(path, body, opts = {}) {
    if (busy) {
      if (!opts.quiet) notify('', SAVING);
      return { ok: false, status: 0, message: SAVING };
    }
    const payload = { ...body };
    if (!opts.noView && board && board.dept) payload.view = { dept: board.dept, level: board.level };
    // the plan rev the board on screen was built from: an edit to something changed since is refused
    if (board && board.rev != null) payload.rev = board.rev;
    busy = true;
    el('board').classList.add('saving');
    const n = ++seq;
    try {
      let res;
      try { res = await request('POST', path, payload); }
      catch (e) { res = { ok: false, status: 0, detail: e.message }; }
      if (res.status === 409 && res.data && res.data.board) {
        // stale: someone changed it since this board was loaded; show the board as it is now
        cellErrors.clear();
        pending = null;
        show(res.data.board, n, opts);
        // an undo that someone else's change is in the way of can never be made again: no "make it again"
        const message = opts.undo ? `${res.detail}. The board now shows the latest.`
          : `${res.detail}. The board now shows the latest: make your change again if you still want it.`;
        if (opts.cell && findCell(opts.cell)) { cellErrors.set(opts.cell, message); redraw(renderGrid); }
        else if (!opts.quiet) notify('error', message);
        return { ok: false, status: res.status, message };
      }
      if (!res.ok) {
        if (opts.cell) { cellErrors.set(opts.cell, res.detail); pending = null; redraw(renderGrid); }
        else if (!opts.quiet) notify('error', res.detail);
        return { ok: false, status: res.status, message: res.detail };
      }
      cellErrors.clear();
      pending = null;
      show(res.data, n, opts);
      return { ok: true };
    } finally {
      busy = false;
      el('board').classList.remove('saving');
      if (reloadAfter) {           // the load asked for meanwhile, now after the change's own board
        want = reloadAfter;
        reloadAfter = null;
        window.loadBoard();
      }
    }
  }
  const mutate = (action, body, opts) => change(`/api/plan/board/${action}`, body, opts);

  // A re-render replaces the cells and cards: the one that had keyboard focus (by its data-cell or
  // data-teacher) gets it back.
  function focusKey() {
    const a = document.activeElement;
    const n = a && a.closest && a.closest('[data-cell], [data-teacher]');
    if (!n) return null;
    return n.dataset.cell != null ? ['data-cell', n.dataset.cell] : ['data-teacher', n.dataset.teacher];
  }
  function redraw(...fns) {
    const key = focusKey();
    fns.forEach((fn) => fn());
    if (!key) return;
    const again = document.querySelector(`[${key[0]}="${CSS.escape(key[1])}"]`);
    if (again && again !== document.activeElement) again.focus();
  }

  // ---------- words ----------
  function levelLabel(tab) {
    if (!/^\d+$/.test(String(tab))) return String(tab);
    const school = !window.Words || window.Words.isDefault(window.Words.get());
    return school ? `Sec ${tab}` : `Level ${tab}`;
  }
  const teacherOf = (id) => ((board && board.tray) || []).find((t) => t.id === id);
  // a head of department's submitted department: nothing on the board changes until it is reopened
  const readOnly = () => DEPT && !!board && (board.department_status || {}).status === 'submitted';
  const nameOf = (id) => { const t = teacherOf(id); return t ? (t.name || t.id) : id; };
  const findCell = (key) => {
    for (const row of (board && board.rows) || []) {
      const cell = row.cells.find((c) => c.key === key);
      if (cell) return { row, cell };
    }
    return null;
  };

  function lockIcon() {
    const ns = 'http://www.w3.org/2000/svg';
    const svg = document.createElementNS(ns, 'svg');
    svg.setAttribute('viewBox', '0 0 12 14'); svg.setAttribute('class', 'board-lock'); svg.setAttribute('aria-hidden', 'true');
    const body = document.createElementNS(ns, 'rect');
    [['x', 1], ['y', 6], ['width', 10], ['height', 7], ['rx', 1.5]].forEach(([k, v]) => body.setAttribute(k, v));
    const shackle = document.createElementNS(ns, 'path');
    shackle.setAttribute('d', 'M3 6V4a3 3 0 0 1 6 0v2');
    shackle.setAttribute('fill', 'none'); shackle.setAttribute('stroke', 'currentColor'); shackle.setAttribute('stroke-width', '1.6');
    svg.append(body, shackle);
    return svg;
  }

  // ---------- toolbar ----------
  function renderBar() {
    const depts = board.departments || [];
    const sel = el('board-dept');
    if (sel) {
      sel.innerHTML = '';
      depts.forEach((d) => { const o = node('option', null, d); o.value = d; o.selected = d === board.dept; sel.appendChild(o); });
      sel.disabled = !depts.length;
    }
    const tabs = el('board-levels');
    tabs.innerHTML = '';
    (board.levels || []).forEach((lv) => {
      const b = button(lv === board.level ? 'on' : '', levelLabel(lv), () => { want = { dept: board.dept, level: lv }; window.loadBoard(); });
      b.setAttribute('role', 'tab');
      b.setAttribute('aria-selected', String(lv === board.level));
      tabs.appendChild(b);
    });
    const rows = board.rows || [];
    const closed = readOnly();
    const levelLocked = rows.length > 0 && rows.every((r) => r.locked);
    const lockLevel = el('board-lock-level');
    lockLevel.textContent = `${levelLocked ? 'Unlock' : 'Lock'} ${board.level ? levelLabel(board.level) : 'level'}`;
    lockLevel.disabled = !rows.length || closed;
    // offered from any level: the department is locked when every requirement of it is (dept_locked)
    const lockDept = el('board-lock-dept');
    if (lockDept) {
      lockDept.textContent = `${board.dept_locked ? 'Unlock' : 'Lock'} entire ${board.dept || 'department'}`;
      lockDept.title = board.dept_locked ? 'Unlock every level of this department' : 'Lock every level of this department';
      lockDept.disabled = !board.dept;
    }
    el('board-add-band').disabled = !board.dept || closed;
    el('board-add-row').disabled = closed;
    el('board-undo').disabled = closed;
    // the note that says why: in place of the hint on how to drag and pick up
    let note = el('board-closed-note');
    if (!note) {
      note = node('p', 'board-hint board-closed-note');
      note.id = 'board-closed-note';
      note.setAttribute('role', 'status');
      el('board-hint').after(note);
    }
    note.textContent = closed ? `${board.dept} is submitted: nothing on the board can change until the timetabler reopens it.` : '';
    note.hidden = !closed;
    el('board-hint').hidden = closed;
  }

  // ---------- grid ----------
  // Cells of one row that share columns (the option groups of a band) go on separate lines.
  function lanesOf(cells) {
    const lanes = [];
    cells.forEach((cell) => {
      const free = lanes.find((lane) => lane.every((o) => cell.col + cell.span <= o.col || o.col + o.span <= cell.col));
      if (free) free.push(cell); else lanes.push([cell]);
    });
    lanes.forEach((lane) => lane.sort((a, b) => a.col - b.col));
    return lanes.length ? lanes : [[]];
  }

  function rowHeader(row, lanes) {
    const th = node('th', 'board-rowhead' + (row.locked ? ' locked' : ''));
    th.rowSpan = lanes;
    th.scope = 'row';
    th.appendChild(node('div', 'board-row-title', row.title));
    th.appendChild(node('div', 'board-row-meta', `${row.level} · ${row.periods}p [${row.pattern}]`));
    const lock = button('board-mini', row.locked ? 'Unlock row' : 'Lock row',
      () => mutate('lock', { scope: { row: row.key }, locked: !row.locked }));
    lock.disabled = readOnly();
    if (row.locked) lock.prepend(lockIcon());
    th.appendChild(lock);
    return th;
  }

  function chip(part, cell) {
    const prov = part.teachers.some((id) => (teacherOf(id) || {}).provisional);
    const c = node('div', 'board-chip' + (part.locked ? ' locked' : '') + (prov ? ' prov' : ''));
    part.teachers.forEach((id) => {
      const who = node('span', 'board-chip-name');
      who.appendChild(node('span', null, nameOf(id)));
      if ((teacherOf(id) || {}).provisional) who.appendChild(node('span', 'board-tag prov', 'PROV'));
      if (!part.locked && !readOnly()) {
        const x = button('board-x', '×', () => mutate('unassign', { req: part.req, teacher: id }, { cell: cell.key }));
        x.setAttribute('aria-label', `Remove ${nameOf(id)}`);
        x.title = `Remove ${nameOf(id)}`;
        who.appendChild(x);
      }
      c.appendChild(who);
    });
    const meta = node('span', 'board-chip-meta');
    if (part.locked) meta.appendChild(lockIcon());
    meta.appendChild(node('span', null, `${part.periods} period${part.periods === 1 ? '' : 's'}`));
    if (cell.split) meta.appendChild(node('span', 'board-tag', 'Split'));
    if (part.teachers.length > 1) meta.appendChild(node('span', 'board-tag', 'Co-taught'));
    c.appendChild(meta);
    return c;
  }

  function choice(cell) {
    const box = node('div', 'board-choice');
    box.appendChild(node('div', 'board-choice-text', `${nameOf(pending.teacher)} onto a full cell:`));
    // a locked part is not replaced, a co-taught one not replaced whole (remove a teacher first),
    // and co-teaching adds to every taught part, so not when any part is locked
    const taught = cell.parts.filter((p) => p.teachers.length === 1 && !p.locked);
    const go = (body) => mutate('assign', { cell: cell.key, teacher: pending.teacher, ...body }, { cell: cell.key });
    if (cell.parts.length <= 1) { if (taught.length) box.appendChild(button('btn', 'Replace', () => go({ mode: 'replace' }))); }
    else taught.forEach((p) => box.appendChild(button('btn', `Replace ${nameOf(p.teachers[0])}`, () => go({ mode: 'replace', req: p.req }))));
    if (!cell.parts.some((p) => p.locked)) box.appendChild(button('btn', 'Co-teach', () => go({ mode: 'coteach' })));
    box.appendChild(button('btn', 'Split…', () => { const t = pending.teacher; pending = null; redraw(renderGrid); openCell(cell.key, t); }));
    box.appendChild(button('btn', 'Cancel', () => { pending = null; redraw(renderGrid); }));
    return box;
  }

  // nothing on the cell can take a teacher: every share locked
  const shut = (cell) => cell.locked || (!cell.unassigned.some((u) => !u.locked) && cell.parts.every((p) => p.locked));

  function dropOn(cell, teacher) {
    if (!teacher || !teacherOf(teacher) || readOnly()) return;
    cellErrors.delete(cell.key);
    pending = null;
    if (shut(cell)) {
      cellErrors.set(cell.key, LOCKED);
      redraw(renderGrid);
      return;
    }
    if (cell.unassigned.some((u) => !u.locked)) { mutate('assign', { cell: cell.key, teacher }, { cell: cell.key }); return; }
    pending = { cell: cell.key, teacher };
    redraw(renderGrid);
    const first = document.querySelector('.board-choice .btn');
    if (first) first.focus();
  }

  function cellTd(cell) {
    const done = cell.assigned >= cell.needed;
    const td = node('td', 'board-cell' + (done ? ' done' : cell.assigned ? ' part' : ' open')
      + (cell.locked ? ' locked' : '') + (cellErrors.has(cell.key) ? ' bad' : '') + (selected ? ' picking' : ''));
    td.colSpan = cell.span;
    td.dataset.cell = cell.key;
    td.tabIndex = 0;

    const head = node('div', 'board-cell-head');
    head.appendChild(node('b', 'board-count', `${cell.assigned}/${cell.needed}p`));
    if (cell.grouping && cell.grouping !== 'class') head.appendChild(node('span', 'board-tag group', cell.grouping));
    if (cell.classes.length > cell.span) head.appendChild(node('span', 'board-tag', cell.classes.join(', ')));
    if (cell.locked) head.appendChild(lockIcon());
    const unassigned = cell.needed - cell.assigned;
    head.appendChild(node('span', done ? 'board-status ok' : 'board-status miss', done ? '✓' : `${unassigned}p unassigned`));
    td.appendChild(head);

    cell.parts.forEach((p) => td.appendChild(chip(p, cell)));
    cell.unassigned.forEach((u) => {
      td.appendChild(node('div', 'board-slot' + (u.locked ? ' locked' : ''),
        u.locked ? `${u.periods}p, locked` : `${u.periods}p · drag a teacher here`));
    });
    if (pending && pending.cell === cell.key) td.appendChild(choice(cell));
    if (cellErrors.has(cell.key)) {
      const err = node('div', 'board-cell-error', cellErrors.get(cell.key));
      err.setAttribute('role', 'alert');
      td.appendChild(err);
    }

    // a locked cell shows it will not take the teacher (dropEffect none: the browser then fires no
    // drop), and the drag's end says why on the cell (see the document's dragend below)
    td.addEventListener('dragover', (ev) => {
      if (readOnly()) return;                  // no drop at all (dragover not taken)
      ev.preventDefault();
      const refused = shut(cell);
      ev.dataTransfer.dropEffect = refused ? 'none' : 'copy';
      td.classList.add(refused ? 'refuse' : 'drop');
      refusedHover = refused ? cell.key : null;
      lastOver = td;
    });
    td.addEventListener('dragleave', (ev) => {
      if (td.contains(ev.relatedTarget)) return;
      td.classList.remove('drop', 'refuse');
      // a release over the cell also fires dragleave, with no element to go to: keep the refusal then
      if (ev.relatedTarget && refusedHover === cell.key) refusedHover = null;
    });
    td.addEventListener('drop', (ev) => {
      ev.preventDefault();
      refusedHover = null;
      td.classList.remove('drop', 'refuse');
      document.body.classList.remove('board-dragging');
      dropOn(cell, ev.dataTransfer.getData('text/plain'));
    });
    const act = () => { if (selected && !readOnly()) dropOn(cell, selected); else openCell(cell.key); };
    td.addEventListener('click', (ev) => { if (!ev.target.closest('button')) act(); });
    td.addEventListener('keydown', (ev) => {
      if (ev.target === td && (ev.key === 'Enter' || ev.key === ' ')) { ev.preventDefault(); act(); }
    });
    return td;
  }

  function renderGrid() {
    const table = el('board-grid');
    table.innerHTML = '';
    if (!board) return;
    const classes = board.classes || [];
    const rows = board.rows || [];
    const width = Math.max(classes.length, ...rows.flatMap((r) => r.cells.map((c) => c.col + c.span)), 1);
    const thead = node('thead'), htr = node('tr');
    htr.appendChild(node('th', 'board-corner', 'Subject'));
    for (let i = 0; i < width; i++) {
      const cls = classes[i];
      const th = node('th', 'board-class');
      th.scope = 'col';
      th.appendChild(node('div', 'board-class-code', cls ? cls.code : ''));
      if (cls) cls.divisions.forEach((d) => th.appendChild(node('span', 'board-tag', d)));
      htr.appendChild(th);
    }
    thead.appendChild(htr);
    table.appendChild(thead);
    const tbody = node('tbody');
    if (!rows.length) {
      const tr = node('tr'), td = node('td', 'board-empty');
      td.colSpan = width + 1;
      td.textContent = (board.departments || []).length
        ? 'No subjects at this level yet: Add subject or Create band.'
        : 'No plan yet: upload a workbook, download the template to fill in, or Add subject.';
      tr.appendChild(td);
      tbody.appendChild(tr);
    }
    rows.forEach((row) => {
      const lanes = lanesOf(row.cells);
      lanes.forEach((lane, i) => {
        const tr = node('tr', i === 0 ? 'board-first' : '');
        if (i === 0) tr.appendChild(rowHeader(row, lanes.length));
        let col = 0;
        lane.forEach((cell) => {
          for (; col < cell.col; col++) tr.appendChild(node('td', 'board-none'));
          tr.appendChild(cellTd(cell));
          col = cell.col + cell.span;
        });
        for (; col < width; col++) tr.appendChild(node('td', 'board-none'));
        tbody.appendChild(tr);
      });
    });
    table.appendChild(tbody);
  }

  function renderPicked() {
    const box = el('board-picked');
    box.innerHTML = '';
    box.hidden = !selected;
    if (!selected) return;
    box.appendChild(node('span', null, `${nameOf(selected)} picked up: click cells to assign. `));
    box.appendChild(button('board-mini', 'Put down', () => toggleSelect(selected)));
  }

  const renderTray = () => window.BoardTray.render(board);
  const renderStatus = () => { if (window.BoardStatus) window.BoardStatus.render(board); };
  function render() {
    if (!board) return;
    redraw(renderBar, renderGrid, renderPicked, renderTray, renderStatus);
  }

  function toggleSelect(id) {
    selected = selected === id ? null : id;
    pending = null;
    redraw(renderGrid, renderPicked, renderTray);
  }

  // ---------- the cell editor ----------
  let dialogKey = null;
  let shares = [];                 // [{ teachers: [...], periods }] in the split editor
  let dialogDirty = false;         // the split editor holds edits not yet saved

  // The splits a lesson pattern allows: every way of sharing its lessons out into 2 or 3 shares,
  // largest share first ("2,2,1" -> 4/1, 3/2, 2/2/1). The server checks the one saved.
  function splitOptions(pattern) {
    const lens = String(pattern || '').split(',').map(Number).filter((n) => n > 0);
    if (lens.length < 2 || lens.length > 16) return [];
    const out = [];
    for (let k = 2; k <= Math.min(3, lens.length); k++) {
      const sums = new Array(k).fill(0), seen = new Set();
      const place = (i) => {
        const key = i + ':' + sums.slice().sort((a, b) => b - a).join(',');
        if (seen.has(key)) return;
        seen.add(key);
        if (i === lens.length) {
          const label = sums.slice().sort((a, b) => b - a).join('/');
          if (sums.every((x) => x > 0) && !out.includes(label)) out.push(label);
          return;
        }
        for (let j = 0; j < k; j++) { sums[j] += lens[i]; place(i + 1); sums[j] -= lens[i]; }
      };
      place(0);
    }
    return out;
  }

  function sharesFrom(cell) {
    const parts = cell.parts.map((p) => ({ req: p.req, teachers: p.teachers.slice(), periods: p.periods }))
      .concat(cell.unassigned.map((u) => ({ req: u.req, teachers: [], periods: u.periods })));
    return parts.sort((a, b) => (a.req < b.req ? -1 : 1)).map(({ teachers, periods }) => ({ teachers, periods }));
  }

  function teacherSelect(share) {
    const sel = node('select');
    sel.setAttribute('aria-label', 'Who teaches this share');
    const add = (value, text) => { const o = node('option', null, text); o.value = value; sel.appendChild(o); return o; };
    add('', '(not assigned)');
    if (share.teachers.length > 1) add(share.teachers.join('+'), share.teachers.map(nameOf).join(' + '));
    const tray = (board.tray || []).slice().sort((a, b) => (b.home - a.home));
    tray.forEach((t) => add(t.id, t.home ? t.name : `${t.name} (${t.dept})`));
    sel.value = share.teachers.join('+');
    sel.addEventListener('change', () => { share.teachers = sel.value ? sel.value.split('+') : []; dialogDirty = true; });
    return sel;
  }

  function renderShares(cell) {
    const box = el('board-shares');
    box.innerHTML = '';
    shares.forEach((share, i) => {
      const row = node('div', 'board-share');
      row.appendChild(teacherSelect(share));
      const n = node('input');
      n.type = 'number'; n.min = '1'; n.step = '1'; n.value = share.periods == null ? '' : String(share.periods);
      n.setAttribute('aria-label', 'Periods');
      n.addEventListener('input', () => { share.periods = n.value === '' ? null : Number(n.value); dialogDirty = true; });
      row.append(n, node('span', 'relief-hint', 'p'));
      if (shares.length > 1) {
        row.appendChild(button('board-mini', '×', () => { shares.splice(i, 1); dialogDirty = true; renderShares(cell); }));
      }
      box.appendChild(row);
    });
    const opts = el('board-split-options');
    opts.innerHTML = '';
    const options = splitOptions(cell.pattern);
    opts.appendChild(node('span', 'relief-hint', options.length ? `Lessons ${cell.pattern}: ` : `Lessons ${cell.pattern} cannot be split.`));
    options.forEach((label) => opts.appendChild(button('board-mini', label, () => {
      const sizes = label.split('/').map(Number);
      shares = sizes.map((p, i) => ({ teachers: shares[i] ? shares[i].teachers : [], periods: p }));
      dialogDirty = true;
      renderShares(cell);
    })));
  }

  // keepShares: a re-render that was not the editor's own change leaves unsaved shares alone
  function fillCellDialog(fresh, addTeacher, keepShares) {
    const found = findCell(dialogKey);
    if (!found) { closeDialog(el('board-cell-dialog')); return; }
    const { row, cell } = found;
    el('board-cell-title').textContent = `${row.title} · ${cell.classes.join(', ')}`
      + (cell.grouping && cell.grouping !== 'class' ? ` · ${cell.grouping}` : '');
    el('board-cell-meta').textContent = `${cell.needed} periods a cycle as lessons of ${cell.pattern}; ${cell.assigned} assigned.`
      + (cell.locked ? ' Locked: unlock it to change anything.' : '');
    const list = el('board-cell-parts');
    list.innerHTML = '';
    cell.parts.forEach((p) => {
      const li = node('li', p.locked ? 'locked' : '');
      li.appendChild(node('b', null, `${p.periods}p`));
      p.teachers.forEach((id) => {
        const who = node('span', 'board-part-name', nameOf(id));
        if (!p.locked && !readOnly()) {
          who.appendChild(button('board-mini', 'Remove', async () => {
            const res = await mutate('unassign', { req: p.req, teacher: id }, { quiet: true, dialog: true });
            if (!res.ok) cellError(res.message);
          }));
        }
        li.appendChild(who);
      });
      if (p.locked) li.appendChild(node('span', 'board-tag', 'locked'));
      list.appendChild(li);
    });
    cell.unassigned.forEach((u) => list.appendChild(node('li', 'open', `${u.periods}p not assigned yet`)));
    el('board-cell-lock').textContent = cell.locked ? 'Unlock cell' : 'Lock cell';
    el('board-cell-lock').disabled = readOnly();
    el('board-split-save').disabled = cell.locked || readOnly();
    el('board-share-add').disabled = cell.locked || readOnly();
    if (fresh) el('board-cell-error').hidden = true;
    if (keepShares) return;
    // on opening and after the editor's own changes the split editor starts from the cell as it is
    shares = sharesFrom(cell);
    dialogDirty = !!addTeacher;
    if (addTeacher) {
      // Split… from a drop: the dropped teacher as one more share, periods from the most even
      // split the lessons allow
      const options = splitOptions(cell.pattern).filter((o) => o.split('/').length === shares.length + 1);
      const even = options.length ? options[options.length - 1].split('/').map(Number) : null;
      shares.push({ teachers: [addTeacher], periods: null });
      if (even) shares.forEach((sh, i) => { sh.periods = even[i]; });
    }
    renderShares(cell);
  }

  function cellError(message) {
    const box = el('board-cell-error');
    box.textContent = message;
    box.hidden = false;
  }

  function openCell(key, addTeacher) {
    dialogKey = key;
    fillCellDialog(true, addTeacher);
    openDialog(el('board-cell-dialog'));
  }

  // ---------- wiring ----------
  function wire() {
    const picked = node('p', 'board-picked');
    picked.id = 'board-picked';
    picked.hidden = true;
    el('board-hint').after(picked);

    if (!DEPT) el('board-dept').addEventListener('change', (ev) => { want = { dept: ev.target.value, level: null }; window.loadBoard(); });
    el('board-lock-level').addEventListener('click', () => {
      const locked = (board.rows || []).every((r) => r.locked);
      mutate('lock', { scope: { dept: board.dept, level: board.level }, locked: !locked });
    });
    if (!DEPT) {
      el('board-lock-dept').addEventListener('click', () => {
        mutate('lock', { scope: { dept: board.dept }, locked: !board.dept_locked });
      });
    }
    el('board-undo').addEventListener('click', async () => {
      const btn = el('board-undo');
      btn.disabled = true;
      try {
        const res = await change('/api/plan/board/undo', {}, { quiet: true, undo: true });
        if (res.status === 404) notify('', 'Nothing to undo on the board.');
        else if (!res.ok) notify(res.message === SAVING ? '' : 'error', res.message);
      } finally { btn.disabled = readOnly(); }
    });


    el('board-cell-close').addEventListener('click', () => closeDialog(el('board-cell-dialog')));
    el('board-cell-lock').addEventListener('click', async () => {
      const found = findCell(dialogKey);
      if (!found) return;
      const res = await mutate('lock', { scope: { cell: dialogKey }, locked: !found.cell.locked }, { quiet: true, dialog: true });
      if (!res.ok) cellError(res.message);
    });
    el('board-share-add').addEventListener('click', () => {
      const found = findCell(dialogKey);
      if (!found) return;
      shares.push({ teachers: [], periods: null });
      dialogDirty = true;
      renderShares(found.cell);
    });
    el('board-split-save').addEventListener('click', async () => {
      if (shares.some((s) => !(s.periods > 0))) { cellError('Give every share its periods.'); return; }
      const body = { cell: dialogKey, shares: shares.map((s) => (s.teachers.length > 1
        ? { teachers: s.teachers, periods: s.periods } : { teacher: s.teachers[0] || null, periods: s.periods })) };
      const btn = el('board-split-save');
      btn.disabled = true;
      try {
        const res = await mutate('split', body, { quiet: true, dialog: true });
        if (res.ok) el('board-cell-error').hidden = true; else cellError(res.message);
      } finally {
        const found = findCell(dialogKey);
        btn.disabled = !!(found && found.cell.locked) || readOnly();
      }
    });

    document.addEventListener('keydown', (ev) => {
      if (ev.key !== 'Escape' || document.querySelector('dialog[open]')) return;
      if (selected || pending) { selected = null; pending = null; redraw(renderGrid, renderPicked, renderTray); }
    });
    // a drag that leaves the window forgets the locked cell it passed over: Firefox then ends it at
    // 0,0, which must not read as a release on that cell
    document.addEventListener('dragleave', (ev) => {
      if (ev.relatedTarget) return;
      const edge = ev.clientX <= 0 || ev.clientY <= 0 || ev.clientX >= window.innerWidth || ev.clientY >= window.innerHeight;
      if (edge || ev.target === document || ev.target === document.documentElement) {
        refusedHover = null;
        lastOver = null;
      }
    });
    document.addEventListener('dragend', (ev) => {
      document.body.classList.remove('board-dragging');
      const key = refusedHover;
      const over = lastOver;
      refusedHover = null;
      lastOver = null;
      if (!key) return;
      // only a release on that locked cell (not one outside the window after passing over it);
      // Firefox reports a dragend at 0,0, so there the cell last dragged over stands in for the point
      const noPoint = ev.clientX === 0 && ev.clientY === 0;
      const hit = noPoint ? over : document.elementFromPoint(ev.clientX, ev.clientY);
      const cellEl = hit && hit.closest('[data-cell]');
      if (!cellEl || cellEl.dataset.cell !== key) return;
      cellErrors.set(key, LOCKED);
      redraw(renderGrid);
    });
  }

  window.BoardTray.init({ mutate, selected: () => selected, toggleSelect, readOnly });
  window.BoardForms.init({ mutate, board: () => board });
  if (window.BoardStatus) window.BoardStatus.init({ mutate, board: () => board });
  wire();
  // the department page loads the board itself (department.js); the admin page when the board shows
  if (!DEPT && !el('plan').hidden && !el('board').hidden) window.loadBoard();
})();
