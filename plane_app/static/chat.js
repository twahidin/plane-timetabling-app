(function () {
  'use strict';

  const el = (id) => document.getElementById(id);
  const api = async (path, opts = {}) => {
    const r = await fetch(path, { headers: { 'Content-Type': 'application/json' }, ...opts });
    if (!r.ok) throw new Error((await r.json().catch(() => ({}))).detail || r.statusText);
    return r.json();
  };
  const node = (tag, cls, text) => {
    const n = document.createElement(tag);
    if (cls) n.className = cls;
    if (text != null) n.textContent = text;
    return n;
  };

  // ---------- Chat log ----------
  const log = el('chat-log');
  function scrollLog() { log.scrollTop = log.scrollHeight; }
  function appendBubble(role, text) {
    const b = node('div', 'msg ' + role, text);
    log.appendChild(b); scrollLog(); return b;
  }
  function appendTool(name, result) {
    const d = node('details', 'tool');
    d.appendChild(node('summary', null, 'used tool ' + name));
    let pretty = result;
    try { pretty = JSON.stringify(JSON.parse(result), null, 1); } catch (e) { /* keep the raw text */ }
    d.appendChild(node('pre', null, String(pretty == null ? '' : pretty)));
    log.appendChild(d); scrollLog();
  }
  function renderMessage(m) {
    const c = m.content || {};
    if (m.role === 'user') appendBubble('user', c.text || '');
    else if (m.role === 'tool') appendTool(c.name || '?', c.result);
    else if (m.role === 'assistant') {
      if (c.text) appendBubble('assistant', c.text);
      // the tool call itself is echoed by the following tool message; nothing to show here when text is empty
    }
  }
  async function loadMessages() {
    log.textContent = '';
    try {
      const msgs = await api('/api/messages');
      msgs.forEach(renderMessage);
      if (!msgs.length) appendBubble('assistant', 'Ask me where someone is, who is in a room, when two people are both free, or tell me to change or build the draft.');
    } catch (e) { appendBubble('error', e.message); }
  }

  // ---------- Proposal cards ----------
  // A card per pending option: the model's `propose`/`book`/`undo` tools only stage a change; nothing
  // is real until Apply is clicked (or a confirm word / later `apply` call takes the same route
  // server-side). Every card in the batch that made the id being applied or the dismiss call is
  // greyed out together, since the server clears the whole pending list on either action, except
  // after applying a cover card (relief): the server takes that one card and keeps the rest on offer,
  // so only it is greyed out and the other lessons' cards stay clickable.
  function fmtDelta(delta) {
    return Object.entries(delta || {}).filter(([, v]) => v)
      .map(([k, v]) => `${k} ${v > 0 ? '+' : ''}${v}`).join(' · ');
  }
  function markBatchDone(ids) {
    (ids || []).forEach((id) => { const c = el('proposal-' + id); if (c) c.classList.add('done'); });
  }
  async function applyProposal(item, batchIds) {
    let res;
    try { res = await api('/api/proposals/apply', { method: 'POST', body: JSON.stringify({ id: item.id }) }); }
    catch (e) { appendBubble('error', e.message); return; }
    // A cover card is greyed out alone and only once applied: a refused one stays on offer (the
    // server keeps it pending, and it may become applicable once something else changes).
    if (item.kind !== 'cover' || res.ok) markBatchDone(item.kind === 'cover' ? [item.id] : batchIds);
    if (res.ok) {
      appendBubble('assistant', 'Applied: ' + res.description);
      // The Relief card's counts: a cover adds one, and an undo may take one back.
      if ((item.kind === 'cover' || item.kind === 'undo') && window.loadRelief) window.loadRelief().catch(() => {});
      if (window.reloadModel) window.reloadModel().catch(() => {});
      loadDraft();
    } else {
      const clashText = (res.clashes || []).map((c) => c.message || String(c)).join('; ');
      appendBubble('error', 'Not applied: ' + res.description + (clashText ? ' — ' + clashText : ''));
    }
  }
  async function dismissProposals(batchIds) {
    try { await api('/api/proposals/dismiss', { method: 'POST' }); }
    catch (e) { appendBubble('error', e.message); return; }
    markBatchDone(batchIds);
    if (window.loadRelief) window.loadRelief().catch(() => {});   // dismissed cover cards no longer wait in the chat
  }
  // Card ids can repeat: a cover plan returns every pending cover card again, with ids made from the
  // absence, date and lesson, so replanning restages cards already in the chat. The newest copy
  // replaces the old one, keeping one card (and one Apply) per id.
  function replaceProposalCard(card) {
    const old = el(card.id);
    if (old) old.remove();
    log.appendChild(card);
  }
  function appendProposals(items) {
    const batchIds = (items || []).map((i) => i.id);
    (items || []).forEach((item) => {
      const card = node('div', 'proposal');
      card.id = 'proposal-' + item.id;
      card.appendChild(node('div', null, item.text));
      const deltaText = fmtDelta(item.delta);
      if (deltaText) card.appendChild(node('div', 'delta', deltaText));
      (item.review || []).forEach((c) => card.appendChild(node('div', 'error', c.message || String(c))));
      const actions = node('div', 'actions');
      const applyBtn = node('button', 'btn apply', 'Apply'); applyBtn.type = 'button';
      const dismissBtn = node('button', 'btn dismiss', 'Dismiss'); dismissBtn.type = 'button';
      applyBtn.hidden = !!item.uncovered;           // a cover card with no teacher free has nothing to apply
      applyBtn.addEventListener('click', () => applyProposal(item, batchIds));
      dismissBtn.addEventListener('click', () => dismissProposals(batchIds));
      actions.appendChild(applyBtn); actions.appendChild(dismissBtn);
      card.appendChild(actions);
      replaceProposalCard(card);
    });
    scrollLog();
  }

  function handleEvents(events) {
    let reload = false;
    (events || []).forEach((ev) => {
      if (ev.kind === 'build') {
        if (ev.ok) { reload = true; showNotes([], [], [{ cls: 'good', text: `Built: ${(ev.placed || []).length} events placed. The draft is now the live timetable.` }]); }
        else showNotes([], [], [{ cls: 'error', text: `Build did not settle: ${(ev.unplaced || []).length} unplaced, ${(ev.clashes || []).length} clashes.` }]
          .concat((ev.clashes || []).map((c) => ({ cls: 'error', text: c.message || String(c) }))));
      } else if (ev.kind === 'draft_updated') reload = true;
      else if (ev.kind === 'proposals') appendProposals(ev.items);
      else if (ev.kind === 'applied') {
        const clashText = (ev.clashes || []).map((c) => c.message || String(c)).join('; ');
        appendBubble(ev.ok ? 'assistant' : 'error', (ev.ok ? 'Applied: ' : 'Not applied: ') + ev.description
          + (!ev.ok && clashText ? ' — ' + clashText : ''));
        if (ev.ok) reload = true;
      } else if (ev.kind === 'dismissed') {
        document.querySelectorAll('.proposal').forEach((c) => c.classList.add('done'));
        appendBubble('assistant', 'Dismissed.');
        if (window.loadRelief) window.loadRelief().catch(() => {});
      } else if (ev.kind === 'undone') reload = true;
      else if (ev.kind === 'bookings_updated') loadBookings();
      else if (ev.kind === 'relief') { if (window.loadRelief) window.loadRelief().catch(() => {}); }
      else if (ev.kind === 'periods') { if (window.loadPeriods) window.loadPeriods().catch(() => {}); if (window.loadTimetables) window.loadTimetables(); }
      else if (ev.kind === 'plan_updated') { if (window.loadPlan) window.loadPlan().catch(() => {}); if (window.loadWizard) window.loadWizard().catch(() => {}); }
      else if (ev.kind === 'wizard') { if (window.renderWizardEvent) window.renderWizardEvent(ev); }
      else if (ev.kind === 'settings_updated') {
        if (window.reloadModel) window.reloadModel().catch(() => {});
        if (window.loadWizard) window.loadWizard().catch(() => {});
      }
    });
    if (reload) { if (window.reloadModel) window.reloadModel().catch(() => {}); loadDraft(); }
  }
  window.handleEvents = handleEvents;

  window.sendChat = (text) => { el('chat-text').value = text; el('chat-form').requestSubmit(); };
  window.draftChat = (text) => { const input = el('chat-text'); input.value = text; input.focus(); };

  el('undo').addEventListener('click', async () => {
    const btn = el('undo'); btn.disabled = true;
    try {
      const res = await api('/api/undo', { method: 'POST' });
      appendBubble('assistant', res.ok ? 'Undid: ' + res.undone : 'Nothing to undo');
      if (res.ok) {
        if (window.reloadModel) window.reloadModel().catch(() => {});
        loadDraft();
        if (window.loadRelief) window.loadRelief().catch(() => {});   // the change undone may have been a cover
      }
    } catch (e) { appendBubble('error', e.message); }
    finally { btn.disabled = false; }
  });

  el('chat-form').addEventListener('submit', async (ev) => {
    ev.preventDefault();
    const input = el('chat-text'), text = input.value.trim();
    if (!text) return;
    input.value = '';
    appendBubble('user', text);
    const pending = appendBubble('assistant pending', '');
    pending.appendChild(node('span', 'spinner', ''));
    pending.appendChild(node('span', null, modelLabel ? `Thinking with ${modelLabel}\u2026` : 'Thinking\u2026'));
    const btn = ev.target.querySelector('button'); btn.disabled = true;
    try {
      const res = await api('/api/chat', { method: 'POST', body: JSON.stringify({ text }) });
      pending.remove();
      // the server stored every turn; re-render from it so tool lines appear in order
      await loadMessages();
      if (!res.text) appendBubble('assistant', '(no reply)');
      handleEvents(res.events);
    } catch (e) {
      pending.remove(); appendBubble('error', e.message);
    } finally { btn.disabled = false; input.focus(); }
  });

  el('chat-clear').addEventListener('click', async () => {
    try { await api('/api/messages/clear', { method: 'POST' }); } catch (e) { appendBubble('error', e.message); return; }
    loadMessages();
  });

  // ---------- Busy indicator ----------
  // One running task at a time: a spinner, a label, and an elapsed-time counter, so a long
  // model call (reading PDFs can take a minute or more) never looks frozen.
  let modelLabel = '';
  async function loadModelLabel() {
    try {
      const s = await api('/api/settings');
      const p = s.provider || {};
      modelLabel = p.model ? `${p.model} via ${p.kind || 'provider'}` : '';
      if (!p.api_key) modelLabel = '';
    } catch (e) { modelLabel = ''; }
    return modelLabel;
  }
  function startBusy(box, label) {
    box.textContent = '';
    const row = node('div', 'note busy');
    row.appendChild(node('span', 'spinner', ''));
    const text = node('span', null, label);
    const elapsed = node('span', 'elapsed', '0s');
    row.appendChild(text); row.appendChild(elapsed); box.appendChild(row);
    const t0 = Date.now();
    const timer = setInterval(() => { elapsed.textContent = `${Math.round((Date.now() - t0) / 1000)}s`; }, 1000);
    return { stop: () => { clearInterval(timer); if (row.parentNode) row.remove(); }, seconds: () => Math.round((Date.now() - t0) / 1000) };
  }

  // ---------- Notes ----------
  function showNotes(notes, warnings, extra) {
    const box = el('notes'); box.textContent = '';
    (extra || []).forEach((x) => box.appendChild(node('div', 'note ' + (x.cls || ''), x.text)));
    (warnings || []).forEach((w) => box.appendChild(node('div', 'note warn', String(w))));
    (notes || []).forEach((n) => {
      const d = node('div', 'note');
      if (n && typeof n === 'object') {
        d.appendChild(node('b', null, [n.section, n.source].filter(Boolean).join(' \u00b7 ')));
        d.appendChild(document.createTextNode(n.note || ''));
      } else d.textContent = String(n);
      box.appendChild(d);
    });
  }
  window.showNotes = showNotes;

  // ---------- Upload ----------
  const drop = el('drop'), fileInput = el('files');
  async function upload(files) {
    if (!files || !files.length) return;
    const fd = new FormData();
    Array.from(files).forEach((f) => fd.append('files', f, f.name));
    fd.append('mode', el('import-mode').value);
    if (el('classes-as-planes').checked) fd.append('classes_as_planes', '1');
    drop.classList.add('busy'); fileInput.disabled = true;
    const label = await loadModelLabel();
    const names = Array.from(files).map((f) => f.name).join(', ');
    const busy = startBusy(el('notes'), label
      ? `Reading ${names} with ${label}. Large PDFs can take a minute or two\u2026`
      : `Reading ${names}\u2026`);
    try {
      const r = await fetch('/api/upload', { method: 'POST', body: fd });
      const body = await r.json().catch(() => ({}));
      const took = busy.seconds(); busy.stop();
      if (!r.ok) { showNotes([], [], [{ cls: 'error', text: body.detail || r.statusText }]); return; }
      if (!body.draft) {
        // only a plan workbook (or workbooks) was uploaded: nothing to turn into a draft
        showNotes(body.notes, body.warnings, []);
        handleEvents(body.events);
        return;
      }
      const s = body.draft || {};
      const how = body.source === 'asc' ? 'read from the timetable export (no model needed)' : `extracted${label ? ' with ' + label : ''}`;
      showNotes(body.notes, body.warnings, [{ cls: 'good', text: `Draft ${how} in ${took}s: ${s.persons} persons, ${s.locations} locations, ${s.events} events. Check the tables, then Build.` }]);
      await loadDraft();
      handleEvents(body.events);
    } catch (e) { busy.stop(); showNotes([], [], [{ cls: 'error', text: e.message }]); }
    finally { drop.classList.remove('busy'); fileInput.disabled = false; fileInput.value = ''; }
  }
  fileInput.addEventListener('change', () => upload(fileInput.files));
  ['dragenter', 'dragover'].forEach((t) => drop.addEventListener(t, (ev) => { ev.preventDefault(); drop.classList.add('over'); }));
  ['dragleave', 'dragend'].forEach((t) => drop.addEventListener(t, () => drop.classList.remove('over')));
  drop.addEventListener('drop', (ev) => { ev.preventDefault(); drop.classList.remove('over'); upload(ev.dataTransfer && ev.dataTransfer.files); });
  // The old empty-draft button is gone; that path is still reachable through the chat's
  // new_draft tool ("start an empty draft"). Start wizard just asks the assistant to begin.
  el('start-wizard').addEventListener('click', () => { window.sendChat("I'd like to set up a new timetable"); });

  window.reloadIntake = async () => { el('notes').textContent = ''; stopFollowing(); await loadMessages(); await loadDraft(); await resumeSolve(); };

  el('clear-uploads').addEventListener('click', async () => {
    const btn = el('clear-uploads'); btn.disabled = true;
    try {
      await api('/api/uploads/clear', { method: 'POST' });
      const box = el('draft'); box.hidden = true; box.textContent = ''; draft = null;
      showNotes([], [], [{ cls: '', text: 'Uploads cleared. The next files you drop start a fresh draft.' }]);
    } catch (e) { showNotes([], [], [{ cls: 'error', text: e.message }]); }
    finally { btn.disabled = false; }
  });

  // ---------- Draft tables ----------
  const SECTIONS = [
    { key: 'persons', title: 'Persons', fields: ['name', 'role', 'avail', 'eligible'] },
    { key: 'locations', title: 'Locations', fields: ['name', 'cap', 'kind', 'shared', 'rest'] },
    { key: 'events', title: 'Events', fields: ['name', 'members', 'dur', 'eligible_locs', 'fixed'] },
    // groups come from the import (or the chat) and are read here, not edited cell by cell
    { key: 'groups', title: 'Groups', fields: ['name', 'classes', 'band', 'option'], readOnly: true, optional: true },
  ];
  const LIST_FIELDS = new Set(['eligible', 'members', 'eligible_locs']);
  const INT_FIELDS = new Set(['cap', 'dur']);
  const BOOL_FIELDS = new Set(['shared', 'rest', 'fixed']);

  // Accept the old flat [a, b] form as well as the current list-of-windows form,
  // the way model.js normalises it (Array.isArray(p.avail[0]) ? p.avail : [p.avail]).
  // A stored draft can still hold the flat form, so display code must handle both.
  function normaliseAvail(v) {
    if (!Array.isArray(v) || !v.length) return null;
    return Array.isArray(v[0]) ? v : [v];
  }
  function show(field, v) {
    if (BOOL_FIELDS.has(field)) return v ? 'true' : 'false';
    if (v == null) return '';
    if (field === 'avail') {
      const windows = normaliseAvail(v);
      if (windows) return windows.map((w) => w.join('-')).join('; ');
    }
    if (Array.isArray(v)) return v.join(', ');
    return String(v);
  }
  // The avail cell's title spells out each window's first and last slot by their time_labels,
  // e.g. "Odd Mon 7:35–Odd Mon 12:00; Even Tue 7:35–Even Tue 10:00" (end exclusive, so the last slot is b - 1).
  function availTitle(v) {
    const labels = (draft && draft.time_labels) || [];
    const windows = normaliseAvail(v);
    if (!windows) return '';
    return windows.map(([a, b]) => `${labels[a] != null ? labels[a] : a}–${labels[b - 1] != null ? labels[b - 1] : b - 1}`).join('; ');
  }
  function parse(field, text) {
    const s = text.trim();
    if (field === 'avail') {
      const windows = s.split(';').map((part) => {
        const nums = part.trim().split(/[\s,\u2013-]+/).filter(Boolean).map(Number);
        if (nums.length !== 2 || nums.some((n) => !Number.isInteger(n))) {
          throw new Error('avail must be "0-8" or windows "0-3; 5-8"');
        }
        return nums;
      });
      return windows;
    }
    if (INT_FIELDS.has(field)) {
      const n = Number(s);
      if (!Number.isInteger(n)) throw new Error(field + ' must be an integer');
      return n;
    }
    if (LIST_FIELDS.has(field)) {
      if (field === 'eligible_locs' && s === '') return null;
      return s.split(',').map((x) => x.trim()).filter(Boolean);
    }
    if (BOOL_FIELDS.has(field)) return /^(true|yes|y|1|x)$/i.test(s);
    return s;
  }

  let draft = null;
  async function loadDraft() {
    const box = el('draft');
    let d;
    try { d = await api('/api/draft'); }
    catch (e) { draft = null; box.hidden = true; box.textContent = ''; await showSolveBar(false); await loadBookings(); return; }
    draft = d;
    box.textContent = '';
    const head = node('div', 'head');
    head.appendChild(node('div', 'section-title', 'Draft timetable' + (d.name ? ': ' + d.name : '')));
    const placed = (d.events || []).filter((e) => e.loc != null && e.t0 != null).length;
    const summary = node('div', 'summary');
    summary.innerHTML = `${String((d.persons || []).length)} <span data-word="person" data-word-plural>teachers</span> \u00b7 ${String((d.locations || []).length)} <span data-word="venue" data-word-plural>rooms</span> \u00b7 ${String((d.events || []).length)} <span data-word="requirement" data-word-plural>lessons</span> (${String(placed)} fixed) \u00b7 ${String((d.time_labels || []).length)} time slots`;
    window.Words.apply(summary);
    head.appendChild(summary);
    const groups = d.groups || [], bands = d.bands || [];
    if (groups.length) head.appendChild(node('div', 'summary', `${groups.filter((g) => !g.band).length} whole-class groups \u00b7 ${groups.filter((g) => g.band).length} option groups in ${bands.length} band${bands.length === 1 ? '' : 's'}`));
    box.appendChild(head);
    box.appendChild(node('div', 'help', 'Click a cell to edit; changes save when you leave the cell. Lists are comma-separated ids; avail is "0-8" or windows "0-3; 5-8" (slot numbers, end exclusive). Then Quick timetable or Best timetable above.'));
    SECTIONS.forEach((sec) => { const rows = d[sec.key] || []; if (!sec.optional || rows.length) box.appendChild(renderTable(sec, rows)); });
    box.hidden = false;
    await showSolveBar(true);
    await loadBookings();
  }
  window.loadDraft = loadDraft;

  // ---------- Bookings ----------
  // Dated venue bookings, fixed and memberless, injected into every engine call. Rendered below the
  // draft tables; a booking belongs to the live timetable, so this renders whether or not a draft exists.
  async function loadBookings() {
    const box = el('bookings');
    let data;
    try { data = await api('/api/bookings'); }
    catch (e) { box.hidden = true; box.textContent = ''; return; }
    const items = data.items || [];
    box.textContent = '';
    if (!items.length) { box.hidden = true; return; }
    box.appendChild(node('h3', null, `Bookings (${items.length})`));
    const tw = node('div', 'tablewrap'), table = node('table', 'grid');
    const thead = node('thead'), htr = node('tr');
    ['date', 'venue', 'slots', 'title', 'booked by', ''].forEach((h) => htr.appendChild(node('th', null, h)));
    thead.appendChild(htr); table.appendChild(thead);
    const tbody = node('tbody');
    items.forEach((b) => {
      const tr = node('tr');
      tr.appendChild(node('td', null, (data.describe || {})[b.id] || b.date));
      tr.appendChild(node('td', null, b.venue));
      tr.appendChild(node('td', null, `${b.start}\u2013${b.start + b.dur - 1}`));
      tr.appendChild(node('td', null, b.title));
      tr.appendChild(node('td', null, b.booked_by || ''));
      const del = node('button', 'btn', 'Delete');
      del.type = 'button';
      del.addEventListener('click', async () => {
        del.disabled = true;
        try { await api(`/api/bookings/${b.id}`, { method: 'DELETE' }); await loadBookings(); }
        catch (e) { showNotes([], [], [{ cls: 'error', text: e.message }]); del.disabled = false; }
      });
      const tdBtn = node('td'); tdBtn.appendChild(del);
      tr.appendChild(tdBtn);
      tbody.appendChild(tr);
    });
    table.appendChild(tbody); tw.appendChild(table); box.appendChild(tw);
    box.hidden = false;
  }
  window.loadBookings = loadBookings;

  async function showSolveBar(hasDraft) {
    let hasLive = false;
    try { const t = await api('/api/timetables'); hasLive = !!(t.items.find((x) => x.id === t.current) || {}).has_live; } catch (e) { /* bar stays as it is */ }
    el('solve-bar').hidden = !hasDraft && !hasLive;
    ['build', 'solve', 'solve-preset', 'solve-time'].forEach((id) => { el(id).disabled = !hasDraft; });
    el('score-live').disabled = !hasLive;
  }

  function renderTable(sec, rows) {
    const wrap = node('div');
    wrap.appendChild(node('h3', null, `${sec.title} (${rows.length})`));
    const tw = node('div', 'tablewrap'), table = node('table', 'grid');
    const thead = node('thead'), tr = node('tr');
    ['id'].concat(sec.fields).forEach((f) => tr.appendChild(node('th', null, f)));
    thead.appendChild(tr); table.appendChild(thead);
    const tbody = node('tbody');
    rows.forEach((row) => {
      const r = node('tr');
      r.appendChild(node('td', 'id', row.id));
      sec.fields.forEach((f) => {
        const td = node('td', null, show(f, row[f]));
        if (f === 'avail') td.title = availTitle(row[f]);
        if (sec.readOnly) { r.appendChild(td); return; }
        td.contentEditable = 'true'; td.spellcheck = false;
        td.dataset.section = sec.key; td.dataset.id = row.id; td.dataset.field = f; td.dataset.orig = td.textContent;
        td.addEventListener('keydown', (ev) => { if (ev.key === 'Enter') { ev.preventDefault(); td.blur(); } if (ev.key === 'Escape') { td.textContent = td.dataset.orig; td.blur(); } });
        td.addEventListener('blur', () => commitCell(td));
        r.appendChild(td);
      });
      tbody.appendChild(r);
    });
    table.appendChild(tbody); tw.appendChild(table); wrap.appendChild(tw);
    return wrap;
  }

  async function commitCell(td) {
    const text = td.textContent;
    if (text === td.dataset.orig) return;
    td.classList.remove('saved', 'failed'); td.title = '';
    let value;
    try { value = parse(td.dataset.field, text); }
    catch (e) { td.classList.add('failed'); td.title = e.message; showNotes([], [], [{ cls: 'error', text: e.message }]); return; }
    const patch = { [td.dataset.section]: { [td.dataset.id]: { [td.dataset.field]: value } } };
    try {
      await api('/api/draft/patch', { method: 'POST', body: JSON.stringify({ patch }) });
      td.dataset.orig = text; td.classList.add('saved');
      setTimeout(() => td.classList.remove('saved'), 1200);
    } catch (e) {
      td.classList.add('failed'); td.title = e.message;   // the reason stays on the cell after the notes strip moves on
      showNotes([], [], [{ cls: 'error', text: `${td.dataset.id}.${td.dataset.field}: ${e.message}` }]);
    }
  }

  el('build').addEventListener('click', () => runBuild(el('build')));

  async function runBuild(btn) {
    if (btn) btn.disabled = true;
    const busy = startBusy(el('notes'), 'Building on the engine (no model involved; this takes well under a second)\u2026');
    try {
      const res = await api('/api/build', { method: 'POST' });
      busy.stop();
      handleEvents([{ kind: 'build', ok: !!res.ok, placed: res.placed, unplaced: res.unplaced, clashes: res.clashes }]);
      if (!res.ok) loadDraft();
    } catch (e) { busy.stop(); showNotes([], [], [{ cls: 'error', text: e.message }]); }
    finally { if (btn) btn.disabled = false; }
  }

  // ---------- Solve: presets, live progress, quality ----------
  // POST /api/solve queues a job on the engine; GET /api/solve is polled every 2 s and, once the job
  // has finished, the server promotes the result (same rule as Build) and reports `promoted`. The
  // progress card is the busy indicator for a solve: it names the preset, the elapsed time and the
  // best objective so far. The quality panel compares the live timetable's score taken before the
  // solve (`before`, recorded by the server when the job starts) with the solved one (`result.scores`).
  const PRESET_LABELS = { close: 'keep it close', balanced: 'balanced', quality: 'best quality', custom: 'custom weights' };
  const RULE_LABELS = {
    spread: ['spread', 'a group meets a subject once a day'], stability: ['stability', 'lessons stay where they were'],
    compact: ['compact', 'no gaps in a teacher\u2019s day'], even_days: ['even days', 'a group\u2019s load is level across days'],
    edge: ['edge', 'heavy subjects away from first and last slots'], venue: ['venue', 'lessons in their usual room'],
  };
  let solvePoll = null;      // the setTimeout handle while a solve is being followed
  let solveGen = 0;          // a timetable switch or a new solve makes older polls fall silent
  let solveTick = null;      // a 1 s timer that keeps the elapsed count moving between polls
  let solveStartedAt = 0;    // Date.now() minus the server's wall time since the job was queued
  let solveState = null;     // the last polled state, read by the tick

  function stopFollowing() {
    if (solvePoll) clearTimeout(solvePoll);
    if (solveTick) clearInterval(solveTick);
    solvePoll = solveTick = null; solveGen++;
  }
  const fmtSec = (s) => (s < 10 ? (Math.round(s * 10) / 10).toFixed(1) : String(Math.round(s))) + 's';

  // The label counts wall time since the job was queued (the engine's own `elapsed` starts only once
  // the model is built, which can take a minute on a whole school and would read as frozen).
  function renderProgress(st) {
    const card = el('solve-progress');
    const limit = st.time_limit || 1;
    solveState = st;
    solveStartedAt = Date.now() - Math.round((st.running_for || 0) * 1000);
    paintSolveLabel();
    if (!solveTick) solveTick = setInterval(paintSolveLabel, 1000);
    const meta = el('solve-meta'); meta.textContent = '';
    meta.appendChild(node('span', null, `status ${st.status}`));
    meta.appendChild(node('span', null, `search ${fmtSec(st.elapsed || 0)} of ${limit}s`));
    if (st.bound != null && st.bound > 0) {
      meta.appendChild(node('span', null, `bound ${st.bound}`));
      if (st.best_objective != null) meta.appendChild(node('span', null, `gap ${gapPct(st.best_objective, st.bound)}`));
    }
    el('solve-cancel').disabled = false;
    card.hidden = false;
  }
  function paintSolveLabel() {
    const st = solveState; if (!st) return;
    const wall = Math.max(0, (Date.now() - solveStartedAt) / 1000);
    const best = st.best_objective != null ? `best ${st.best_objective}`
      : st.status === 'queued' ? 'queued' : wall > 5 ? 'building the model' : 'no solution yet';
    el('solve-label').textContent = `Finding the best timetable \u00b7 ${PRESET_LABELS[st.preset] || st.preset} \u00b7 ${Math.round(wall)}s \u00b7 ${best}`;
    el('solve-fill').style.width = `${Math.min(100, ((st.elapsed || 0) / (st.time_limit || 1)) * 100)}%`;
  }
  const gapPct = (obj, bound) => `${(Math.max(0, obj - bound) / Math.max(obj, 1) * 100).toFixed(1)}%`;

  function renderQuality(st) {
    const box = el('quality'); box.textContent = '';
    const res = st.result, after = res && res.scores, before = st.before && st.before.scores;
    if (!after && !before) { box.hidden = true; return; }
    const head = node('div', 'head');
    head.appendChild(node('b', null, 'Quality'));
    head.appendChild(node('span', 'meta', after
      ? `${PRESET_LABELS[st.preset] || st.preset} \u00b7 ${fmtSec(st.elapsed || 0)} (limit ${st.time_limit}s) \u00b7 objective ${res.objective}` + (res.bound > 0 ? ` \u00b7 bound ${res.bound} \u00b7 gap ${gapPct(res.objective, res.bound)}` : '')
      : `live timetable under the current weights \u00b7 objective ${st.before.total}`));
    if (after) {
      const chip = node('span', 'chip ' + (res.optimal ? 'ok' : ''), res.optimal ? 'proven optimal' : st.status === 'cancelled' ? 'stopped early' : 'time limit reached');
      head.appendChild(chip);
    }
    box.appendChild(head);
    const table = node('table'), thead = node('thead'), tr = node('tr');
    ['rule', before ? 'before' : '', after ? 'after' : '', before && after ? 'change' : ''].filter((h) => h !== '').forEach((h) => tr.appendChild(node('th', null, h)));
    thead.appendChild(tr); table.appendChild(thead);
    const tbody = node('tbody');
    const rules = Object.keys(RULE_LABELS);
    rules.concat(['total']).forEach((r) => {
      const row = node('tr');
      const name = node('td', 'rule', r === 'total' ? 'total' : RULE_LABELS[r][0]);
      if (r !== 'total') name.appendChild(node('small', null, RULE_LABELS[r][1]));
      row.appendChild(name);
      const b = before ? (r === 'total' ? st.before.total : before[r]) : null;
      const a = after ? (r === 'total' ? res.objective : after[r]) : null;
      if (before) row.appendChild(node('td', 'n', b == null ? '' : String(b)));
      if (after) row.appendChild(node('td', 'n', a == null ? '' : String(a)));
      if (before && after) {
        const d = (a == null || b == null) ? null : a - b;
        const better = r === 'total' ? d < 0 : d > 0;      // scores rise towards 100; the total (the objective) falls
        row.appendChild(node('td', 'n ' + (d === 0 || d == null ? '' : better ? 'up' : 'down'), d == null ? '' : (d > 0 ? '+' : '') + d));
      }
      tbody.appendChild(row);
    });
    table.appendChild(tbody); box.appendChild(table);
    box.appendChild(node('div', 'meta', 'Scores are 0 to 100 per rule (100 is the ideal); the total is the weighted objective, lower is better. "Before" is the live timetable before this solve.'));
    box.hidden = false;
  }

  function finishSolve(st) {
    stopFollowing();
    el('solve-progress').hidden = true;
    el('solve').disabled = false;
    const res = st.result || {};
    if (st.status === 'failed') showNotes([], [], [{ cls: 'error', text: `Solve failed: ${st.error || 'unknown error'}` }]);
    else if (st.promoted) {
      showNotes([], [], [{ cls: 'good', text: `${st.status === 'cancelled' ? 'Stopped early' : 'Solved'} (${PRESET_LABELS[st.preset] || st.preset}, ${fmtSec(st.elapsed || 0)}): ${(res.placed || []).length} events placed, objective ${res.objective}${res.optimal ? ', proven optimal' : ''}. The draft is now the live timetable.` }]);
      if (window.reloadModel) window.reloadModel().catch(() => {});
      loadDraft();
    } else if (res.unplaced || res.clashes) {
      showNotes([], [], [{ cls: 'error', text: `Solve did not settle: ${(res.unplaced || []).length} unplaced, ${(res.clashes || []).length} clashes. The draft is unchanged.` }]
        .concat((res.clashes || []).slice(0, 20).map((c) => ({ cls: 'error', text: c.message || String(c) }))));
      loadDraft();
    } else showNotes([], [], [{ cls: 'warn', text: `Solve ${st.status} without a solution.` }]);
    renderQuality(st);
  }

  async function pollSolve(gen) {
    if (gen !== solveGen) return;
    let st;
    try { st = await api('/api/solve'); }
    catch (e) {
      if (gen !== solveGen) return;
      el('solve-meta').textContent = `waiting: ${e.message}`;
      solvePoll = setTimeout(() => pollSolve(gen), 4000); return;
    }
    if (gen !== solveGen) return;
    if (st.status === 'queued' || st.status === 'running') {
      renderProgress(st);
      solvePoll = setTimeout(() => pollSolve(gen), 2000);
    } else finishSolve(st);
  }

  async function startSolve() {
    const btn = el('solve'); btn.disabled = true;
    const preset = el('solve-preset').value, time_limit = Number(el('solve-time').value);
    if (!Number.isInteger(time_limit) || time_limit < 10 || time_limit > 900) { btn.disabled = false; showNotes([], [], [{ cls: 'error', text: 'The time limit must be a whole number of seconds between 10 and 900.' }]); return; }
    el('notes').textContent = ''; el('quality').hidden = true;
    stopFollowing();
    try {
      await api('/api/solve', { method: 'POST', body: JSON.stringify({ preset, time_limit }) });
      // remember the choice for next time (the preset select and time limit mirror the settings page)
      api('/api/settings').then((s) => api('/api/settings', { method: 'PUT', body: JSON.stringify({ solve: { ...s.solve, preset, time_limit } }) })).catch(() => {});
    } catch (e) { btn.disabled = false; showNotes([], [], [{ cls: 'error', text: e.message }]); return; }
    renderProgress({ preset, time_limit, status: 'queued', elapsed: 0 });
    pollSolve(solveGen);
  }
  el('solve').addEventListener('click', startSolve);
  el('solve-cancel').addEventListener('click', async () => {
    el('solve-cancel').disabled = true;
    try { await api('/api/solve/cancel', { method: 'POST' }); el('solve-meta').textContent = 'stopping at the next improving solution\u2026'; }
    catch (e) { showNotes([], [], [{ cls: 'error', text: e.message }]); el('solve-cancel').disabled = false; }
  });

  // On load (and after a timetable switch): follow a solve that is still running, or show the last one's quality.
  async function resumeSolve() {
    stopFollowing();
    el('solve-progress').hidden = true; el('quality').hidden = true;
    try {
      const s = await api('/api/settings');
      el('solve-preset').value = s.solve.preset; el('solve-time').value = s.solve.time_limit;
    } catch (e) { /* the defaults in the markup stay */ }
    let st;
    try { st = await api('/api/solve'); } catch (e) { return; }
    if (st.status === 'queued' || st.status === 'running') { el('solve').disabled = true; renderProgress(st); pollSolve(solveGen); }
    else if (st.promoted || (st.result && st.result.scores)) renderQuality(st);
  }
  // The live timetable's quality on demand, without solving: one engine check.
  el('score-live').addEventListener('click', async () => {
    const btn = el('score-live'); btn.disabled = true;
    try { renderQuality({ before: await api('/api/score'), preset: 'live', status: 'live' }); }
    catch (e) { showNotes([], [], [{ cls: 'error', text: e.message }]); }
    finally { btn.disabled = false; }
  });

  loadModelLabel();
  loadMessages();
  loadDraft().then(resumeSolve);
})();
