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

  // ---------- the Project tab's "Your templates" card (spec §1.3, §1.4) ----------
  // Save as template opens #template-dialog; the list shows each saved template with Delete, which asks for a
  // second click instead of a native confirm (embedded browsers suppress those).
  let tgen = 0;

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
      row.appendChild(body);
      const acts = node('div', 'learning-actions');
      const del = node('button', 'btn ghost', 'Delete');
      del.type = 'button';
      del.setAttribute('aria-label', `Delete ${t.name}`);
      let armed = null;
      del.addEventListener('click', async () => {
        if (!armed) {                                  // the first click asks; the second deletes
          del.textContent = 'Click again to delete';
          armed = setTimeout(() => { armed = null; del.textContent = 'Delete'; }, 4000);
          return;
        }
        clearTimeout(armed);
        del.disabled = true;
        try { await call(`/api/templates/local/${slug(t.id)}`, 'DELETE'); }
        catch (e) { body.appendChild(node('div', 'learning-note', e.message)); del.disabled = false; del.textContent = 'Delete'; armed = null; return; }
        loadTemplates();
      });
      acts.appendChild(del);
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
    if (ev.detail && ev.detail.key === 'project') loadTemplates();
  });
})();
