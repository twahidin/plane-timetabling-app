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
  const stuClass = el('print-student-class'), stuLevel = el('print-student-level'), stuAll = el('print-student-all');

  let targets = null;     // last /api/print/targets response
  let items = [];         // {id, name} list for the current kind, unfiltered
  let pendingId = null;    // preselect once the options for the requested kind are built

  function showError(msg) { errBox.textContent = msg || ''; errBox.hidden = !msg; }
  function showNote(msg) { noteBox.textContent = msg || ''; noteBox.hidden = !msg; }

  // A PDF is rendered synchronously: "all teachers" (or every student) on a large school takes tens of seconds. Say so,
  // and block a second click until the download navigation starts. There is no job queue, so the only
  // signal that it started is the navigation itself; re-enable after 20 s in case it never does.
  let busy = false, busyTimer = null;
  function setBusy(on) {
    busy = on;
    showNote(on ? 'Generating PDF… this can take a while' : '');
    [el('print-pdf-btn'), el('print-view-btn'), allTeachers, allClasses, allRooms, stuClass, stuLevel, stuAll].forEach((b) => {
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
    // a student is found by name, ID or class: one text carries all three
    if (kind === 'student') return (targets.students || []).map((s) => ({ id: s.id, name: `${s.name} · ${s.id} · ${s.class}` }));
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
    updateStudentLinks();
  }

  function onKindChange() {
    const kind = kindSel.value;
    items = itemsFor(kind);
    targetSel.multiple = kind === 'custom';
    titleLabel.hidden = kind !== 'custom';
    rebuildOptions();
    onViewChange();
  }

  // A student's timetable is the cycle only (no dated week).
  const cycleOnly = () => kindSel.value === 'student';

  // The admin alone is offered students: the server lists them in the targets only for the admin, and
  // /api/me is asked as well. The Student kind is added to the picker only then.
  async function offerStudents() {
    let admin = false;
    try { admin = (await api('/api/me')).role === 'admin'; } catch (e) { admin = false; }
    const has = Array.prototype.some.call(kindSel.options, (o) => o.value === 'student');
    if (admin && (targets.students || []).length && !has) {
      const o = document.createElement('option');
      o.value = 'student'; o.textContent = 'Student';
      kindSel.insertBefore(o, kindSel.querySelector('option[value="custom"]'));
    } else if (!(admin && (targets.students || []).length) && has) {
      Array.prototype.find.call(kindSel.options, (o) => o.value === 'student').remove();
    }
  }

  // The class, level and everyone's PDFs for the chosen student: one student a page, class then ID.
  function updateStudentLinks() {
    const on = kindSel.value === 'student';
    const mine = on ? (targets.students || []).find((s) => s.id === targetSel.value) : null;
    stuClass.hidden = !mine;
    stuLevel.hidden = !(mine && mine.level);
    stuAll.hidden = !on;
    if (mine) {
      stuClass.textContent = 'Class ' + mine.class + ' PDF';
      stuClass.href = '/print/students.pdf?class=' + encodeURIComponent(mine.class);
      if (mine.level) {
        stuLevel.textContent = 'Level ' + mine.level + ' PDF';
        stuLevel.href = '/print/students.pdf?level=' + encodeURIComponent(mine.level);
      }
    }
    stuAll.href = '/print/students.pdf?all=1';
  }

  function weekQuery() {
    if (viewSel.value !== 'week' || cycleOnly()) return '';
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
    viewSel.disabled = cycleOnly();
    dateLabel.hidden = viewSel.value !== 'week' || cycleOnly();
    updateAllLinks();
    updateStudentLinks();
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
    await offerStudents();
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
  targetSel.addEventListener('change', updateStudentLinks);
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
  [allTeachers, allClasses, allRooms, stuClass, stuLevel, stuAll].forEach((a) => {
    if (!a) return;
    a.addEventListener('click', (ev) => {
      if (busy) { ev.preventDefault(); return; }
      setBusy(true);
    });
  });

  // Preselected open from the Selected card: window.openPrint('teacher', 'lee').
  window.openPrint = (kind, id) => { openDialog(kind, id).catch((e) => showError(e.message)); };
})();
