// The deployment board's Add subject and Create band dialogs (spec
// docs/superpowers/specs/2026-09-26-deployment-board-design.md §4-§5): a subject row for the classes
// ticked, and option groups that run together over a division of the classes. board.js hands in
// the board on screen and its change function through BoardForms.init. DOM built with
// createElement/textContent; innerHTML only clears.
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
  const openDialog = (dlg) => { if (typeof dlg.showModal === 'function') { if (!dlg.open) dlg.showModal(); } else dlg.setAttribute('open', ''); };
  const closeDialog = (dlg) => { if (dlg.open && typeof dlg.close === 'function') dlg.close(); else dlg.removeAttribute('open'); };

  let ctx = null;          // { mutate, board }, from board.js

  function lessonsFrom(fieldsetId) {
    const out = {};
    el(fieldsetId).querySelectorAll('input[data-len]').forEach((i) => { const n = Number(i.value || 0); if (n) out[i.dataset.len] = n; });
    return out;
  }
  function classBoxes(boxId) {
    const box = el(boxId);
    box.innerHTML = '';
    (ctx.board().classes || []).forEach((c) => {
      const label = node('label', 'board-check');
      const cb = node('input');
      cb.type = 'checkbox'; cb.value = c.code; cb.checked = true;
      label.append(cb, document.createTextNode(' ' + c.code));
      box.appendChild(label);
    });
    if (!(ctx.board().classes || []).length) box.appendChild(node('p', 'relief-hint', 'No classes at this level yet: type them below.'));
  }
  const checked = (boxId) => Array.from(el(boxId).querySelectorAll('input:checked')).map((i) => i.value);
  const defaultLevel = () => { const b = ctx.board(); return ((b.rows || [])[0] || {}).level || b.level || ''; };
  function resetLessons(fieldsetId) { el(fieldsetId).querySelectorAll('input[data-len]').forEach((i) => { i.value = '0'; }); }
  function formError(id, message) { const e = el(id); e.textContent = message; e.hidden = !message; }

  function openRow() {
    el('board-row-dept').value = ctx.board().dept || '';
    el('board-row-level').value = defaultLevel();
    el('board-row-subject').value = '';
    el('board-row-more').value = '';
    resetLessons('board-row-lessons');
    classBoxes('board-row-classes');
    formError('board-row-error', '');
    openDialog(el('board-row-dialog'));
    el('board-row-subject').focus();
  }

  function bandGroup(code) {
    const row = node('div', 'board-share');
    const input = node('input');
    input.type = 'text'; input.value = code || ''; input.placeholder = 'code, or blank to make one up';
    input.setAttribute('aria-label', 'Group code');
    row.append(input, button('board-mini', '×', () => row.remove()));
    return row;
  }
  function openBand() {
    el('board-band-subject').value = '';
    el('board-band-level').value = defaultLevel();
    resetLessons('board-band-lessons');
    classBoxes('board-band-classes');
    const groups = el('board-band-groups');
    groups.innerHTML = '';
    groups.append(bandGroup(''), bandGroup(''));
    formError('board-band-error', '');
    openDialog(el('board-band-dialog'));
    el('board-band-subject').focus();
  }
  function init(context) {
    ctx = context;
    el('board-add-row').addEventListener('click', () => { if (ctx.board()) openRow(); });
    el('board-row-cancel').addEventListener('click', () => closeDialog(el('board-row-dialog')));
    el('board-row-form').addEventListener('submit', async (ev) => {
      ev.preventDefault();
      const classes = checked('board-row-classes').concat(el('board-row-more').value.split(',').map((s) => s.trim()).filter(Boolean));
      const body = { dept: el('board-row-dept').value.trim(), level: el('board-row-level').value.trim(),
        subject: el('board-row-subject').value.trim(), lessons: lessonsFrom('board-row-lessons'), classes };
      if (!body.dept || !body.level || !body.subject) { formError('board-row-error', 'Give the department, level and subject.'); return; }
      if (!Object.keys(body.lessons).length) { formError('board-row-error', 'Give at least one lesson a cycle.'); return; }
      if (!classes.length) { formError('board-row-error', 'Tick or type at least one class.'); return; }
      const btn = el('board-row-ok');
      btn.disabled = true;
      try {
        const res = await ctx.mutate('row', body, { noView: true, quiet: true });
        if (res.ok) closeDialog(el('board-row-dialog')); else formError('board-row-error', res.message);
      } finally { btn.disabled = false; }
    });

    el('board-add-band').addEventListener('click', () => { if (ctx.board() && ctx.board().dept) openBand(); });
    el('board-band-group-add').addEventListener('click', () => el('board-band-groups').appendChild(bandGroup('')));
    el('board-band-cancel').addEventListener('click', () => closeDialog(el('board-band-dialog')));
    el('board-band-form').addEventListener('submit', async (ev) => {
      ev.preventDefault();
      const codes = Array.from(el('board-band-groups').querySelectorAll('input')).map((i) => i.value.trim());
      const body = { dept: ctx.board().dept, level: el('board-band-level').value.trim(), subject: el('board-band-subject').value.trim(),
        classes: checked('board-band-classes'), lessons: lessonsFrom('board-band-lessons'),
        // a group without a code gets one made up by the server (e.g. 1ELP, 1ELQ)
        groups: codes.map((code) => (code ? { code } : { code: null })) };
      if (!body.level || !body.subject) { formError('board-band-error', 'Give the level and subject.'); return; }
      if (!Object.keys(body.lessons).length) { formError('board-band-error', 'Give at least one lesson a cycle.'); return; }
      if (!body.classes.length || !body.groups.length) { formError('board-band-error', 'Tick the classes and add at least one group.'); return; }
      const btn = el('board-band-ok');
      btn.disabled = true;
      try {
        const res = await ctx.mutate('band', body, { noView: true, quiet: true });
        if (res.ok) closeDialog(el('board-band-dialog')); else formError('board-band-error', res.message);
      } finally { btn.disabled = false; }
    });
  }

  window.BoardForms = { init };
})();
