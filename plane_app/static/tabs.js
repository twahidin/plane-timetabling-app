(function () {
  'use strict';
  // What three tabs show beyond today's cards (spec docs/superpowers/specs/2026-09-29-nocturne-redesign-design.md §3):
  // the current timetable's locations, the timetable rules and checks, and the Consolidation heading. Read-only;
  // each loads when its tab opens (shell.js's plane:tab event).
  const el = (id) => document.getElementById(id);
  const api = async (path) => {
    const r = await fetch(path);
    if (!r.ok) throw new Error((await r.json().catch(() => ({}))).detail || r.statusText);
    return r.json();
  };
  const node = (tag, cls, text) => {
    const n = document.createElement(tag);
    if (cls) n.className = cls;
    if (text != null) n.textContent = text;
    return n;
  };
  const word = (k, o) => (window.Words && window.Words.word ? window.Words.word(k, o) : k);

  // ---------- Locations ----------
  let locGen = 0;
  async function loadLocations() {
    const my = ++locGen;
    const table = el('locations-table'), note = el('locations-empty');
    let org = null, failed = null;
    try { org = (await api('/api/solid')).organisation; } catch (e) { failed = e; }
    if (my !== locGen) return;
    table.textContent = '';
    const locs = (org && org.locations) || [];
    if (failed || !locs.length) {
      note.textContent = failed ? failed.message : 'No timetable yet.';
      note.hidden = false; table.hidden = true; return;
    }
    note.hidden = true; table.hidden = false;
    const head = node('tr');
    ['', 'Name', 'Capacity', 'Shared', 'Rest row'].forEach((h) => head.appendChild(node('th', null, h)));
    const thead = node('thead'); thead.appendChild(head); table.appendChild(thead);
    const body = node('tbody');
    locs.forEach((l, i) => {
      const tr = node('tr');
      const sw = node('span', 'sw');
      sw.style.background = l.rest ? 'var(--rest)' : `var(--loc-${(i % 5) + 1})`;   // the 3D view's colours
      const first = node('td'); first.appendChild(sw); tr.appendChild(first);
      tr.appendChild(node('td', null, l.name || l.id));
      tr.appendChild(node('td', 'n', l.rest ? '—' : String(l.cap == null ? '' : l.cap)));
      tr.appendChild(node('td', null, l.shared && !l.rest ? 'yes' : ''));
      tr.appendChild(node('td', null, l.rest ? 'yes' : ''));
      body.appendChild(tr);
    });
    table.appendChild(body);
  }

  // ---------- Constraints ----------
  const CHECKS = () => [
    'Nobody is in two places at once',
    `No ${word('venue')} is double-booked or over capacity`,
    `${word('requirement', { plural: true, cap: true })} that must start together do`,
    'Everyone works only when and where they may',
    'Nobody is overloaded or without rest',
  ];
  let conGen = 0;
  async function loadConstraints() {
    const my = ++conGen;
    const box = el('constraints-rules');
    let s = null, failed = null;
    try { s = await api('/api/settings'); } catch (e) { failed = e; }
    if (my !== conGen) return;
    box.textContent = '';
    if (failed) { box.appendChild(node('p', 'empty', failed.message)); return; }
    const r = s.rules || {}, labels = (s.time || {}).labels || [];
    const rest = (r.mandatory_rest || []).map((i) => labels[i] || `slot ${i + 1}`);
    const list = node('dl', 'kv');
    [
      [`Most ${word('requirement', { plural: true })} a ${word('person')} has in a day`, r.max_load == null ? '6' : String(r.max_load)],
      ['Most in a row without a break', r.max_run == null ? '4' : String(r.max_run)],
      ['Rest for everyone at', rest.length ? rest.join(', ') : 'none'],
    ].forEach(([k, v]) => { list.appendChild(node('dt', null, k)); list.appendChild(node('dd', null, v)); });
    box.appendChild(list);
    const link = node('a', null, 'Change these in Settings'); link.href = '/settings';
    const p = node('p', 'constraints-link'); p.appendChild(link); box.appendChild(p);
    box.appendChild(node('h3', null, 'What every timetable is checked for'));
    const ul = node('ul', 'constraints-checks');
    CHECKS().forEach((c) => ul.appendChild(node('li', null, c)));
    box.appendChild(ul);
  }

  // ---------- Consolidation heading ----------
  let conTitleGen = 0;
  async function loadConsolidation() {
    const my = ++conTitleGen;
    const t = el('consolidation-title');
    let s = null;
    try { s = await api('/api/solid'); } catch (e) { s = null; }
    if (my !== conTitleGen) return;
    if (!s) { t.textContent = 'Checks'; return; }
    const n = ((s.check && s.check.clashes) || []).length;
    t.textContent = !s.organisation ? 'No timetable yet.' : !s.check ? 'Not checked yet'
      : n ? `${n} issue${n > 1 ? 's' : ''} to clear` : 'No issues found';
  }

  document.addEventListener('plane:tab', (ev) => {
    const key = ev.detail && ev.detail.key;
    if (key === 'locations') loadLocations();
    else if (key === 'constraints') loadConstraints();
    else if (key === 'consolidation') loadConsolidation();
  });
})();
