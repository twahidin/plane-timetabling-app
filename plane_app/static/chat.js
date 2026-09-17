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

  function handleEvents(events) {
    let reload = false;
    (events || []).forEach((ev) => {
      if (ev.kind === 'build') {
        if (ev.ok) { reload = true; showNotes([], [], [{ cls: 'good', text: `Built: ${(ev.placed || []).length} events placed. The draft is now the live timetable.` }]); }
        else showNotes([], [], [{ cls: 'error', text: `Build did not settle: ${(ev.unplaced || []).length} unplaced, ${(ev.clashes || []).length} clashes.` }]
          .concat((ev.clashes || []).map((c) => ({ cls: 'error', text: c.message || String(c) }))));
      } else if (ev.kind === 'draft_updated') reload = true;
    });
    if (reload) { if (window.reloadModel) window.reloadModel().catch(() => {}); loadDraft(); }
  }

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
      ? `Reading ${names} and extracting the organisation with ${label}. Large PDFs can take a minute or two\u2026`
      : `Reading ${names}\u2026`);
    try {
      const r = await fetch('/api/upload', { method: 'POST', body: fd });
      const body = await r.json().catch(() => ({}));
      const took = busy.seconds(); busy.stop();
      if (!r.ok) { showNotes([], [], [{ cls: 'error', text: body.detail || r.statusText }]); return; }
      const s = body.draft || {};
      const how = body.source === 'asc' ? 'read from the timetable export (no model needed)' : `extracted${label ? ' with ' + label : ''}`;
      showNotes(body.notes, body.warnings, [{ cls: 'good', text: `Draft ${how} in ${took}s: ${s.persons} persons, ${s.locations} locations, ${s.events} events. Check the tables, then Build.` }]);
      await loadDraft();
    } catch (e) { busy.stop(); showNotes([], [], [{ cls: 'error', text: e.message }]); }
    finally { drop.classList.remove('busy'); fileInput.disabled = false; fileInput.value = ''; }
  }
  fileInput.addEventListener('change', () => upload(fileInput.files));
  ['dragenter', 'dragover'].forEach((t) => drop.addEventListener(t, (ev) => { ev.preventDefault(); drop.classList.add('over'); }));
  ['dragleave', 'dragend'].forEach((t) => drop.addEventListener(t, () => drop.classList.remove('over')));
  drop.addEventListener('drop', (ev) => { ev.preventDefault(); drop.classList.remove('over'); upload(ev.dataTransfer && ev.dataTransfer.files); });
  el('start-criteria').addEventListener('click', async () => {
    const btn = el('start-criteria'); btn.disabled = true;
    try {
      const s = await api('/api/draft/new', { method: 'POST' });
      showNotes([], [], [{ cls: 'good', text: `Empty draft started (${s.locations} location: rest). Describe the organisation in the chat: people and their hours, venues and capacities, the lessons or shifts and who attends, and any rules. The assistant fills the tables; check them, then Build.` }]);
      await loadDraft();
      const input = el('chat-text'); input.placeholder = 'Describe the people, venues, lessons and rules…'; input.focus();
    } catch (e) { showNotes([], [], [{ cls: 'error', text: e.message }]); }
    finally { btn.disabled = false; }
  });

  window.reloadIntake = async () => { el('notes').textContent = ''; await loadMessages(); await loadDraft(); };

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
    { key: 'locations', title: 'Locations', fields: ['name', 'cap', 'shared', 'rest'] },
    { key: 'events', title: 'Events', fields: ['name', 'members', 'dur', 'eligible_locs', 'fixed'] },
  ];
  const LIST_FIELDS = new Set(['eligible', 'members', 'eligible_locs']);
  const INT_FIELDS = new Set(['cap', 'dur']);
  const BOOL_FIELDS = new Set(['shared', 'rest', 'fixed']);

  function show(field, v) {
    if (BOOL_FIELDS.has(field)) return v ? 'true' : 'false';
    if (v == null) return '';
    if (Array.isArray(v)) return v.join(', ');
    return String(v);
  }
  function parse(field, text) {
    const s = text.trim();
    if (field === 'avail') {
      const parts = s.split(/[\s,\u2013-]+/).filter(Boolean).map(Number);
      if (parts.length !== 2 || parts.some((n) => !Number.isInteger(n))) throw new Error('avail must be two integers, e.g. 0, 8');
      return parts;
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
    catch (e) { draft = null; box.hidden = true; box.textContent = ''; return; }
    draft = d;
    box.textContent = '';
    const head = node('div', 'head');
    head.appendChild(node('div', 'section-title', 'Draft organisation' + (d.name ? ': ' + d.name : '')));
    const placed = (d.events || []).filter((e) => e.loc != null && e.t0 != null).length;
    head.appendChild(node('div', 'summary', `${(d.persons || []).length} persons \u00b7 ${(d.locations || []).length} locations \u00b7 ${(d.events || []).length} events (${placed} fixed) \u00b7 ${(d.time_labels || []).length} slots`));
    const buildBtn = node('button', 'btn primary', 'Build'); buildBtn.type = 'button'; buildBtn.id = 'build';
    buildBtn.addEventListener('click', () => runBuild(buildBtn));
    head.appendChild(buildBtn);
    box.appendChild(head);
    box.appendChild(node('div', 'help', 'Click a cell to edit; changes save when you leave the cell. Lists are comma-separated ids; avail is "first, one past last".'));
    SECTIONS.forEach((sec) => box.appendChild(renderTable(sec, d[sec.key] || [])));
    box.hidden = false;
  }
  window.loadDraft = loadDraft;

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

  loadModelLabel();
  loadMessages();
  loadDraft();
})();
