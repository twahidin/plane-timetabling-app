// Settings: Departments & accounts (spec docs/superpowers/specs/2026-09-27-department-accounts-design.md
// §5). The timetable heads of department work on, their accounts (Add, Reset password, Deactivate,
// Delete; Reset and Delete confirmed inline) and each department's sign-off with Reopen, as
// GET /api/deployment reports them from the deployment timetable's plan. A temporary password is
// shown once, in a dialog with a Copy button, as soon as it is made (a list refresh that fails
// after it never hides it). DOM built with createElement/textContent; innerHTML only clears.
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
  const say = (id, text, cls) => { const m = el(id); m.textContent = text || ''; m.className = 'msg-line ' + (cls || ''); };
  const when = (at) => {
    const d = at ? new Date(at) : null;
    return d && !isNaN(d) ? d.toLocaleString(undefined, { day: 'numeric', month: 'short', hour: '2-digit', minute: '2-digit' }) : 'never';
  };
  const day = (at) => {
    const d = at ? new Date(at) : null;
    return d && !isNaN(d) ? d.toLocaleDateString(undefined, { day: 'numeric', month: 'short' }) : '';
  };

  let accounts = [];
  let confirming = null;          // { id, kind: 'delete' | 'reset' }: the account whose action asks "are you sure?"

  // ---------- the temporary password, shown once ----------
  function showTemp(user, temp) {
    el('temp-password-for').textContent = `For ${user.name} (username ${user.username}, ${user.dept}).`;
    el('temp-password').textContent = temp;
    say('temp-password-msg', '');
    const dlg = el('temp-password-dialog');
    if (typeof dlg.showModal === 'function') dlg.showModal(); else dlg.setAttribute('open', '');
  }
  el('temp-password-dialog').addEventListener('close', () => { el('temp-password').textContent = ''; });
  el('temp-password-copy').addEventListener('click', async () => {
    const text = el('temp-password').textContent;
    try {
      await navigator.clipboard.writeText(text);
      say('temp-password-msg', 'Copied.', 'ok');
    } catch (e) {
      // no clipboard (an insecure page, say): select it for Ctrl+C / ⌘C
      const range = document.createRange();
      range.selectNodeContents(el('temp-password'));
      const sel = window.getSelection();
      sel.removeAllRanges();
      sel.addRange(range);
      say('temp-password-msg', 'Selected: press Ctrl+C or ⌘C to copy it.', '');
    }
  });

  // ---------- accounts ----------
  function renderAccounts() {
    const table = el('accounts-table');
    table.innerHTML = '';
    const thead = node('thead'), htr = node('tr');
    ['Name', 'Username', 'Department', 'Active', 'Last login', ''].forEach((h) => htr.appendChild(node('th', null, h)));
    thead.appendChild(htr);
    table.appendChild(thead);
    const tbody = node('tbody');
    if (!accounts.length) {
      const tr = node('tr'), td = node('td', 'empty', 'No accounts yet: Add account makes one for a head of department.');
      td.colSpan = 6;
      tr.appendChild(td);
      tbody.appendChild(tr);
    }
    accounts.forEach((u) => {
      const tr = node('tr', u.active ? '' : 'inactive');
      tr.appendChild(node('td', null, u.name));
      tr.appendChild(node('td', 'id', u.username));
      tr.appendChild(node('td', null, u.dept));
      tr.appendChild(node('td', null, u.active ? (u.must_change ? 'Yes (not logged in yet)' : 'Yes') : 'No'));
      tr.appendChild(node('td', null, when(u.last_login)));
      const actions = node('td', 'accounts-row-actions');
      const asking = confirming && confirming.id === u.id ? confirming.kind : null;
      const cancel = button('btn', 'Cancel', () => { confirming = null; renderAccounts(); });
      if (asking === 'delete') {
        actions.appendChild(node('span', null, `Delete ${u.name}? `));
        actions.appendChild(button('btn primary', 'Delete', () => act(u, () => api(`/api/users/${encodeURIComponent(u.id)}`, { method: 'DELETE' }), `${u.name}'s account is deleted.`)));
        actions.appendChild(cancel);
      } else if (asking === 'reset') {
        actions.appendChild(node('span', null, `Reset ${u.name}'s password? They are logged out everywhere. `));
        actions.appendChild(button('btn primary', 'Reset password', (ev) => resetPassword(u, ev.target)));
        actions.appendChild(cancel);
      } else {
        actions.appendChild(button('btn', 'Reset password', () => { confirming = { id: u.id, kind: 'reset' }; say('accounts-msg', ''); renderAccounts(); }));
        actions.appendChild(button('btn', u.active ? 'Deactivate' : 'Activate', () => act(u,
          () => api(`/api/users/${encodeURIComponent(u.id)}`, { method: 'PATCH', body: JSON.stringify({ active: !u.active }) }),
          u.active ? `${u.name} can no longer log in.` : `${u.name} can log in again.`)));
        actions.appendChild(button('btn', 'Delete', () => { confirming = { id: u.id, kind: 'delete' }; say('accounts-msg', ''); renderAccounts(); }));
      }
      tr.appendChild(actions);
      tbody.appendChild(tr);
    });
    table.appendChild(tbody);
  }

  // after a change: the accounts, then the departments (whose "no account" notes follow them); a
  // failure here is said, never thrown over what the change itself showed
  async function refresh() {
    try {
      await loadAccounts();
      await loadDeployment();
    } catch (e) { say('accounts-msg', `The lists did not refresh: ${e.message}. Reload the page.`, 'bad'); }
  }

  async function act(u, call, done) {
    try {
      await call();
    } catch (e) { say('accounts-msg', e.message, 'bad'); return; }
    confirming = null;
    say('accounts-msg', done, 'ok');
    await refresh();
  }

  async function resetPassword(u, btn) {
    btn.disabled = true;
    let res;
    try {
      res = await api(`/api/users/${encodeURIComponent(u.id)}/reset`, { method: 'POST' });
    } catch (e) { btn.disabled = false; say('accounts-msg', e.message, 'bad'); return; }
    confirming = null;
    showTemp(u, res.temp_password);                  // at once: the only time it can be seen
    say('accounts-msg', `${u.name}'s password is reset.`, 'ok');
    await refresh();
  }

  async function loadAccounts() {
    const data = await api('/api/users');
    accounts = data.users || [];
    renderAccounts();
  }

  function newForm(open) {
    el('accounts-new').hidden = !open;
    el('accounts-add').hidden = open;
    if (open) {
      ['account-username', 'account-name', 'account-dept'].forEach((id) => { el(id).value = ''; });
      el('account-username').focus();
    }
  }
  el('accounts-add').addEventListener('click', () => { say('accounts-msg', ''); newForm(true); });
  el('account-cancel').addEventListener('click', () => newForm(false));
  el('account-create').addEventListener('click', async () => {
    const body = { username: el('account-username').value.trim().toLowerCase(), name: el('account-name').value.trim(),
      dept: el('account-dept').value.trim() };
    if (!body.username || !body.name || !body.dept) { say('accounts-msg', 'Give the username, name and department.', 'bad'); return; }
    const btn = el('account-create');
    btn.disabled = true;
    let res;
    try {
      res = await api('/api/users', { method: 'POST', body: JSON.stringify(body) });
    } catch (e) { say('accounts-msg', e.message, 'bad'); return; }
    finally { btn.disabled = false; }
    newForm(false);
    showTemp(res.user, res.temp_password);           // at once: the only time it can be seen
    say('accounts-msg', `${res.user.name}'s account is made.`, 'ok');
    await refresh();                                 // the first account fixes the timetable
  });

  // ---------- the deployment timetable ----------
  // { timetable, name, stored, rev, departments: {dept: {status, by, at, accounts}} }: the
  // departments are read from the deployment timetable's plan, whichever timetable is open
  let deployment = null;
  let current = null;             // the timetable the admin has open
  async function loadDeployment() {
    const [dep, tts] = await Promise.all([api('/api/deployment'), api('/api/timetables')]);
    deployment = dep;
    current = tts.current;
    const sel = el('deployment-timetable');
    sel.innerHTML = '';
    (tts.items || []).filter((t) => !t.period_base).forEach((t) => {
      const o = node('option', null, t.name || t.id);
      o.value = t.id;
      sel.appendChild(o);
    });
    sel.value = dep.timetable;
    renderDepartments();
  }
  el('deployment-timetable').addEventListener('change', async (ev) => {
    const sel = ev.target;
    sel.disabled = true;
    try {
      deployment = await api('/api/deployment', { method: 'PUT', body: JSON.stringify({ timetable: sel.value }) });
      say('deployment-msg', `Heads of department now work on ${deployment.name}.`, 'ok');
      renderDepartments();
    } catch (e) {
      say('deployment-msg', e.message, 'bad');
      if (deployment) sel.value = deployment.timetable;
    } finally { sel.disabled = false; }
  });

  // ---------- departments: sign-off and Reopen ----------
  // Reopen goes through the board, which works on the timetable the admin has open: it is offered
  // only while that is the deployment timetable.
  function renderDepartments() {
    const box = el('departments-status');
    box.textContent = '';
    if (!deployment) return;
    const departments = deployment.departments || {};
    const depts = Object.keys(departments).sort();
    if (!depts.length) {
      box.appendChild(node('p', 'help', `No departments in ${deployment.name}'s plan yet: upload a staff deployment workbook on the Plan tab.`));
      return;
    }
    const open = current === deployment.timetable;
    const list = node('ul', 'departments-list');
    let anySubmitted = false;
    depts.forEach((d) => {
      const st = departments[d] || {};
      const submitted = st.status === 'submitted';
      anySubmitted = anySubmitted || submitted;
      const li = node('li', submitted ? 'submitted' : '');
      li.appendChild(node('b', null, d));
      li.appendChild(node('span', 'dept-state', submitted
        ? `Submitted${st.by ? ` by ${st.by}` : ''}${st.at ? ` on ${day(st.at)}` : ''}` : 'Open'));
      if (!st.accounts) li.appendChild(node('span', 'help', 'no account'));
      if (submitted && open) {
        li.appendChild(button('btn', 'Reopen', async (ev) => {
          ev.target.disabled = true;
          try {
            await api('/api/plan/board/reopen', { method: 'POST', body: JSON.stringify({ dept: d, rev: deployment.rev }) });
            say('accounts-msg', `${d} is open again.`, 'ok');
          } catch (e) { say('accounts-msg', e.message, 'bad'); }
          try { await loadDeployment(); } catch (e) { say('accounts-msg', `The departments did not refresh: ${e.message}`, 'bad'); }
        }));
      }
      list.appendChild(li);
    });
    box.appendChild(list);
    if (anySubmitted && !open) {
      box.appendChild(node('p', 'help', `To reopen a department, open ${deployment.name} on the timetable page first.`));
    }
  }

  // the accounts first, then the departments
  loadAccounts().then(loadDeployment).catch((e) => say('accounts-msg', e.message, 'bad'));
})();
