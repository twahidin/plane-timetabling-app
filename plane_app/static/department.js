// The department page (templates/department.html; spec
// docs/superpowers/specs/2026-09-27-department-accounts-design.md §5): a head of department's own
// password, the issues of their department above the board (board.js, in department mode) and the
// live timetable read-only below it (grid.js). Calls only the routes a department account may
// reach. Until the temporary password is changed the page holds only the password form.
// DOM built with createElement/textContent; innerHTML only clears.
(function () {
  'use strict';

  const el = (id) => document.getElementById(id);
  const node = (tag, cls, text) => {
    const n = document.createElement(tag);
    if (cls) n.className = cls;
    if (text != null) n.textContent = text;
    return n;
  };
  const MIN_PASSWORD = 10;
  const first = document.body.dataset.mustChange === 'true';

  // ---------- password ----------
  const dialog = el('password-dialog');
  const formError = (text) => { const e = el('password-error'); e.textContent = text || ''; e.hidden = !text; };
  function resetForm() {
    ['password-old', 'password-new', 'password-again'].forEach((id) => { el(id).value = ''; });
    formError('');
  }
  function notify(cls, text) {
    const box = el('notes');
    if (box) { box.textContent = ''; box.appendChild(node('div', 'note ' + cls, text)); }
  }

  el('password-form').addEventListener('submit', async (ev) => {
    ev.preventDefault();
    const old = el('password-old').value, pw = el('password-new').value;
    if (pw.length < MIN_PASSWORD) { formError(`The new password needs at least ${MIN_PASSWORD} characters.`); return; }
    if (pw !== el('password-again').value) { formError('The two new passwords are not the same.'); return; }
    if (pw === old) { formError('Choose a password different from the current one.'); return; }
    const ok = el('password-ok');
    ok.disabled = true;
    try {
      const r = await fetch('/api/me/password', { method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ old, new: pw }) });
      const data = await r.json().catch(() => ({}));
      if (!r.ok) { formError(typeof data.detail === 'string' ? data.detail : r.statusText); return; }
      if (first) { window.location.reload(); return; }          // now the whole page
      resetForm();
      if (dialog.open) dialog.close();
      notify('good', 'Password changed. Other browsers where you were logged in are logged out.');
    } catch (e) {
      formError(e.message);
    } finally { ok.disabled = false; }
  });

  if (first) { el('password-old').focus(); return; }

  el('dept-password').addEventListener('click', () => {
    resetForm();
    if (typeof dialog.showModal === 'function') dialog.showModal(); else dialog.setAttribute('open', '');
    el('password-old').focus();
  });
  el('password-cancel').addEventListener('click', () => { resetForm(); if (dialog.open) dialog.close(); });
  // however the dialog closes (Cancel, Esc, a save), no typed password is left in it
  dialog.addEventListener('close', resetForm);

  // ---------- the department's issues, from every board response ----------
  window.showPlanIssues = (list, empty, counts) => {
    const box = el('dept-issues');
    box.textContent = '';
    const issues = list || [];
    const nBlocks = counts ? counts.block : issues.filter((i) => i.level === 'block').length;
    const nWarns = counts ? counts.warn : issues.filter((i) => i.level === 'warn').length;
    const more = counts ? counts.more : 0;
    const summary = node('div', 'plan-issues-summary');
    summary.appendChild(node('span', 'chip bad', `${nBlocks} blocking`));
    summary.appendChild(node('span', 'chip warn', `${nWarns} warning${nWarns === 1 ? '' : 's'}`));
    box.appendChild(summary);
    if (!issues.length) return;
    const ul = node('ul', 'plan-issue-list');
    issues.forEach((i) => { const li = node('li', i.level, i.text); li.title = i.where; ul.appendChild(li); });
    if (more > 0) ul.appendChild(node('li', 'more', `and ${more} more`));
    box.appendChild(ul);
  };

  // ---------- Print this: the printable page of what the grid shows, in a new tab ----------
  window.openPrint = (kind, id) => {
    window.open(`/print/${encodeURIComponent(kind)}/${encodeURIComponent(id)}`, '_blank', 'noopener');
  };

  if (typeof window.loadBoard === 'function') window.loadBoard();
})();
