// Period timetables: the Periods card (list, New period, Open / Refresh / Remove) and the banner under
// the header that says when a period is in force today or when the timetable on screen is a period.
// A period is its own timetable instance, so opening one is a timetable switch like the header's.
(function () {
  'use strict';
  const el = (id) => document.getElementById(id);
  const api = async (path, opts = {}) => {
    const r = await fetch(path, { headers: { 'Content-Type': 'application/json' }, ...opts });
    if (!r.ok) throw new Error((await r.json().catch(() => ({}))).detail || r.statusText);
    return r.json();
  };
  const node = (tag, cls, text) => { const n = document.createElement(tag); if (cls) n.className = cls; if (text != null) n.textContent = text; return n; };

  const DAYS = ['Sun', 'Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat'];
  const MONTHS = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec'];
  const parse = (iso) => { const [y, m, d] = iso.split('-').map(Number); return new Date(y, m - 1, d); };
  const dayDate = (iso) => { const d = parse(iso); return `${DAYS[d.getDay()]} ${d.getDate()} ${MONTHS[d.getMonth()]}`; };
  function range(from, to) {                     // "6 to 10 Oct", "29 Sep to 3 Oct", "29 Dec 2026 to 2 Jan 2027"
    const a = parse(from), b = parse(to);
    if (from === to) return `${a.getDate()} ${MONTHS[a.getMonth()]}`;
    if (a.getFullYear() !== b.getFullYear()) return `${a.getDate()} ${MONTHS[a.getMonth()]} ${a.getFullYear()} to ${b.getDate()} ${MONTHS[b.getMonth()]} ${b.getFullYear()}`;
    if (a.getMonth() !== b.getMonth()) return `${a.getDate()} ${MONTHS[a.getMonth()]} to ${b.getDate()} ${MONTHS[b.getMonth()]}`;
    return `${a.getDate()} to ${b.getDate()} ${MONTHS[b.getMonth()]}`;
  }
  function scopeWords(scope) {
    if (!scope || scope.all) return 'whole school';
    const parts = (scope.levels || []).map((l) => `Sec ${l}`).concat((scope.classes || []).map((c) => c.toUpperCase()));
    return parts.join(', ');
  }

  let data = null;
  const note = (text) => { const n = el('periods-note'); n.textContent = text || ''; n.hidden = !text; };

  // Everything the current timetable feeds (model, grid, draft, chat, this card) after a switch.
  async function afterSwitch() {
    if (window.loadTimetables) await window.loadTimetables();
    if (window.refreshTimetable) await window.refreshTimetable();
    else {
      if (window.reloadModel) await window.reloadModel().catch(() => {});
      if (window.loadDraft) await window.loadDraft();
      await window.loadPeriods();
    }
  }
  async function select(tid) {
    try { await api(`/api/timetables/${encodeURIComponent(tid)}/select`, { method: 'POST' }); }
    catch (e) { note(e.message); return; }
    await afterSwitch();
  }

  function renderBanner() {
    const banner = el('period-banner'), text = el('period-banner-text');
    const mine = data && data.this ? data.items.find((p) => p.id === data.this) : null;
    const today = data && !mine && data.today ? data.items.find((p) => p.id === data.today) : null;
    el('period-banner-open').hidden = !today;
    el('period-banner-back').hidden = !mine;
    if (mine) text.textContent = `This is the ${mine.name} timetable (${range(mine.from, mine.to)}) —`;
    else if (today) text.textContent = `${today.name} is in force until ${dayDate(today.to)} —`;
    banner.hidden = !(mine || today);
  }

  function renderList() {
    const list = el('periods-list');
    list.textContent = '';
    el('period-new').disabled = !!data.this;
    el('period-new').title = data.this ? 'Open the normal timetable to add a period' : 'A timetable in force for some dates, such as an exam week';
    if (!data.items.length) {
      list.appendChild(node('li', 'empty', 'No periods. A period is a timetable in force for some dates, such as an exam week or a camp; on those dates it replaces the normal timetable for the classes it covers.'));
      return;
    }
    data.items.forEach((p) => {
      const li = node('li', 'period' + (p.id === data.this ? ' this' : ''));
      const head = node('div', 'period-head');
      head.appendChild(node('b', null, p.name));
      if (p.id === data.today) head.appendChild(node('span', 'badge', 'in force now'));
      if (p.id === data.this) head.appendChild(node('span', 'badge quiet', 'open'));
      li.appendChild(head);
      li.appendChild(node('div', 'period-meta', `${dayDate(p.from)} to ${dayDate(p.to)} · ${scopeWords(p.scope)}`));
      const btns = node('div', 'period-btns');
      const open = node('button', 'btn', 'Open'); open.type = 'button';
      open.disabled = p.id === data.this;
      open.addEventListener('click', () => select(p.timetable));
      const refresh = node('button', 'btn', 'Refresh'); refresh.type = 'button';
      refresh.title = 'Copy the normal timetable’s other lessons into it again, keeping what the period has of its own';
      refresh.addEventListener('click', async () => {
        refresh.disabled = true;
        try {
          const r = await api(`/api/periods/${encodeURIComponent(p.id)}/refresh`, { method: 'POST' });
          note(`${p.name}: ${r.replaced} lessons copied from the normal timetable into its draft, ${r.kept} of its own kept` +
            (r.clashes == null ? '. The clash check could not run.' : r.clashes ? `, ${r.clashes} clashes to fix.` : ', no clashes.') +
            ' Build it again — Quick or Best timetable — for its dates to use this.');
          if (p.id === data.this) {
            if (window.reloadModel) window.reloadModel().catch(() => {});
            if (window.loadDraft) window.loadDraft();
          }
          await window.loadPeriods();
        } catch (e) { note(e.message); }
        finally { refresh.disabled = false; }
      });
      const remove = node('button', 'btn', 'Remove'); remove.type = 'button';
      remove.addEventListener('click', () => askRemove(p));
      btns.append(open, refresh, remove);
      if (p.base_changes > 0) {
        btns.appendChild(node('span', 'period-changes',
          `${p.base_changes >= 20 ? '20+' : p.base_changes} ${p.base_changes === 1 ? 'change' : 'changes'} on the normal timetable since this period was made`));
      }
      li.appendChild(btns);
      list.appendChild(li);
    });
  }

  // Remove asks in an in-page dialog, never window.confirm: embedded browsers suppress native popups.
  const rmDlg = el('period-remove-dialog');
  let removing = null;
  function askRemove(p) {
    removing = p;
    el('period-remove-title').textContent = `Remove the period "${p.name}"?`;
    el('period-remove-error').hidden = true;
    if (typeof rmDlg.showModal === 'function') rmDlg.showModal(); else rmDlg.setAttribute('open', '');
  }
  const closeRemove = () => { if (rmDlg.open) rmDlg.close(); else rmDlg.removeAttribute('open'); };
  async function doRemove(alsoTimetable) {
    const p = removing;
    if (!p) return;
    ['period-remove-keep', 'period-remove-delete'].forEach((id) => { el(id).disabled = true; });
    try {
      await api(`/api/periods/${encodeURIComponent(p.id)}${alsoTimetable ? '?timetable=1' : ''}`, { method: 'DELETE' });
      closeRemove();
      note(`${p.name} removed` + (alsoTimetable ? ' with its timetable.' : '; its timetable stays as an ordinary one.'));
      await afterSwitch();
    } catch (e) { el('period-remove-error').textContent = e.message; el('period-remove-error').hidden = false; }
    finally { ['period-remove-keep', 'period-remove-delete'].forEach((id) => { el(id).disabled = false; }); }
  }
  el('period-remove-cancel').addEventListener('click', closeRemove);
  el('period-remove-keep').addEventListener('click', () => doRemove(false));
  el('period-remove-delete').addEventListener('click', () => doRemove(true));

  window.loadPeriods = async () => {
    try { data = await api('/api/periods'); }
    catch (e) { data = null; el('period-banner').hidden = true; return; }
    renderList();
    renderBanner();
  };

  // ---------- banner ----------
  el('period-banner-open').addEventListener('click', () => { if (data && data.today) select(data.items.find((p) => p.id === data.today).timetable); });
  el('period-banner-back').addEventListener('click', () => { if (data) select(data.base); });

  // ---------- New period dialog ----------
  const dlg = el('period-dialog'), form = el('period-form'), errBox = el('period-error');
  const scopeValue = () => (form.querySelector('input[name="period-scope"]:checked') || {}).value || 'all';
  function syncScope() {
    el('period-levels-label').hidden = scopeValue() !== 'levels';
    el('period-classes-label').hidden = scopeValue() !== 'classes';
  }
  function fill(select, values, label) {
    select.textContent = '';
    values.forEach((v) => { const o = node('option', null, label(v)); o.value = v; select.appendChild(o); });
  }
  form.querySelectorAll('input[name="period-scope"]').forEach((r) => r.addEventListener('change', syncScope));
  el('period-new').addEventListener('click', async () => {
    await window.loadPeriods();
    if (!data) return;
    form.reset();
    fill(el('period-levels'), data.levels, (l) => `Sec ${l}`);
    fill(el('period-classes'), data.classes, (c) => c);
    form.querySelector('input[value="levels"]').disabled = !data.levels.length;
    form.querySelector('input[value="classes"]').disabled = !data.classes.length;
    el('period-from').value = data.today_date; el('period-to').value = data.today_date;
    errBox.hidden = true; errBox.textContent = '';
    syncScope();
    if (typeof dlg.showModal === 'function') dlg.showModal(); else dlg.setAttribute('open', '');
    el('period-name').focus();
  });
  const close = () => { if (dlg.open) dlg.close(); else dlg.removeAttribute('open'); };
  el('period-cancel').addEventListener('click', close);
  form.addEventListener('submit', async (ev) => {
    ev.preventDefault();
    const picked = (id) => Array.from(el(id).selectedOptions).map((o) => o.value);
    const kind = scopeValue();
    const scope = kind === 'levels' ? { levels: picked('period-levels') } : kind === 'classes' ? { classes: picked('period-classes') } : { all: true };
    const body = { name: el('period-name').value.trim(), from: el('period-from').value, to: el('period-to').value, scope };
    const fail = (msg) => { errBox.textContent = msg; errBox.hidden = false; };
    if (!body.name) return fail('Give the period a name.');
    if (!body.from || !body.to) return fail('Pick the first and the last day.');
    if (kind !== 'all' && !(scope.levels || scope.classes).length) return fail(kind === 'levels' ? 'Pick at least one level.' : 'Pick at least one class.');
    el('period-ok').disabled = true;
    try {
      const r = await api('/api/periods', { method: 'POST', body: JSON.stringify(body) });
      close();
      note(`${r.period.name} is open. ${r.dropped} lessons it covers were left out for you to plan: fill it in by chat, workbook or plan, then Quick or Best timetable. Its other lessons are copied from the normal timetable and pinned.`);
      await afterSwitch();
    } catch (e) { fail(e.message); }
    finally { el('period-ok').disabled = false; }
  });

  window.loadPeriods();
})();
