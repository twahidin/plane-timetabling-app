// The deployment board's sign-off and activity (spec
// docs/superpowers/specs/2026-09-27-department-accounts-design.md §4-§5). On the department page:
// the status line, Submit department (confirmed inline) and the Activity panel. On the admin page:
// the department's status beside the department select, Reopen for a submitted one, and the
// Activity panel. board.js owns the board and its requests and hands this file what it needs
// through BoardStatus.init; it calls BoardStatus.render after every board it shows.
// DOM built with createElement/textContent; innerHTML only clears.
(function () {
  'use strict';

  const el = (id) => document.getElementById(id);
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
  function notify(cls, text) {
    if (typeof window.showNotes === 'function') { window.showNotes([], [], [{ cls, text }]); return; }
    const box = el('notes');
    if (box) { box.textContent = ''; box.appendChild(node('div', 'note ' + cls, text)); }
  }

  let ctx = null;          // { mutate, board }, from board.js
  let confirming = false;  // the Submit department question is showing
  let activityGen = 0;

  const day = (at) => {
    const d = at ? new Date(at) : null;
    return d && !isNaN(d) ? d.toLocaleDateString(undefined, { day: 'numeric', month: 'short' }) : '';
  };
  const stamp = (at) => {
    const d = at ? new Date(at) : null;
    return d && !isNaN(d) ? d.toLocaleString(undefined, { day: 'numeric', month: 'short', hour: '2-digit', minute: '2-digit' }) : '';
  };
  const statusOf = (board) => (board && board.department_status) || { status: 'open' };
  const byWhom = (st) => (DEPT && st.by && st.by === document.body.dataset.user ? 'you' : (st.by || 'someone'));

  // ---------- the department page: status line and Submit ----------
  function renderDept(board) {
    const st = statusOf(board);
    const submitted = st.status === 'submitted';
    el('dept-status').textContent = submitted
      ? `Submitted by ${byWhom(st)}${st.at ? ` on ${day(st.at)}` : ''} — ask the timetabler to reopen it if something needs to change.`
      : `Open — submit ${board.dept || 'the department'} when it is done.`;
    el('dept-status').classList.toggle('submitted', submitted);
    el('dept-submit').hidden = submitted || confirming;
    el('dept-submit').disabled = !(board.rows || []).length && !(board.levels || []).length;
    if (submitted) { confirming = false; el('dept-submit-confirm').hidden = true; }
    el('board').classList.toggle('submitted', submitted);
  }

  function askSubmit() {
    const board = ctx.board();
    if (!board) return;
    confirming = true;
    const box = el('dept-submit-confirm');
    box.textContent = '';
    box.appendChild(node('span', null, `Submit ${board.dept}? Every row is locked, and only the timetabler can reopen it.`));
    const yes = button('btn primary', 'Submit department', async () => {
      yes.disabled = true;
      const res = await ctx.mutate('submit', {}, { quiet: true });
      confirming = false;
      box.hidden = true;
      if (res.ok) notify('good', `${board.dept} is submitted: every row is locked, and the timetabler sees it as done.`);
      else { notify('error', res.message); renderDept(ctx.board()); }
    });
    const no = button('btn', 'Cancel', () => { confirming = false; box.hidden = true; renderDept(ctx.board()); });
    box.append(yes, no);
    box.hidden = false;
    el('dept-submit').hidden = true;
    yes.focus();
  }

  // ---------- the admin page: status beside the select, Reopen ----------
  function renderAdmin(board) {
    const st = statusOf(board);
    const submitted = st.status === 'submitted';
    const line = el('board-dept-status');
    line.textContent = !board.dept ? '' : submitted
      ? `Submitted${st.by ? ` by ${st.by}` : ''}${st.at ? ` on ${day(st.at)}` : ''}` : 'Open';
    line.className = 'board-dept-status' + (submitted ? ' submitted' : '');
    el('board-reopen').hidden = !submitted;
    // the select says which departments are in
    const statuses = board.department_statuses || {};
    Array.from(el('board-dept').options).forEach((o) => {
      if (statuses[o.value] === 'submitted') o.textContent = `${o.value} (submitted)`;
    });
  }

  // ---------- activity ----------
  async function loadActivity() {
    const board = ctx.board();
    const list = el('board-activity-list');
    if (!board || !list) return;
    const my = ++activityGen;
    let data;
    try {
      const r = await fetch('/api/plan/activity' + (board.dept ? `?dept=${encodeURIComponent(board.dept)}` : ''));
      data = await r.json().catch(() => ({}));
      if (!r.ok) throw new Error(typeof data.detail === 'string' ? data.detail : r.statusText);
    } catch (e) {
      if (my !== activityGen) return;
      list.textContent = '';
      list.appendChild(node('li', 'error', `Activity: ${e.message}`));
      return;
    }
    if (my !== activityGen) return;
    list.textContent = '';
    const rows = (data.activity || []).slice(0, 60);
    rows.forEach((a) => {
      const li = node('li');
      li.appendChild(node('span', 'board-activity-when', stamp(a.when)));
      li.appendChild(node('b', null, a.who || 'Someone'));
      li.appendChild(node('span', null, a.text || 'a change'));
      list.appendChild(li);
    });
    if (!rows.length) list.appendChild(node('li', 'empty', 'No changes on this board yet.'));
  }

  function render(board) {
    if (!board) return;
    if (DEPT) renderDept(board); else renderAdmin(board);
    if (el('board-activity').open) loadActivity();
  }

  function init(context) {
    ctx = context;
    el('board-activity').addEventListener('toggle', () => { if (el('board-activity').open) loadActivity(); });
    if (DEPT) {
      el('dept-submit').addEventListener('click', askSubmit);
      return;
    }
    el('board-reopen').addEventListener('click', async () => {
      const board = ctx.board();
      if (!board || !board.dept) return;
      const btn = el('board-reopen');
      btn.disabled = true;
      try {
        const res = await ctx.mutate('reopen', { dept: board.dept });
        if (res.ok) notify('good', `${board.dept} is open again: its head of department can change it and submit it again.`);
      } finally { btn.disabled = false; }
    });
  }

  window.BoardStatus = { init, render };
})();
