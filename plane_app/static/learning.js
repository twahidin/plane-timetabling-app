(function () {
  'use strict';
  // The Constraints tab's "Learned from how you work" card (spec docs/superpowers/specs/2026-09-30-learning-design.md
  // §1.2, §1.4): suggestions to accept or hide, the rules already learned with a switch each, and how many changes
  // were made after each of the last timetables. Loads when the tab opens (shell.js's plane:tab event) and after
  // every accept, hide and switch. Nothing changes until Accept is pressed. Also the Project tab's saved
  // wizard templates (below).
  const el = (id) => document.getElementById(id);
  const node = (tag, cls, text) => {
    const n = document.createElement(tag);
    if (cls) n.className = cls;
    if (text != null) n.textContent = text;
    return n;
  };
  const call = async (path, method, body) => {
    const opts = { method: method || 'GET' };
    if (body !== undefined) { opts.headers = { 'Content-Type': 'application/json' }; opts.body = JSON.stringify(body); }
    const r = await fetch(path, opts);
    const data = await r.json().catch(() => ({}));
    if (!r.ok) throw new Error(data.detail || r.statusText);
    return data;
  };
  const slug = (id) => encodeURIComponent(id);       // ids hold ':' and may hold spaces or slashes
  const day = (t) => {
    const d = new Date((t || 0) * 1000);
    return Number.isNaN(d.getTime()) ? '' : d.toLocaleDateString(undefined, { day: 'numeric', month: 'short', year: 'numeric' });
  };

  let gen = 0;
  const notes = {};                                   // learned rule id -> the message its last switch was refused with
  const sugNotes = {};                                // suggestion id -> the message its last Accept / Not now / Never failed with
  // Accepting or switching a rule changes the plan's rules; the Constraints tab's "Rules in the plan" table
  // (plan.js) holds the old values and would send them back on blur, so it is loaded again.
  const refreshPlan = () => { if (typeof window.loadPlan === 'function') Promise.resolve(window.loadPlan()).catch(() => {}); };

  function renderSuggestions(box, list) {
    box.textContent = '';
    const live = new Set(list.map((s) => s.id));
    Object.keys(sugNotes).forEach((id) => { if (!live.has(id)) delete sugNotes[id]; });    // its suggestion is gone
    if (!list.length) {
      box.appendChild(node('p', 'empty', 'No suggestions yet. They appear once the same kind of change has been made a few times.'));
      return;
    }
    list.forEach((s) => {
      const row = node('div', 'learning-item');
      const body = node('div', 'learning-body');
      body.appendChild(node('div', 'learning-text', s.text));
      body.appendChild(node('div', 'learning-evidence', s.evidence));
      if (sugNotes[s.id]) body.appendChild(node('div', 'learning-note', sugNotes[s.id]));
      row.appendChild(body);
      const acts = node('div', 'learning-actions');
      const act = (label, cls, fn, changesPlan) => {
        const b = node('button', 'btn' + (cls ? ' ' + cls : ''), label);
        b.type = 'button';
        b.addEventListener('click', async () => {
          acts.querySelectorAll('button').forEach((x) => { x.disabled = true; });
          try {
            await fn();
            delete sugNotes[s.id];
            if (changesPlan) refreshPlan();
          } catch (e) { sugNotes[s.id] = e.message; }       // kept across load(), which draws the list again
          load();
        });
        acts.appendChild(b);
      };
      act('Accept', 'primary', () => call(`/api/learning/suggestions/${slug(s.id)}/accept`, 'POST'), true);
      act('Not now', 'ghost', () => call(`/api/learning/suggestions/${slug(s.id)}/hide`, 'POST', { never: false }));
      act('Never', 'ghost', () => call(`/api/learning/suggestions/${slug(s.id)}/hide`, 'POST', { never: true }));
      row.appendChild(acts);
      box.appendChild(row);
    });
  }

  function renderRules(box, list) {
    box.textContent = '';
    if (!list.length) return;
    box.appendChild(node('h3', 'learning-sub', 'What it has learned'));
    list.forEach((e) => {
      const row = node('div', 'learning-item');
      const body = node('div', 'learning-body');
      const rule = e.rule || e.text;                    // an entry kept before rules were worded has only its question
      body.appendChild(node('div', 'learning-text', rule));
      body.appendChild(node('div', 'learning-evidence', `accepted ${day(e.accepted_at)}`));
      if (notes[e.id]) body.appendChild(node('div', 'learning-note', notes[e.id]));
      row.appendChild(body);
      const label = node('label', 'learning-switch');
      const box_ = node('input');
      box_.type = 'checkbox';
      box_.checked = !!e.active;
      box_.setAttribute('aria-label', `${e.active ? 'Switch off' : 'Switch on'}: ${rule}`);
      label.appendChild(box_);
      label.appendChild(node('span', null, e.active ? 'On' : 'Off'));
      box_.addEventListener('change', async () => {
        box_.disabled = true;
        delete notes[e.id];
        try { await call(`/api/learning/learned/${slug(e.id)}/switch`, 'POST', { on: box_.checked }); refreshPlan(); }
        catch (err) { notes[e.id] = err.message; }
        load();
      });
      row.appendChild(label);
      box.appendChild(row);
    });
  }

  function renderEdits(p, edits) {
    const counts = (edits && edits.per_solve) || [];
    if (!counts.length) { p.textContent = 'No timetables built yet.'; return; }
    const avg = edits.average == null ? '' : ` (average ${edits.average.toFixed(1)})`;
    p.textContent = `Changes you made after each of the last ${counts.length} timetables: ${counts.join(', ')}${avg}.`;
  }

  async function load() {
    const my = ++gen;
    const sug = el('learning-suggestions'), rules = el('learning-rules'), edits = el('learning-edits');
    if (!sug || !rules || !edits) return;
    let got = null, failed = null;
    try { got = await call('/api/learning'); } catch (e) { failed = e; }
    if (my !== gen) return;                            // a newer load is on its way
    if (failed) {
      sug.textContent = ''; rules.textContent = ''; edits.textContent = '';
      sug.appendChild(node('p', 'empty', failed.message));
      return;
    }
    renderSuggestions(sug, got.suggestions || []);
    renderRules(rules, got.learned || []);
    renderEdits(edits, got.edits);
  }

  // ---------- the Project tab's "Your templates" card (spec §1.3, §1.4, §2.2) ----------
  // Save as template opens #template-dialog; the list shows each saved template with Delete, which asks for a
  // second click instead of a native confirm (embedded browsers suppress those). A template not yet shared offers
  // Share with other schools (#share-dialog shows exactly what is sent); a shared one shows the review status and
  // Stop sharing, which also asks for a second click.
  let tgen = 0;
  const KINDS = { education: 'Education', health: 'Health', business: 'Business', sports: 'Sports' };
  const STATUS = { pending: 'Waiting for review', approved: 'Shared with other schools' };

  const statusText = (shared) => {
    if (shared.status === 'rejected') return shared.note ? `Not accepted: ${shared.note}` : 'Not accepted.';
    return STATUS[shared.status] || STATUS.pending;
  };

  // A button that acts on the second click only: the first changes its label for a few seconds. A refusal is
  // handed to `failed` and the button can be pressed again.
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
      try { await fn(); }
      catch (e) { b.disabled = false; b.textContent = label; failed(e); }
    });
    return b;
  }

  function renderTemplates(box, list) {
    box.textContent = '';
    if (!list.length) {
      box.appendChild(node('p', 'empty', 'No saved templates yet. Save this timetable as one and the start wizard offers it next time.'));
      return;
    }
    list.forEach((t) => {
      const row = node('div', 'learning-item');
      const body = node('div', 'learning-body');
      body.appendChild(node('div', 'learning-text', t.name));
      body.appendChild(node('div', 'learning-evidence', t.summary));
      const shared = t.shared && typeof t.shared === 'object' ? t.shared : null;
      if (shared) body.appendChild(node('div', 'learning-status', statusText(shared)));
      row.appendChild(body);
      const acts = node('div', 'learning-actions');
      const failed = (e) => body.appendChild(node('div', 'learning-note', e.message));    // says why, under the name
      if (shared) {
        const stop = twoClick('Stop sharing', 'Click again to stop sharing', `Stop sharing ${t.name}`, async () => {
          await call(`/api/templates/local/${slug(t.id)}/withdraw`, 'POST');
          loadTemplates();
        }, failed);
        acts.appendChild(stop);
      } else {
        const share = node('button', 'btn', 'Share with other schools');
        share.type = 'button';
        share.setAttribute('aria-label', `Share ${t.name} with other schools`);
        share.addEventListener('click', () => openShare(t));
        acts.appendChild(share);
      }
      acts.appendChild(twoClick('Delete', 'Click again to delete', `Delete ${t.name}`, async () => {
        await call(`/api/templates/local/${slug(t.id)}`, 'DELETE');
        loadTemplates();
      }, failed));
      row.appendChild(acts);
      box.appendChild(row);
    });
  }

  async function loadTemplates() {
    const my = ++tgen;
    const box = el('template-list');
    if (!box) return;
    let got = null, failed = null;
    try { got = await call('/api/templates/local'); } catch (e) { failed = e; }
    if (my !== tgen) return;
    if (failed) { box.textContent = ''; box.appendChild(node('p', 'empty', failed.message)); return; }
    renderTemplates(box, Array.isArray(got) ? got : []);
  }

  // Every time the Project tab opens the saved templates are drawn at once from the app's own record; once per page
  // load the review status of shared ones is then asked of the engine and, when it answers, drawn again. A slow or
  // unreachable shared library never holds the list back.
  let refreshed = false;
  async function refreshTemplates() {
    const first = loadTemplates();
    if (refreshed) return first;
    refreshed = true;
    const mark = tgen;                                 // the load just started
    let got = null;
    try { got = await call('/api/templates/shared/refresh', 'POST'); } catch (e) { got = null; }
    await first;
    const box = el('template-list');
    if (!box || !Array.isArray(got) || mark !== tgen) return;     // unreachable, or something newer drew the list
    ++tgen;
    renderTemplates(box, got);
  }

  // ---------- the share dialog: the template as a readable list, exactly what is sent ----------
  const plural = (n, one, many) => `${n} ${n === 1 ? one : (many || one + 's')}`;

  function lengthsText(lengths) {
    const parts = Object.keys(lengths || {}).map(Number).filter((k) => lengths[k] > 0).sort((a, b) => a - b)
      .map((k) => `${plural(lengths[k], 'lesson')} of ${plural(k, 'period')}`);
    return parts.join(', ');
  }

  function shareLines(t) {
    const out = [];
    const add = (text, subs) => out.push({ text, subs: subs || [] });
    const time = t.time || {}, rules = t.rules || {}, sample = t.sample || {}, voc = t.vocabulary || {};
    const unit = time.slot_minutes === 60 ? 'hour' : 'period';
    const cycle = ((t.knobs || []).find((k) => k.key === 'cycle_days') || {}).default;
    add(`Name: ${t.name}`);
    add(`About it: ${t.summary}`);
    add(`When to choose it: ${t.when_to_choose}`);
    add(`Kind: ${KINDS[t.domain] || t.domain}`);
    add(`Words it uses: ${[voc.person, voc.group, voc.requirement, voc.venue].filter(Boolean).join(', ')}`);
    add(`Days: ${cycle != null ? plural(cycle, 'day') + ' in the cycle, ' : ''}${plural(time.slots_per_day, unit)} a day ` +
        `of ${time.slot_minutes} minutes, starting at ${time.day_start}`);
    const rest = (rules.mandatory_rest || []).map((o) => `${unit} ${o + 1}`);
    add(`Rules: at most ${plural(rules.max_load_slots, unit)} a day and ${rules.max_run_slots} in a row for each ${voc.person || 'person'}` +
        (rest.length ? `; a break at ${rest.join(', ')}` : ''));
    if (t.sheets === 'deployment') {
      add(`Shape: ${plural(sample.levels, 'level')} of ${plural(sample.classes_per_level, voc.group || 'class', (voc.group || 'class') + 'es')}, ` +
          `${plural(sample.teachers, voc.person || 'teacher')}`);
      add('Subjects:', (sample.subjects || []).map((s) =>
        `${s.name}: ${plural(s.periods, unit)} (${lengthsText(s.lengths)})${s.band ? ', in an option block' : ''}`));
    } else {
      add(`Shape: ${plural(sample.units, voc.group || 'unit')}, ${plural(sample.staff, voc.person || 'person', (voc.person || 'person') + 's')}`);
      add(`${(voc.requirement || 'duty').replace(/^./, (c) => c.toUpperCase())}s:`, (sample.duties || []).map((d) =>
        `${d.name}: ${plural(d.per_cycle, 'time')} a cycle, ${plural(d.length_slots, unit)} long, at least ${plural(d.min_staff, voc.person || 'person', (voc.person || 'person') + 's')}`));
    }
    const learned = [];
    const pr = t.plan_rules;
    if (pr) {
      if ((pr.edge_subjects || []).length) learned.push(`Kept away from the first and last ${unit}: ${pr.edge_subjects.join(', ')}`);
      learned.push(pr.no_double_across_rest ? 'No double lesson runs across a break' : 'Double lessons may run across a break');
    }
    if (t.solve && t.solve.preset) learned.push(`Timetable preference: ${t.solve.preset}`);
    if (learned.length) add('Learned rules:', learned);
    (t.tradeoffs || []).forEach((x) => add(`Note: ${x}`));
    return out;
  }

  // Everything that is sent, field by field, below the summary: the dialog promises "exactly what other schools
  // will see", so nothing in the body is left out of it. One line per value, its place in plain words
  // ("knobs › 1 › help: …").
  const fieldWords = (key) => String(key).replace(/_/g, ' ');
  function everyField(value, path, out) {
    const at = path.join(' › ');
    if (Array.isArray(value)) {
      if (!value.length) out.push(`${at}: none`);
      value.forEach((v, i) => everyField(v, path.concat(String(i + 1)), out));
    } else if (value && typeof value === 'object') {
      const keys = Object.keys(value);
      if (!keys.length) out.push(`${at}: none`);
      keys.forEach((k) => everyField(value[k], path.concat(fieldWords(k)), out));
    } else {
      out.push(`${at}: ${value === true ? 'yes' : value === false ? 'no' : value}`);
    }
    return out;
  }
  const sentBody = (t) => {
    const body = {};
    Object.keys(t).forEach((k) => { if (k !== 'shared') body[k] = t[k]; });      // the app's own record is never sent
    return body;
  };

  const sdlg = el('share-dialog'), sform = el('share-form'), serr = el('share-error');
  let sharing = null;                                  // the template the dialog is open for
  const sclose = () => { if (sdlg.open) sdlg.close(); else sdlg.removeAttribute('open'); };

  function openShare(t) {
    if (!sdlg || !sform) return;
    sharing = t;
    const list = el('share-list');
    list.textContent = '';
    shareLines(t).forEach((line) => {
      const li = node('li', null, line.text);
      if (line.subs.length) {
        const ul = node('ul');
        line.subs.forEach((x) => ul.appendChild(node('li', null, x)));
        li.appendChild(ul);
      }
      list.appendChild(li);
    });
    const full = el('share-full');
    if (full) {
      full.textContent = '';
      const body = sentBody(t);
      Object.keys(body).forEach((k) => everyField(body[k], [fieldWords(k)], []).forEach((line) => full.appendChild(node('li', null, line))));
    }
    if (el('share-all')) el('share-all').open = false;
    serr.hidden = true; serr.textContent = '';
    el('share-ok').disabled = false;
    if (typeof sdlg.showModal === 'function') sdlg.showModal(); else sdlg.setAttribute('open', '');
    el('share-ok').focus();
  }

  if (sdlg && sform) {
    el('share-cancel').addEventListener('click', sclose);
    sform.addEventListener('submit', async (ev) => {
      ev.preventDefault();
      if (!sharing) return sclose();
      el('share-ok').disabled = true;
      try {
        await call(`/api/templates/local/${slug(sharing.id)}/publish`, 'POST');
        sclose();
        loadTemplates();
      } catch (e) { serr.textContent = e.message; serr.hidden = false; }
      finally { el('share-ok').disabled = false; }
    });
  }

  const tdlg = el('template-dialog'), tform = el('template-form'), terr = el('template-error');
  const tclose = () => { if (tdlg.open) tdlg.close(); else tdlg.removeAttribute('open'); };
  if (tdlg && tform && el('template-save')) {
    el('template-save').addEventListener('click', () => {
      tform.reset();
      terr.hidden = true; terr.textContent = '';
      if (typeof tdlg.showModal === 'function') tdlg.showModal(); else tdlg.setAttribute('open', '');
      el('template-name').focus();
    });
    el('template-cancel').addEventListener('click', tclose);
    tform.addEventListener('submit', async (ev) => {
      ev.preventDefault();
      const body = { name: el('template-name').value.trim(), summary: el('template-summary').value.trim(),
        when_to_choose: el('template-when').value.trim(), domain: el('template-domain').value };
      const fail = (msg) => { terr.textContent = msg; terr.hidden = false; };
      if (!body.name) return fail('Give the template a name.');
      if (!body.summary) return fail('Say in one line what it is.');
      if (!body.when_to_choose) return fail('Say when someone should choose it.');
      el('template-ok').disabled = true;
      try {
        await call('/api/templates/local', 'POST', body);
        tclose();
        loadTemplates();
      } catch (e) { fail(e.message); }
      finally { el('template-ok').disabled = false; }
    });
  }

  document.addEventListener('plane:tab', (ev) => {
    if (ev.detail && ev.detail.key === 'constraints') load();
    if (ev.detail && ev.detail.key === 'project') refreshTemplates();
  });
})();
