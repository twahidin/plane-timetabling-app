// The deployment board's teacher tray (spec docs/superpowers/specs/2026-09-26-deployment-board-design.md
// §5): a card per teacher with their load against their allowance, drag a card onto a cell of the
// board, or click it to pick the teacher up and click cells; the Edit button opens the teacher's
// editor (allowance, reductions, provisional, name). board.js owns the board and its requests and
// hands this file what it needs through BoardTray.init. DOM built with createElement/textContent.
(function () {
  'use strict';

  const el = (id) => document.getElementById(id);
  const node = (tag, cls, text) => {
    const n = document.createElement(tag);
    if (cls) n.className = cls;
    if (text != null) n.textContent = text;
    return n;
  };
  const word = (key, opts) => (window.Words ? window.Words.word(key, opts) : key);
  const store = {
    get(key) { try { return localStorage.getItem(key); } catch (e) { return null; } },
    set(key, value) { try { localStorage.setItem(key, value); } catch (e) { /* private mode, etc. */ } },
  };

  let ctx = null;          // { mutate, selected, toggleSelect }, from board.js
  let board = null;
  let filter = 'home';
  let search = '';
  let collapsed = store.get('plane.boardTray') === 'collapsed';

  // periods left before the teacher's effective allowance is used up
  const left = (t) => Math.round((Number(t.effective) - Number(t.assigned)) * 10) / 10;
  function tone(t) {
    const l = left(t);
    if (l <= 0) return 'none';
    if (l < 4) return 'orange';
    if (l < 8) return 'amber';
    return 'ok';
  }

  function shown(t) {
    if (filter !== 'everyone' && !t.home) return false;
    if (filter === 'available' && left(t) <= 0) return false;
    if (filter === 'full' && left(t) > 0) return false;
    if (filter === 'provisional' && !t.provisional) return false;
    if (search) {
      const hay = `${t.name} ${t.short} ${t.id} ${t.dept}`.toLowerCase();
      if (!hay.includes(search)) return false;
    }
    return true;
  }

  // A bar of period blocks: filled for each period assigned, dashed for each still free, red for
  // each over the allowance. Past 48 blocks, one block stands for several periods.
  function blocks(t) {
    const bar = node('div', 'board-blocks');
    const effective = Math.max(0, Math.round(Number(t.effective) || 0));
    const assigned = Math.max(0, Math.round(Number(t.assigned) || 0));
    const total = Math.max(effective, assigned);
    const per = Math.max(1, Math.ceil(total / 48));
    for (let i = 0; i < Math.ceil(total / per); i++) {
      const at = i * per;
      const cls = at < Math.min(assigned, effective) ? 'on' : at < effective ? 'free' : 'over';
      bar.appendChild(node('i', cls));
    }
    bar.title = `${assigned} of ${effective} periods assigned` + (per > 1 ? ` (a block is ${per} periods)` : '');
    return bar;
  }

  function card(t) {
    const picked = ctx.selected() === t.id;
    const c = node('div', `board-card tone-${tone(t)}` + (picked ? ' picked' : '') + (t.provisional ? ' prov' : ''));
    c.draggable = true;
    c.tabIndex = 0;
    c.setAttribute('role', 'button');
    c.setAttribute('aria-pressed', String(picked));
    c.dataset.teacher = t.id;
    c.title = picked ? 'Picked up: click cells to assign, click again or press Esc to put down' : 'Drag onto a cell, or click to pick up';

    const head = node('div', 'board-card-head');
    head.appendChild(node('b', 'board-card-name', t.name || t.id));
    if (t.short) head.appendChild(node('span', 'board-card-short', t.short));
    if (t.provisional) head.appendChild(node('span', 'board-tag prov', 'PROV'));
    if (t.part_time) head.appendChild(node('span', 'board-tag', 'Part-time'));
    const edit = node('button', 'board-mini board-card-edit', 'Edit');
    edit.type = 'button';
    edit.setAttribute('aria-label', `Edit ${t.name || t.id}`);
    edit.addEventListener('click', (ev) => { ev.stopPropagation(); openStaff(t); });
    head.appendChild(edit);
    c.appendChild(head);

    const meta = node('div', 'board-card-meta');
    meta.appendChild(node('span', null, `Home: ${t.dept || 'none'}`));
    meta.appendChild(node('span', 'board-card-load', `${t.assigned}/${t.effective}p`));
    const l = left(t);
    meta.appendChild(node('span', 'board-card-left', l >= 0 ? `${l}p left` : `${-l}p over`));
    c.appendChild(meta);
    if (board && t.assigned_here !== t.assigned) {
      c.appendChild(node('div', 'board-card-here', `${board.dept}: ${t.assigned_here}p · elsewhere: ${t.assigned - t.assigned_here}p`));
    }
    c.appendChild(blocks(t));
    if ((t.reductions || []).length) {
      const tags = node('div', 'board-card-tags');
      t.reductions.forEach((r) => tags.appendChild(node('span', 'board-tag red', `${r.reason} −${r.periods}p`)));
      c.appendChild(tags);
    }

    c.addEventListener('dragstart', (ev) => {
      ev.dataTransfer.setData('text/plain', t.id);
      ev.dataTransfer.effectAllowed = 'copy';
      document.body.classList.add('board-dragging');
    });
    c.addEventListener('dragend', () => document.body.classList.remove('board-dragging'));
    c.addEventListener('click', () => ctx.toggleSelect(t.id));
    c.addEventListener('keydown', (ev) => {
      if (ev.target !== c) return;
      if (ev.key === 'Enter' || ev.key === ' ') { ev.preventDefault(); ctx.toggleSelect(t.id); }
    });
    return c;
  }

  function render(data) {
    board = data || board;
    if (!board) return;
    const tray = board.tray || [];
    const home = tray.filter((t) => t.home);
    el('board-tray-title').textContent = `${board.dept || ''} ${word('person', { plural: true })}`.trim();
    el('board-tray-count').textContent = `${home.length} in the department · ${tray.length} in all`;
    el('board-tray-filters').querySelectorAll('button').forEach((b) => {
      b.classList.toggle('on', b.dataset.filter === filter);
      b.setAttribute('aria-pressed', String(b.dataset.filter === filter));
    });
    el('board-tray').classList.toggle('collapsed', collapsed);
    el('board-tray-toggle').textContent = collapsed ? 'Expand' : 'Collapse';
    el('board-tray-toggle').setAttribute('aria-expanded', String(!collapsed));
    const box = el('board-cards');
    box.innerHTML = '';
    const list = tray.filter(shown);
    list.forEach((t) => box.appendChild(card(t)));
    if (!list.length) {
      box.appendChild(node('p', 'empty', tray.length ? `No ${word('person', { plural: true })} match.`
        : `No ${word('person', { plural: true })} yet: upload a workbook, or add a provisional one.`));
    }
  }

  // ---------- the teacher editor ----------
  let editing = null;      // the staff id being edited, or null for a new teacher
  let allowanceStart = '';  // the allowance field as the editor opened it
  function reductionRow(r) {
    const row = node('div', 'board-reduction');
    const reason = node('input');
    reason.type = 'text'; reason.placeholder = 'Reason, e.g. HOD'; reason.value = r.reason || '';
    reason.setAttribute('aria-label', 'Reason');
    const periods = node('input');
    periods.type = 'number'; periods.min = '0'; periods.step = '1'; periods.value = r.periods == null ? '' : String(r.periods);
    periods.setAttribute('aria-label', 'Periods');
    const drop = node('button', 'board-mini', '×');
    drop.type = 'button';
    drop.setAttribute('aria-label', 'Remove this reduction');
    drop.addEventListener('click', () => row.remove());
    row.append(reason, periods, node('span', 'relief-hint', 'p'), drop);
    return row;
  }

  function openStaff(t) {
    editing = t ? t.id : null;
    el('board-staff-title').textContent = t ? `Edit ${t.name || t.id}` : `New ${word('person')}`;
    el('board-staff-name').value = t ? t.name : '';
    el('board-staff-short').value = t ? t.short : '';
    el('board-staff-dept').value = t ? t.dept : ((board && board.dept) || '');
    // `allowance` is the base figure, given or worked out; `allowance_given` is the stored one.
    // A worked-out allowance leaves the field blank (the figure is the placeholder), so saving
    // other fields never turns it into a fixed number.
    allowanceStart = t ? (t.allowance_given == null ? '' : String(t.allowance_given)) : '';
    el('board-staff-allowance').value = allowanceStart;
    el('board-staff-allowance').placeholder = t && t.allowance_given == null ? `${t.allowance} worked out` : '';
    const list = el('board-staff-reductions');
    list.innerHTML = '';
    ((t && t.reductions) || []).forEach((r) => list.appendChild(reductionRow(r)));
    el('board-staff-provisional').checked = t ? !!t.provisional : true;
    el('board-staff-error').hidden = true;
    const dlg = el('board-staff-dialog');
    if (typeof dlg.showModal === 'function') dlg.showModal(); else dlg.setAttribute('open', '');
    el('board-staff-name').focus();
  }
  const closeStaff = () => { const d = el('board-staff-dialog'); if (d.open) d.close(); else d.removeAttribute('open'); };

  function staffBody() {
    const reductions = [];
    el('board-staff-reductions').querySelectorAll('.board-reduction').forEach((row) => {
      const [reason, periods] = row.querySelectorAll('input');
      if (!reason.value.trim() && periods.value === '') return;
      reductions.push({ reason: reason.value.trim(), periods: periods.value === '' ? 0 : Number(periods.value) });
    });
    const allowance = el('board-staff-allowance').value.trim();
    const body = {
      name: el('board-staff-name').value.trim() || null,
      short: el('board-staff-short').value.trim(),
      dept: el('board-staff-dept').value.trim(),
      reductions,
      provisional: el('board-staff-provisional').checked,
    };
    // sent only when changed: blank means worked out from the load factor
    if (allowance !== allowanceStart) body.allowance = allowance === '' ? null : Number(allowance);
    if (editing) body.id = editing;
    return body;
  }

  function init(context) {
    ctx = context;
    el('board-tray-search').addEventListener('input', (ev) => { search = ev.target.value.trim().toLowerCase(); render(); });
    el('board-tray-filters').addEventListener('click', (ev) => {
      const b = ev.target.closest('button[data-filter]');
      if (!b) return;
      filter = b.dataset.filter;
      render();
    });
    el('board-tray-toggle').addEventListener('click', () => {
      collapsed = !collapsed;
      store.set('plane.boardTray', collapsed ? 'collapsed' : 'open');
      render();
    });
    el('board-add-provisional').addEventListener('click', async () => {
      // straight onto the tray as "New <dept> teacher N"; Edit names it later
      if (!board || !board.dept) { openStaff(null); return; }
      const btn = el('board-add-provisional');
      btn.disabled = true;
      try {
        const res = await ctx.mutate('staff', { dept: board.dept, provisional: true });
        if (res.ok) { filter = filter === 'full' ? 'home' : filter; render(); }
      } finally { btn.disabled = false; }
    });
    el('board-reduction-add').addEventListener('click', () => el('board-staff-reductions').appendChild(reductionRow({})));
    el('board-staff-cancel').addEventListener('click', closeStaff);
    el('board-staff-form').addEventListener('submit', async (ev) => {
      ev.preventDefault();
      const err = el('board-staff-error');
      const body = staffBody();
      if (!body.dept) { err.textContent = 'Give the department.'; err.hidden = false; return; }
      if (!body.name && !body.provisional) { err.textContent = 'Give a name, or tick Provisional.'; err.hidden = false; return; }
      if (!body.name) delete body.name;
      el('board-staff-ok').disabled = true;
      try {
        const res = await ctx.mutate('staff', body, { quiet: true });
        if (res.ok) closeStaff(); else { err.textContent = res.message; err.hidden = false; }
      } finally { el('board-staff-ok').disabled = false; }
    });
  }

  window.BoardTray = { init, render, openStaff };
})();
