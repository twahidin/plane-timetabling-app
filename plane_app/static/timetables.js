// Timetable instances: pick, create, clone, rename, delete. Every API call works on the current one,
// so switching reloads the model, the draft tables and the chat. Uses an in-page <dialog>, never
// native browser popups: embedded and locked-down browsers suppress those silently.
(function () {
  'use strict';
  const el = (id) => document.getElementById(id);
  const api = async (path, opts = {}) => {
    const r = await fetch(path, { headers: { 'Content-Type': 'application/json' }, ...opts });
    if (!r.ok) throw new Error((await r.json().catch(() => ({}))).detail || r.statusText);
    return r.json();
  };
  const sel = el('timetable');
  let current = null;
  let currentName = '';

  function render(data) {
    current = data.current;
    sel.textContent = '';
    data.items.forEach((t) => {
      const o = document.createElement('option');
      o.value = t.id;
      o.textContent = t.name + (t.period_base ? ' · period' : '') + (t.has_live ? ' · built' : t.has_draft ? ' · draft' : ' · empty');
      if (t.id === data.current) { o.selected = true; currentName = t.name; }
      sel.appendChild(o);
    });
    el('tt-delete').disabled = data.items.length < 2;
  }

  async function refreshEverything() {
    if (window.reloadModel) await window.reloadModel().catch(() => {});
    if (window.reloadIntake) await window.reloadIntake().catch(() => {});
    if (window.loadPeriods) await window.loadPeriods().catch(() => {});
    if (window.loadRelief) await window.loadRelief().catch(() => {});
  }

  async function load() {
    try { render(await api('/api/timetables')); } catch (e) { /* the status strip reports API failures */ }
  }
  window.loadTimetables = load;                 // periods.js reloads the switcher after creating or removing one
  window.refreshTimetable = refreshEverything;  // ... and everything the current timetable feeds after switching

  // ---------- dialog ----------
  const dlg = el('tt-dialog'), form = el('tt-form'), nameInput = el('tt-name'), errBox = el('tt-dialog-error');
  let onSubmit = null;
  function openDialog({ title, text, needsName, okLabel, initial, submit }) {
    el('tt-dialog-title').textContent = title;
    el('tt-dialog-text').textContent = text || '';
    el('tt-name-label').hidden = !needsName;
    nameInput.value = initial || '';
    nameInput.required = !!needsName;
    el('tt-ok').textContent = okLabel;
    errBox.hidden = true; errBox.textContent = '';
    onSubmit = submit;
    if (typeof dlg.showModal === 'function') dlg.showModal(); else dlg.setAttribute('open', '');
    if (needsName) { nameInput.focus(); nameInput.select(); }
  }
  function closeDialog() { if (dlg.open) dlg.close(); else dlg.removeAttribute('open'); }
  el('tt-cancel').addEventListener('click', closeDialog);
  form.addEventListener('submit', async (ev) => {
    ev.preventDefault();
    const name = nameInput.value.trim();
    if (!nameInput.hidden && nameInput.required && !name) { errBox.textContent = 'Give it a name.'; errBox.hidden = false; return; }
    el('tt-ok').disabled = true;
    try {
      await onSubmit(name);
      closeDialog();
      await refreshEverything();
    } catch (e) { errBox.textContent = e.message; errBox.hidden = false; }
    finally { el('tt-ok').disabled = false; }
  });

  // ---------- actions ----------
  sel.addEventListener('change', async () => {
    try { render(await api(`/api/timetables/${encodeURIComponent(sel.value)}/select`, { method: 'POST' })); await refreshEverything(); }
    catch (e) { await load(); if (window.loadPeriods) window.loadPeriods().catch(() => {}); }
  });
  el('tt-new').addEventListener('click', () => openDialog({
    title: 'New timetable', text: 'Starts empty. Drop documents or press "Start from criteria" to fill it.',
    needsName: true, okLabel: 'Create',
    submit: async (name) => render(await api('/api/timetables', { method: 'POST', body: JSON.stringify({ name, clone_from: null }) })),
  }));
  el('tt-clone').addEventListener('click', () => openDialog({
    title: `Clone "${currentName}"`, text: 'Copies the people, venues, lessons and time settings into a new draft. Lessons are unplaced so you can edit and rebuild.',
    needsName: true, okLabel: 'Clone', initial: `${currentName} (copy)`,
    submit: async (name) => render(await api('/api/timetables', { method: 'POST', body: JSON.stringify({ name, clone_from: current }) })),
  }));
  el('tt-rename').addEventListener('click', () => openDialog({
    title: 'Rename timetable', needsName: true, okLabel: 'Rename', initial: currentName,
    submit: async (name) => render(await api(`/api/timetables/${encodeURIComponent(current)}`, { method: 'PATCH', body: JSON.stringify({ name }) })),
  }));
  el('tt-delete').addEventListener('click', () => openDialog({
    title: `Delete "${currentName}"?`, text: 'Its draft, built timetable, uploads and chat are removed. This cannot be undone.',
    needsName: false, okLabel: 'Delete',
    submit: async () => render(await api(`/api/timetables/${encodeURIComponent(current)}`, { method: 'DELETE' })),
  }));

  load();
})();
