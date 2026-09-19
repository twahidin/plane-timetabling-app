// The Print dialog: pick a kind and a target (or build a custom set), then View (a new tab with
// the HTML grid) or PDF (a download). Mirrors timetables.js's in-page <dialog> convention — no
// native prompt()/confirm()/alert(), which some embedded browsers suppress silently.
(function () {
  'use strict';

  const el = (id) => document.getElementById(id);
  const api = async (path, opts = {}) => {
    const r = await fetch(path, { headers: { 'Content-Type': 'application/json' }, ...opts });
    if (!r.ok) throw new Error((await r.json().catch(() => ({}))).detail || r.statusText);
    return r.json();
  };

  const dlg = el('print-dialog');
  const kindSel = el('print-kind'), filterInput = el('print-filter'), targetSel = el('print-target');
  const titleLabel = el('print-title-label'), titleInput = el('print-title');
  const viewSel = el('print-view'), dateLabel = el('print-date-label'), dateInput = el('print-date');
  const errBox = el('print-error'), noteBox = el('print-note');
  const allTeachers = el('print-all-teachers'), allClasses = el('print-all-classes'), allRooms = el('print-all-rooms');

  let targets = null;     // last /api/print/targets response
  let items = [];         // {id, name} list for the current kind, unfiltered
  let pendingId = null;    // preselect once the options for the requested kind are built

  function showError(msg) { errBox.textContent = msg || ''; errBox.hidden = !msg; }
  function showNote(msg) { noteBox.textContent = msg || ''; noteBox.hidden = !msg; }

  // A PDF is rendered synchronously: "all teachers" on a large school takes tens of seconds. Say so,
  // and block a second click until the download navigation starts. There is no job queue, so the only
  // signal that it started is the navigation itself; re-enable after 20 s in case it never does.
  let busy = false, busyTimer = null;
  function setBusy(on) {
    busy = on;
    showNote(on ? 'Generating PDF… this can take a while for all teachers' : '');
    [el('print-pdf-btn'), el('print-view-btn'), allTeachers, allClasses, allRooms].forEach((b) => {
      if (!b) return;
      if (b.tagName === 'A') b.setAttribute('aria-disabled', on ? 'true' : 'false');
      else b.disabled = on;
    });
    if (busyTimer) { clearTimeout(busyTimer); busyTimer = null; }
    if (on) busyTimer = setTimeout(() => setBusy(false), 20000);
  }

  function itemsFor(kind) {
    if (!targets) return [];
    if (kind === 'teacher' || kind === 'custom') return targets.teachers || [];
    if (kind === 'group') return targets.groups || [];
    if (kind === 'room') return targets.rooms || [];
    if (kind === 'class') return (targets.classes || []).map((c) => ({ id: c, name: c }));
    return [];
  }

  function rebuildOptions() {
    const q = filterInput.value.trim().toLowerCase();
    targetSel.textContent = '';
    items.filter((it) => !q || it.name.toLowerCase().indexOf(q) !== -1 || it.id.toLowerCase().indexOf(q) !== -1)
      .forEach((it) => {
        const o = document.createElement('option');
        o.value = it.id; o.textContent = it.name;
        targetSel.appendChild(o);
      });
    if (pendingId != null) {
      const opt = Array.prototype.find.call(targetSel.options, (o) => o.value === pendingId);
      if (opt) opt.selected = true;
      pendingId = null;
    }
  }

  function onKindChange() {
    const kind = kindSel.value;
    items = itemsFor(kind);
    targetSel.multiple = kind === 'custom';
    titleLabel.hidden = kind !== 'custom';
    rebuildOptions();
  }

  function weekQuery() {
    if (viewSel.value !== 'week') return '';
    const d = dateInput.value;
    return '?view=week' + (d ? '&date=' + encodeURIComponent(d) : '');
  }

  function updateAllLinks() {
    const q = weekQuery();
    allTeachers.href = '/print/all/teachers.pdf' + q;
    allClasses.href = '/print/all/classes.pdf' + q;
    allRooms.href = '/print/all/rooms.pdf' + q;
  }

  function onViewChange() {
    dateLabel.hidden = viewSel.value !== 'week';
    updateAllLinks();
  }

  async function loadTargets() {
    try {
      targets = await api('/api/print/targets');
      allClasses.hidden = !(targets.classes || []).length;
    } catch (e) {
      targets = { teachers: [], groups: [], classes: [], rooms: [] };
      allClasses.hidden = true;
      showError(e.message);
    }
  }

  async function openDialog(kind, id) {
    showError('');
    setBusy(false);
    await loadTargets();
    kindSel.value = kind || 'teacher';
    pendingId = id || null;
    filterInput.value = '';
    onKindChange();
    onViewChange();
    if (typeof dlg.showModal === 'function') dlg.showModal(); else dlg.setAttribute('open', '');
  }
  function closeDialog() { if (dlg.open) dlg.close(); else dlg.removeAttribute('open'); }

  function selectedPersons() {
    return Array.prototype.map.call(targetSel.selectedOptions || [], (o) => o.value);
  }

  async function customToken() {
    const persons = selectedPersons();
    if (!persons.length) throw new Error('Pick at least one person.');
    const title = titleInput.value.trim() || 'Custom timetable';
    const r = await api('/api/print/custom', { method: 'POST', body: JSON.stringify({ title, persons, events: [] }) });
    return r.token;
  }

  async function targetUrl() {
    const kind = kindSel.value;
    if (kind === 'custom') return '/print/custom/' + (await customToken());
    const id = targetSel.value;
    if (!id) throw new Error('Pick one from the list.');
    return '/print/' + kind + '/' + encodeURIComponent(id);
  }

  el('print-open').addEventListener('click', () => openDialog('teacher', null));
  kindSel.addEventListener('change', onKindChange);
  filterInput.addEventListener('input', rebuildOptions);
  viewSel.addEventListener('change', onViewChange);
  el('print-cancel').addEventListener('click', closeDialog);

  el('print-view-btn').addEventListener('click', async () => {
    showError('');
    try {
      const url = await targetUrl();
      window.open(url + weekQuery(), '_blank');
    } catch (e) { showError(e.message); }
  });
  el('print-pdf-btn').addEventListener('click', async () => {
    showError('');
    if (busy) return;
    try {
      const url = await targetUrl();
      setBusy(true);
      window.location = url + '.pdf' + weekQuery();
    } catch (e) { setBusy(false); showError(e.message); }
  });
  [allTeachers, allClasses, allRooms].forEach((a) => {
    if (!a) return;
    a.addEventListener('click', (ev) => {
      if (busy) { ev.preventDefault(); return; }
      setBusy(true);
    });
  });

  // Preselected open from the prism panel: window.openPrint('teacher', 'lee').
  window.openPrint = (kind, id) => { openDialog(kind, id).catch((e) => showError(e.message)); };
})();
