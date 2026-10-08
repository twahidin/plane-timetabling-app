(function () {
  'use strict';
  // The smart workbook reader's cards (spec docs/superpowers/specs/2026-10-08-smart-workbook-reader-design.md
  // §4, §5). The Requirements tab shows each reading waiting for Apply: its file, what the reader says it is,
  // how many of each thing it read, a few sample records, its doubts, and Apply / Discard. Nothing changes the
  // plan until Apply. The Project tab lists the saved readers (layouts the app has learned) with Delete, on a
  // second click. Everything the model or the workbook wrote is shown with textContent only.
  // Loads when its tab opens (shell.js's plane:tab) and when an upload made a reading (chat.js's plane:readings).
  const el = (id) => document.getElementById(id);
  const node = (tag, cls, text) => {
    const n = document.createElement(tag);
    if (cls) n.className = cls;
    if (text != null) n.textContent = text;
    return n;
  };
  const call = async (path, method) => {
    const r = await fetch(path, { method: method || 'GET', headers: { 'Content-Type': 'application/json' } });
    const data = await r.json().catch(() => ({}));
    if (!r.ok) throw new Error(data.detail || r.statusText);
    return data;
  };
  const slug = (id) => encodeURIComponent(id);

  // what each target's records are, one and many
  const WORDS = {
    venues: ['room', 'rooms'], homes: ['home room', 'home rooms'], classes: ['class', 'classes'],
    staff: ['staff member', 'staff'], requirements: ['requirement', 'requirements'],
  };
  const TITLES = { venues: 'Rooms', homes: 'Home rooms', classes: 'Classes', staff: 'Staff', requirements: 'Requirements' };
  const word = (target, n) => { const w = WORDS[target] || [target, target]; return n === 1 ? w[0] : w[1]; };
  const countsLine = (counts) => Object.keys(counts || {}).map((t) => `${counts[t]} ${word(t, counts[t])}`).join(' · ');
  const cellText = (v) => (Array.isArray(v) ? v.join(', ') : (v == null ? '' : String(v)));

  // A button that acts on the second click only (embedded browsers suppress native confirms).
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

  // ---------- the Requirements tab's reading cards ----------
  function sampleTable(target, records) {
    const wrap = node('div', 'reading-sample');
    wrap.appendChild(node('h4', null, TITLES[target] || target));
    const keys = [];
    records.forEach((r) => Object.keys(r || {}).forEach((k) => { if (k !== 'at' && !keys.includes(k)) keys.push(k); }));
    const hasAt = records.some((r) => r && r.at);
    const table = node('table', 'grid');
    const head = node('tr');
    keys.forEach((k) => head.appendChild(node('th', null, k)));
    if (hasAt) head.appendChild(node('th', null, 'cell'));
    table.appendChild(head);
    records.forEach((r) => {
      const tr = node('tr');
      keys.forEach((k) => tr.appendChild(node('td', null, cellText(r[k]))));
      if (hasAt) tr.appendChild(node('td', 'id', cellText(r.at)));
      table.appendChild(tr);
    });
    const scroll = node('div', 'tablewrap');
    scroll.appendChild(table);
    wrap.appendChild(scroll);
    return wrap;
  }

  function card(r) {
    const c = node('section', 'reading-card');
    const head = node('div', 'reading-head');
    head.appendChild(node('span', 'reading-file', r.file));
    head.appendChild(node('span', 'eyebrow', 'waiting for Apply'));
    c.appendChild(head);
    if (r.reader) c.appendChild(node('p', 'reading-reader', `Read with your saved reader '${r.reader}'`));
    if (r.summary && r.summary !== r.reader) c.appendChild(node('p', 'reading-summary', r.summary));
    c.appendChild(node('p', 'reading-counts', countsLine(r.counts)));
    const samples = r.samples || {};
    const keys = Object.keys(samples).filter((t) => Array.isArray(samples[t]) && samples[t].length);
    if (keys.length) {
      const box = node('div', 'reading-samples');
      keys.forEach((t) => box.appendChild(sampleTable(t, samples[t])));
      c.appendChild(box);
    }
    const doubts = r.doubts || [];
    if (doubts.length) {
      const d = node('details', 'reading-doubts');
      d.appendChild(node('summary', null, `${doubts.length} ${doubts.length === 1 ? 'thing' : 'things'} to check`));
      const ul = node('ul');
      doubts.forEach((t) => ul.appendChild(node('li', null, t)));
      d.appendChild(ul);
      c.appendChild(d);
    }
    const msg = node('p', 'reading-note');
    msg.hidden = true;
    const say = (cls, text) => { msg.className = 'reading-note ' + cls; msg.textContent = text; msg.hidden = false; };
    const acts = node('div', 'reading-actions');
    const apply = node('button', 'btn primary', 'Apply');
    apply.type = 'button';
    apply.setAttribute('aria-label', `Apply ${r.file} to the plan`);
    const discard = node('button', 'btn ghost', 'Discard');
    discard.type = 'button';
    discard.setAttribute('aria-label', `Discard the reading of ${r.file}`);
    apply.addEventListener('click', async () => {
      apply.disabled = true; discard.disabled = true;
      try {
        const out = await call(`/api/readings/${slug(r.id)}/apply`, 'POST');
        if (typeof window.loadPlan === 'function') Promise.resolve(window.loadPlan()).catch(() => {});
        if (typeof window.loadBoard === 'function') Promise.resolve(window.loadBoard()).catch(() => {});
        if (typeof window.loadWizard === 'function') Promise.resolve(window.loadWizard()).catch(() => {});
        const kept = out.reader ? ` The layout is saved as '${out.reader}'.` : '';
        status('good', `Applied ${r.file}: ${countsLine(out.counts)}.${kept}${out.note ? ' ' + out.note : ''}`);
        loadReadings();
        loadReaders();
      } catch (e) { say('error', e.message); apply.disabled = false; discard.disabled = false; }
    });
    discard.addEventListener('click', async () => {
      apply.disabled = true; discard.disabled = true;
      try { await call(`/api/readings/${slug(r.id)}`, 'DELETE'); status('', `Discarded the reading of ${r.file}.`); loadReadings(); }
      catch (e) { say('error', e.message); apply.disabled = false; discard.disabled = false; }
    });
    acts.appendChild(apply);
    acts.appendChild(discard);
    const tpl = node('a', 'reading-template', 'Download the template');
    tpl.href = '/api/plan/template.xlsx';
    tpl.setAttribute('download', '');
    const alt = node('span', 'reading-alt', 'Or fill in the template instead. ');
    alt.appendChild(tpl);
    acts.appendChild(alt);
    c.appendChild(acts);
    c.appendChild(msg);
    return c;
  }

  let lastStatus = null;            // the last Apply / Discard outcome, shown above the cards until the next load after it
  function status(cls, text) { lastStatus = { cls, text }; }

  let gen = 0;
  async function loadReadings() {
    const box = el('readings');
    if (!box) return;
    const my = ++gen;
    let got = null, failed = null;
    try { got = await call('/api/readings'); } catch (e) { failed = e; }
    if (my !== gen) return;
    box.textContent = '';
    if (lastStatus) { box.appendChild(node('p', 'reading-note ' + lastStatus.cls, lastStatus.text)); lastStatus = null; }
    if (failed) { box.appendChild(node('p', 'reading-note error', failed.message)); return; }
    ((got && got.readings) || []).forEach((r) => box.appendChild(card(r)));
  }
  window.loadReadings = loadReadings;

  // ---------- the Project tab's saved readers ----------
  let rgen = 0;
  async function loadReaders() {
    const box = el('reader-list');
    if (!box) return;
    const my = ++rgen;
    let got = null, failed = null;
    try { got = await call('/api/readers'); } catch (e) { failed = e; }
    if (my !== rgen) return;
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
        await call(`/api/readers/${slug(x.signature)}`, 'DELETE');
        loadReaders();
      }, (e) => body.appendChild(node('div', 'learning-note', e.message))));
      row.appendChild(acts);
      box.appendChild(row);
    });
  }
  window.loadReaders = loadReaders;

  document.addEventListener('plane:tab', (ev) => {
    const key = ev.detail && ev.detail.key;
    if (key === 'requirements') loadReadings();
    if (key === 'project') loadReaders();
  });
  document.addEventListener('plane:readings', () => { loadReadings(); });
})();
