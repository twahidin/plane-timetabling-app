// The Assistant takes files (spec docs/superpowers/specs/2026-10-12-assistant-files-design.md §1). Every
// way in (the 📎 button, a drop on the panel, the Intake drop, the Requirements tab's upload) hands its files
// to give(): a row of chips in the chat while they are read, then one review card per reading. Nothing read
// from a file changes the plan or the draft until a card's Apply ticked (or a confirming reply in the chat).
// Every name, sample and doubt is data from a file: built with createElement and textContent only.
(function () {
  'use strict';

  const el = (id) => document.getElementById(id);
  const node = (tag, cls, text) => {
    const n = document.createElement(tag);
    if (cls) n.className = cls;
    if (text != null) n.textContent = text;
    return n;
  };
  const post = async (path, body) => {
    const r = await fetch(path, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body || {}) });
    const data = await r.json().catch(() => ({}));
    if (!r.ok) throw new Error(data.detail || r.statusText);
    return data;
  };
  const slug = (id) => encodeURIComponent(id);

  const MAX_FILES = 10;
  const SHOWN = new Set(['waiting', 'failed', 'retry']);   // and an applied reading that can still be undone
  const VIA = { exact: 'exact', saved: 'saved reader', ai: 'read by AI' };
  const TITLES = { staff: 'Staff', classes: 'Classes', rooms: 'Rooms', requirements: 'Requirements', rules: 'Rules',
    notes: 'Notes', timetable: 'Timetable', students: 'Students', combinations: 'Combinations',
    venues: 'Rooms', subjects: 'Subjects by level' };     // venues and subjects: staff.csv's siblings (venues.csv, requirements.csv)
  const NOUNS = { timetable: 'lessons' };
  const MODES = [['keep', 'Keep the existing placement, then check it'], ['rebuild', 'Start again: place everything afresh']];

  const log = el('chat-log');
  const scrollLog = () => { log.scrollTop = log.scrollHeight; };
  const showAssistant = () => {
    if (typeof window.showAssistant === 'function') window.showAssistant();
    else { const rail = el('assistant-rail'); if (rail) rail.click(); }
  };
  function chatLine(cls, text) { log.appendChild(node('div', 'msg ' + cls, text)); scrollLog(); }

  // ---------- chips: one per file while it is read ----------
  function badge(name) {
    const ext = (/\.([a-z0-9]+)$/i.exec(name || '') || [])[1] || '';
    const e = ext.toLowerCase();
    const label = e.startsWith('xls') ? 'XLS' : e === 'csv' ? 'CSV' : e.startsWith('doc') ? 'DOC' : e === 'pdf' ? 'PDF' : 'FILE';
    return node('span', 'file-badge ' + label.toLowerCase(), label);
  }
  function chip(file) {
    const c = node('span', 'file-chip');
    c.appendChild(badge(file.name));
    c.appendChild(node('span', 'file-name', file.name));
    const st = node('span', 'file-state');
    st.appendChild(node('span', 'spinner', ''));
    st.appendChild(document.createTextNode('reading…'));
    c.appendChild(st);
    return c;
  }
  function settle(c, cls, text) {
    const st = c.querySelector('.file-state');
    st.className = 'file-state ' + cls;
    st.textContent = text;
  }

  async function give(fileList) {
    const files = Array.from(fileList || []);
    if (!files.length) return;
    showAssistant();
    if (files.length > MAX_FILES) { chatLine('error', 'Up to 10 files at a time.'); return; }
    const row = node('div', 'file-chips');
    const chips = files.map((f) => { const c = chip(f); row.appendChild(c); return c; });
    log.appendChild(row); scrollLog();
    const fd = new FormData();
    files.forEach((f) => fd.append('files', f, f.name));
    let body;
    try {
      const r = await fetch('/api/upload', { method: 'POST', body: fd });
      body = await r.json().catch(() => ({}));
      if (!r.ok && !Array.isArray(body.files)) throw new Error(body.detail || r.statusText);
    } catch (e) {
      chips.forEach((c) => settle(c, 'bad', '⚠ ' + e.message));
      return;
    }
    const results = body.files || [];
    chips.forEach((c, i) => {
      const f = results[i] || results.find((x) => x.name === files[i].name);
      if (!f) { settle(c, 'bad', '⚠ no reply for this file'); return; }
      if (f.status === 'read') settle(c, 'ok', '✓ ' + f.message);
      else settle(c, f.status === 'retry' ? 'warn' : 'bad', '⚠ ' + f.message);
    });
    scrollLog();
    const read = results.filter((f) => f.status === 'read');
    if (read.length) document.dispatchEvent(new CustomEvent('plane:journey', { detail: { stage: 'files', files: read.map((f) => ({ name: f.name, status: f.status })) } }));
    await load();
    document.dispatchEvent(new CustomEvent('plane:readings'));
  }

  // ---------- review cards ----------
  // A waiting card's ticks and options as the user left them, by reading id. A chat reply empties the log and
  // the cards are built again from the server's views: these keep the user's choices. A remembered tick counts
  // only while its section still exists (the Assistant may have edited the reading); untouched sections take
  // the server's tick. Forgotten once the reading is no longer waiting.
  // Every change is also saved on the reading (POST …/choices), so a yes in the chat applies what the card
  // shows. Once saved, a view fetched after the save is the truth again (a tick the Assistant set wins).
  const choices = {};
  let loads = 0;                          // each load() of the readings, counted
  const countsText = (counts) => Object.keys(counts).map((k) => `${counts[k]} ${NOUNS[k] || k}`).join(', ');
  let uid = 0;

  function head(view) {
    const h = node('div', 'rc-head');
    h.appendChild(badge(view.file));
    h.appendChild(node('span', 'rc-file', view.file));
    // how it was read; a file that could not be read was not read any way
    if (view.status !== 'failed' && view.status !== 'retry') h.appendChild(node('span', 'rc-via', VIA[view.via] || view.via || ''));
    return h;
  }

  function section(view, name, s, onTick, mine) {
    const sec = node('div', 'sec');
    const id = 'rc-sec-' + (++uid);
    const box = node('input');
    box.type = 'checkbox'; box.id = id; box.dataset.section = name;
    box.checked = name in mine.ticks ? mine.ticks[name] : !!s.ticked;
    box.addEventListener('change', () => { mine.ticks[name] = box.checked; onTick(); });
    sec.appendChild(box);
    const t = node('div', 't');
    const label = node('label');
    label.htmlFor = id;
    label.appendChild(node('b', null, TITLES[name] || name));
    let line = ` · ${s.count}`;
    if (name === 'timetable') line = ` · ${s.count} lessons` + (s.teachers ? `, ${s.teachers} teachers` : '');
    else if (name === 'students') {
      line = ` · ${s.count} student${s.count === 1 ? '' : 's'}` + (s.added && s.changed ? ` (${s.added} new, ${s.changed} changed)` : '');
      if (s.removed) line += `; replaces ${s.removed} stored student${s.removed === 1 ? '' : 's'} not in this file`;
    }
    else if (s.added && s.changed) line += ` (${s.added} new, ${s.changed} changed)`;
    else if (s.changed && !s.added) line += ' changed';
    label.appendChild(document.createTextNode(line));
    t.appendChild(label);
    const samples = s.samples || [];
    if (samples.length) {
      const ul = node('ul', 'ex');
      samples.forEach((x) => ul.appendChild(node('li', null, String(x))));
      t.appendChild(ul);
    }
    if (samples.length && s.count > samples.length) {
      const all = node('button', 'linkish', `Show all ${s.count}`);
      all.type = 'button';
      all.addEventListener('click', () => { if (window.sendChat) window.sendChat('Show me all the ' + name + ' in reading ' + view.id); });   // the id, never the file's name: a name is data, not words to send
      t.appendChild(all);
    }
    sec.appendChild(t);
    return sec;
  }

  // What adopting a week costs (POST /api/week/impact), in words, said before Apply.
  function impactText(imp) {
    const parts = [];
    if (imp.lessons > 0) {
      const where = [...new Set(imp.where || [])].join(', ');
      parts.push(`${imp.lessons} ${imp.lessons === 1 ? 'lesson sits' : 'lessons sit'} in ${where}${imp.lessons > (imp.where || []).length ? ' and other periods' : ''}, where the new week has no period. They will show as clashes until you rebuild or move them.`);
    }
    const other = [imp.bookings > 0 ? `${imp.bookings} room ${imp.bookings === 1 ? 'booking' : 'bookings'}` : '', imp.pins > 0 ? `${imp.pins} pinned ${imp.pins === 1 ? 'lesson' : 'lessons'}` : ''].filter(Boolean);
    if (other.length) parts.push(`It also affects ${other.join(' and ')}.`);
    return 'Adopting changes your timetable\u2019s week. ' + parts.join(' ') + ' Undo history from before this change will be cleared.';
  }

  // What a saved week moved or could not keep (`week_changes` of an apply), in words.
  function weekChangesText(w) {
    if (window.WeekEditor && typeof window.WeekEditor.changesText === 'function') return window.WeekEditor.changesText(w);
    if (!w) return '';
    const out = [];
    if ((w.unplaced || []).length) out.push(`${w.unplaced.length} lesson(s) could not stay on their day and are unplaced.`);
    if ((w.bookings || []).length) out.push(`${w.bookings.length} room booking(s) no longer fit and were removed.`);
    if ((w.pins || []).length) out.push(`${w.pins.length} pin(s) could not be kept.`);
    if ((w.avail || []).length) out.push(`${w.avail.length} person(s)' availability no longer fits the week; check it.`);
    return out.join(' ');
  }

  function timetableOptions(view, mine) {
    const opts = mine.options || view.options || {};
    const box = node('div', 'rc-options');
    const mode = node('select');
    mode.setAttribute('aria-label', 'If the file is a timetable export');
    MODES.forEach(([v, text]) => { const o = node('option', null, text); o.value = v; mode.appendChild(o); });
    mode.value = opts.mode === 'rebuild' ? 'rebuild' : 'keep';
    box.appendChild(mode);
    const check = node('label', 'check');
    const cap = node('input');
    cap.type = 'checkbox'; cap.checked = !!opts.classes_as_planes;
    check.appendChild(cap);
    check.appendChild(document.createTextNode(' Also check each class for clashes'));
    box.appendChild(check);
    // an export whose week differs from the timetable's: both weeks side by side, and Keep (the default) or Adopt
    const radios = [];
    let impact = null;
    if (view.week) {
      const weeks = node('div', 'rc-week');
      [['This file\u2019s week', view.week.file], ['Your timetable\u2019s week', view.week.mine]].forEach(([title, lines]) => {
        const col = node('div', 'rc-week-col');
        col.appendChild(node('div', 'rc-week-title', title + ':'));
        (lines || []).forEach((line) => col.appendChild(node('div', 'rc-week-line', String(line))));
        weeks.appendChild(col);
      });
      box.appendChild(weeks);
      impact = node('div', 'rc-week-impact');
      impact.hidden = true;
      [['keep', 'Keep my week'], ['adopt', 'Adopt the file\u2019s week']].forEach(([v, text]) => {
        const label = node('label', 'check');
        const r = node('input');
        r.type = 'radio'; r.name = 'week-' + view.id; r.value = v; r.checked = (opts.week === 'adopt' ? 'adopt' : 'keep') === v;
        label.appendChild(r);
        label.appendChild(document.createTextNode(' ' + text));
        box.appendChild(label);
        radios.push(r);
      });
      box.appendChild(impact);
    }
    // choosing Adopt asks what it would move, and says so on the card before Apply
    let asked = 0;
    const showImpact = async () => {
      if (!impact) return;
      const adopt = radios.some((r) => r.checked && r.value === 'adopt');
      const mark = ++asked;
      if (!adopt) { impact.hidden = true; impact.textContent = ''; return; }
      impact.hidden = false; impact.textContent = 'Checking what adopting would move\u2026';
      try {
        const imp = await post('/api/week/impact', { time: view.week.time });
        if (mark === asked) impact.textContent = impactText(imp);
      } catch (e) { if (mark === asked) impact.textContent = 'Could not check what adopting would move: ' + e.message; }
    };
    radios.forEach((r) => r.addEventListener('change', showImpact));
    showImpact();
    const read = () => {
      const o = { mode: mode.value, classes_as_planes: cap.checked };
      if (radios.length) o.week = radios.some((r) => r.checked && r.value === 'adopt') ? 'adopt' : 'keep';
      return o;
    };
    const onChange = () => {};
    const out = { box, read, onChange };
    [mode, cap, ...radios].forEach((x) => x.addEventListener('change', () => { mine.options = read(); out.onChange(); }));
    return out;
  }

  // After an apply or an undo: the plan (or the draft) moved, so everything that shows it reloads.
  function moved(view, res) {
    document.dispatchEvent(new CustomEvent('plane:plan'));
    if (typeof window.loadPlan === 'function') Promise.resolve(window.loadPlan()).catch(() => {});
    if (typeof window.loadBoard === 'function') Promise.resolve(window.loadBoard()).catch(() => {});
    if (typeof window.loadWizard === 'function') Promise.resolve(window.loadWizard()).catch(() => {});
    if (view.kind === 'timetable' && typeof window.loadDraft === 'function') Promise.resolve(window.loadDraft()).catch(() => {});
    // blocks_setup or the levels moved, not the plan: no plan status to show
    if (view.kind === 'combinations' || view.kind === 'requirements') return;
    if (view.kind === 'students' || view.kind === 'level_students') {   // the students moved, not the plan
      document.dispatchEvent(new CustomEvent('plane:students'));
      return;
    }
    planeFiles.showStatus(res.status, view.kind);
    loadReaders();                              // an apply of a workbook reading saves its layout as a reader
  }

  // The card's ticks and options kept on the reading; one save at a time per reading, in order.
  function saveChoices(view, card, mine, opts, say) {
    const sections = {};
    card.querySelectorAll('.sec input[data-section]').forEach((b) => { sections[b.dataset.section] = b.checked; });
    const options = opts ? opts.read() : null;
    mine.pending = (mine.pending || 0) + 1;
    mine.saving = (mine.saving || Promise.resolve()).then(async () => {
      try {
        const res = await post(`/api/readings/${slug(view.id)}/choices`, { sections, options });
        if (res.reading && card.isConnected) card.dataset.sig = JSON.stringify(res.reading);
        mine.savedAt = loads;
      } catch (e) {
        say('warn', 'Not saved: ' + e.message + '. A typed yes applies the last saved ticks; Apply ticked takes what is ticked here.');
      } finally { mine.pending -= 1; }
    });
  }

  function build(view, at) {
    const card = node('div', 'reading-card');
    card.dataset.id = view.id;
    card.dataset.sig = JSON.stringify(view);
    card.appendChild(head(view));
    if (view.summary && view.summary !== view.file) card.appendChild(node('p', 'rc-summary', view.summary));
    const note = node('p', 'rc-note');
    note.hidden = true;
    const say = (cls, text) => { note.className = 'rc-note ' + cls; note.textContent = text; note.hidden = false; };
    const acts = node('div', 'acts');
    const button = (text, cls) => { const b = node('button', 'btn' + (cls ? ' ' + cls : ''), text); b.type = 'button'; return b; };
    const busy = (on) => acts.querySelectorAll('button').forEach((b) => { b.disabled = on; });

    if (view.status !== 'waiting') delete choices[view.id];
    if (view.status === 'waiting') {
      const secs = view.sections || {};
      const names = Object.keys(secs);
      const mine = choices[view.id] || (choices[view.id] = { ticks: {}, options: null });
      // a view fetched after the last save finished holds the user's choices (and any the Assistant made)
      if (at != null && mine.savedAt != null && at > mine.savedAt && !mine.pending) { mine.ticks = {}; mine.options = null; }
      Object.keys(mine.ticks).forEach((k) => { if (!(k in secs)) delete mine.ticks[k]; });
      let applyBtn = null;
      let opts = null;
      const onTick = () => { if (applyBtn) applyBtn.disabled = !card.querySelector('.sec input:checked'); };
      const onChoice = () => { onTick(); saveChoices(view, card, mine, opts, say); };
      names.forEach((name) => card.appendChild(section(view, name, secs[name], onChoice, mine)));
      opts = view.kind === 'timetable' ? timetableOptions(view, mine) : null;
      if (opts) { opts.onChange = () => saveChoices(view, card, mine, opts, say); card.appendChild(opts.box); }
      (view.doubts || []).forEach((d) => card.appendChild(node('div', 'doubt', '⚠ ' + d)));
      applyBtn = button('Apply ticked', 'primary');
      const skipBtn = button('Skip file');
      applyBtn.addEventListener('click', async () => {
        const ticked = Array.from(card.querySelectorAll('.sec input:checked')).map((b) => b.dataset.section);
        busy(true);
        let res;
        try { res = await post(`/api/readings/${slug(view.id)}/apply`, { sections: ticked, options: opts ? opts.read() : null }); }
        catch (e) { busy(false); onTick(); say('bad', e.message); return; }
        const next = render(res.reading || { ...view, status: 'applied', undo: true, applied: res.applied });
        if (res.reader || res.note) {
          const extra = node('p', 'rc-note' + (res.note ? ' warn' : ''), res.note || `Saved as a reader: ${res.reader}`);
          next.insertBefore(extra, next.querySelector('.acts'));
        }
        if (res.week_changes) {
          const said = weekChangesText(res.week_changes);
          next.insertBefore(node('p', 'rc-note' + (said ? ' warn' : ''), "The file\u2019s week is now the timetable\u2019s." + (said ? ' ' + said : '')), next.querySelector('.acts'));
        }
        moved(view, res);
      });
      skipBtn.addEventListener('click', async () => {
        busy(true);
        try { const res = await post(`/api/readings/${slug(view.id)}/skip`); render(res.reading || { ...view, status: 'skipped' }); }
        catch (e) { busy(false); onTick(); say('bad', e.message); }
      });
      acts.appendChild(applyBtn); acts.appendChild(skipBtn);
      acts.appendChild(node('span', 'hint', 'or tell me what to change'));
      card.appendChild(acts);
      onTick();
    } else if (view.status === 'applied') {
      card.classList.add('done');
      const counts = view.applied || {};          // what was applied, as the server recorded it
      card.appendChild(node('div', 'applied', '✓ Applied: ' + (countsText(counts) || 'done')));
      if (view.undo) {
        const undoBtn = button('Undo');
        undoBtn.addEventListener('click', async () => {
          busy(true);
          let res;
          try { res = await post(`/api/readings/${slug(view.id)}/undo`); }
          catch (e) { busy(false); say('bad', e.message); return; }
          render(res.reading || { ...view, status: 'waiting', undo: false });
          moved(view, res);
        });
        acts.appendChild(undoBtn);
        card.appendChild(acts);
        if (view.week_adopted) card.appendChild(node('p', 'rc-note', 'Undo puts the draft back; the week stays. Change it in Settings.'));
      }
    } else if (view.status === 'skipped') {
      card.classList.add('done');
      card.appendChild(node('div', 'rc-skipped', 'Skipped: nothing from this file was used.'));
    } else {
      card.appendChild(node('div', 'rc-error', view.error || 'This file could not be read.'));
      if (view.status === 'retry') {
        const again = button('Try again', 'primary');
        again.addEventListener('click', async () => {
          busy(true); say('', 'Reading it again…');
          let res;
          try { res = await post(`/api/readings/${slug(view.id)}/retry`); }
          catch (e) { busy(false); say('bad', e.message); return; }
          if (res.status !== 'read') say(res.status === 'retry' ? 'warn' : 'bad', '⚠ ' + (res.message || 'Not read.'));
          await load();
          document.dispatchEvent(new CustomEvent('plane:readings'));
        });
        acts.appendChild(again);
        card.appendChild(acts);
      }
    }
    card.appendChild(note);
    return card;
  }

  const cardOf = (id) => Array.from(log.querySelectorAll('.reading-card[data-id]')).find((c) => c.dataset.id === id) || null;

  // A reading's card, built afresh: it replaces the card already in the chat, or goes at the end.
  function render(view, at) {
    const card = build(view, at);
    const old = cardOf(view.id);
    if (old) old.replaceWith(card);
    else { log.appendChild(card); scrollLog(); }
    return card;
  }

  // The readings of the timetable being worked on: those waiting (or failed, or to try again, or applied and
  // still undoable) get a card; a card already showing the same reading is left alone, so ticks survive.
  async function load() {
    const at = ++loads;
    let data;
    try {
      const r = await fetch('/api/readings');
      if (!r.ok) return;
      data = await r.json();
    } catch (e) { return; }
    const seen = new Set();
    (data.readings || []).forEach((view) => {
      seen.add(view.id);
      const old = cardOf(view.id);
      const wanted = SHOWN.has(view.status) || (view.status === 'applied' && view.undo);
      if (!wanted && !old) return;
      if (old && old.dataset.sig === JSON.stringify(view)) return;
      render(view, at);
    });
    log.querySelectorAll('.reading-card[data-id]').forEach((c) => { if (!seen.has(c.dataset.id)) c.remove(); });
    Object.keys(choices).forEach((id) => { if (!seen.has(id)) delete choices[id]; });
  }

  // ---------- after an apply: where the plan stands, and Make the timetable ----------
  // Blocking issues first, in plain words, with an offer to suggest fixes; with none, Make the timetable:
  // the draft from the plan (as the Requirements tab's Generate), then the quick timetable (as Build). A
  // timetable reading is the draft already: Make the timetable only builds it.
  async function makeTimetable(btn, say, fromPlan) {
    btn.disabled = true;
    try {
      if (fromPlan) {
        say('', 'Making the draft from the plan…');
        const r = await fetch('/api/plan/generate', { method: 'POST', headers: { 'Content-Type': 'application/json' } });
        const body = await r.json().catch(() => ({}));
        if (!r.ok) {
          const detail = typeof body.detail === 'string' ? body.detail : r.statusText;
          const blocks = Array.isArray(body.blocks) ? body.blocks.map(String) : [];
          say('bad', detail + (blocks.length ? ': ' + blocks.join('; ') : ''));
          btn.disabled = false;
          return;
        }
        if (typeof window.loadDraft === 'function') Promise.resolve(window.loadDraft()).catch(() => {});
        document.dispatchEvent(new CustomEvent('plane:journey', { detail: { stage: 'draft' } }));
      }
      say('', 'Making the timetable…');
      const res = await post('/api/build');
      if (res.ok) {
        document.dispatchEvent(new CustomEvent('plane:journey', { detail: { stage: 'timetable' } }));
        if (typeof window.reloadModel === 'function') Promise.resolve(window.reloadModel()).catch(() => {});   // plane:solid
        const placed = (res.placed || []).length;      // none after a kept aSc import: every lesson was fixed already
        say('good', placed ? `Timetable made: ${placed} lessons placed.` : 'Timetable made.');
      } else {
        const n = (res.unplaced || []).length, m = (res.clashes || []).length;
        say('bad', `The timetable did not settle: ${n} unplaced, ${m} clash${m === 1 ? '' : 'es'}. The draft is unchanged; open it to see why.`);
        if (typeof window.loadDraft === 'function') Promise.resolve(window.loadDraft()).catch(() => {});
        btn.disabled = false;
      }
    } catch (e) { say('bad', e.message); btn.disabled = false; }
  }

  function showStatus(status, kind) {
    if (!status) return;
    log.querySelectorAll('.plan-status').forEach((b) => b.remove());     // only the latest stands
    const box = node('div', 'plan-status');
    const line = node('p', 'ps-line');
    line.hidden = true;
    const say = (cls, text) => { line.className = 'ps-line' + (cls ? ' ' + cls : ''); line.textContent = text; line.hidden = false; scrollLog(); };
    const acts = node('div', 'acts');
    const button = (text, cls) => { const b = node('button', 'btn' + (cls ? ' ' + cls : ''), text); b.type = 'button'; acts.appendChild(b); return b; };
    const warns = status.warn_count ? ` ${status.warn_count} warning${status.warn_count === 1 ? '' : 's'} to look at.` : '';
    if (kind === 'timetable') {
      box.appendChild(node('p', 'ps-head', 'Draft ready: the timetable you gave is the draft now.'));
      const make = button('Make the timetable', 'primary');
      make.addEventListener('click', () => makeTimetable(make, say, false));
    } else if (status.ready) {
      box.appendChild(node('p', 'ps-head', 'Plan ready.' + warns));
      const make = button('Make the timetable', 'primary');
      make.addEventListener('click', () => makeTimetable(make, say, true));
    } else {
      box.appendChild(node('p', 'ps-head', 'Still to sort:'));
      const blocks = status.blocks || [];
      const ul = node('ul', 'ps-blocks');
      blocks.forEach((b) => ul.appendChild(node('li', null, String(b))));
      if (!blocks.length) ul.appendChild(node('li', null, 'The plan has no requirements yet.'));
      const more = (status.block_count || 0) - blocks.length;
      if (more > 0) ul.appendChild(node('li', 'ps-more', `and ${more} more`));
      box.appendChild(ul);
      if (blocks.length) {
        const fix = button('Suggest fixes');
        fix.addEventListener('click', () => { if (window.sendChat) window.sendChat("Suggest fixes for the plan's blocking issues"); });
      }
    }
    if (acts.childNodes.length) box.appendChild(acts);
    box.appendChild(line);
    log.appendChild(box);
    scrollLog();
  }

  // Resolves once every tick and option save in flight has finished (each save settles, failed or not): the
  // chat waits on it before sending a message, so a typed yes is applied against the ticks the card shows.
  function flush() {
    return Promise.all(Object.keys(choices).map((id) => choices[id].saving || Promise.resolve())).then(() => {});
  }

  // ---------- the Project tab's saved readers ----------
  // The layouts the smart reader has learned (an apply of a workbook reading saves one): listed with Delete,
  // which acts on its second click (embedded browsers suppress native confirms). Names are data: textContent.
  const getJson = async (path, method) => {
    const r = await fetch(path, { method: method || 'GET', headers: { 'Content-Type': 'application/json' } });
    const data = await r.json().catch(() => ({}));
    if (!r.ok) throw new Error(data.detail || r.statusText);
    return data;
  };

  function twoClick(label, again, aria, fn, failed) {
    const b = node('button', 'btn ghost', label);
    b.type = 'button';
    b.setAttribute('aria-label', aria);
    let armed = null;
    b.addEventListener('click', async () => {
      if (!armed) {
        b.textContent = again;
        armed = setTimeout(() => { armed = null; b.textContent = label; }, 4000);
        return;
      }
      clearTimeout(armed);
      armed = null;
      b.disabled = true;
      try { await fn(); } catch (e) { b.disabled = false; b.textContent = label; failed(e); }
    });
    return b;
  }

  let readersGen = 0;
  async function loadReaders() {
    const box = el('reader-list');
    if (!box) return;
    const my = ++readersGen;
    let got = null, failed = null;
    try { got = await getJson('/api/readers'); } catch (e) { failed = e; }
    if (my !== readersGen) return;
    box.textContent = '';
    if (failed) { box.appendChild(node('p', 'empty', failed.message)); return; }
    const list = (got && got.readers) || [];
    if (!list.length) {
      box.appendChild(node('p', 'empty', 'No saved readers yet. Apply a reading of a workbook and its layout is remembered here, so the next file like it is read without the AI.'));
      return;
    }
    list.forEach((x) => {
      const row = node('div', 'learning-item');
      const body = node('div', 'learning-body');
      body.appendChild(node('div', 'learning-text', x.name));
      const uses = Number(x.uses) || 0;
      body.appendChild(node('div', 'learning-evidence', `Used ${uses} ${uses === 1 ? 'time' : 'times'}`));
      row.appendChild(body);
      const acts = node('div', 'learning-actions');
      acts.appendChild(twoClick('Delete', 'Click again to delete', `Delete the saved reader ${x.name}`, async () => {
        await getJson(`/api/readers/${slug(x.signature)}`, 'DELETE');
        loadReaders();
      }, (e) => body.appendChild(node('div', 'learning-note', e.message))));
      row.appendChild(acts);
      box.appendChild(row);
    });
  }

  const planeFiles = { give, load, render, showStatus, flush };
  window.planeFiles = planeFiles;

  // ---------- the ways in ----------
  const attach = el('chat-attach'), picker = el('chat-files'), chat = el('chat');
  if (attach && picker) {
    attach.addEventListener('click', () => picker.click());
    picker.addEventListener('change', () => { const files = Array.from(picker.files || []); picker.value = ''; give(files); });
  }
  if (chat) {
    const hasFiles = (ev) => !!(ev.dataTransfer && Array.from(ev.dataTransfer.types || []).includes('Files'));
    ['dragenter', 'dragover'].forEach((t) => chat.addEventListener(t, (ev) => {
      if (!hasFiles(ev)) return;
      ev.preventDefault();
      chat.classList.add('chat-drop');
    }));
    chat.addEventListener('dragleave', (ev) => { if (!chat.contains(ev.relatedTarget)) chat.classList.remove('chat-drop'); });
    chat.addEventListener('drop', (ev) => {
      chat.classList.remove('chat-drop');
      if (!hasFiles(ev)) return;
      ev.preventDefault();
      give(ev.dataTransfer.files);
    });
  }

  document.addEventListener('DOMContentLoaded', load);
  document.addEventListener('plane:tab', (ev) => {
    load();
    if (ev.detail && ev.detail.key === 'project') loadReaders();
  });
  document.addEventListener('plane:readings', () => { loadReaders(); });   // a yes in the chat applies too
  document.addEventListener('plane:solid', () => { load(); });
})();
