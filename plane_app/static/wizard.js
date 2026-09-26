// The start wizard's side card (spec docs/superpowers/specs/2026-09-20-start-wizard-design.md
// §4, §7). The model drives the conversation via chat.py's wizard_* tools; this file only
// renders what those tools produce (the `kind: "wizard"` chat events, forwarded here by
// chat.js's handleEvents) and offers a "Choose" button that calls the same routes directly.
// No ids are ever shown to the user; everything is plain words. Every node is built with
// node()/textContent, never raw markup, the same convention chat.js and plan.js follow.
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
  function notify(cls, text) {
    if (typeof window.showNotes === 'function') window.showNotes([], [], [{ cls, text }]);
  }

  // The three links the wizard hands out (routes.py's DOWNLOADS): fixed paths, always the same
  // once a template has been chosen for this timetable.
  const DOWNLOADS = { workbook: '/api/wizard/workbook.xlsx', sample: '/api/wizard/sample.pdf', guide: '/api/wizard/guide' };

  // "cycle_days" -> "cycle days": a knob's key read as plain words, never shown as an identifier.
  const words = (key) => String(key).replace(/_/g, ' ');

  function factsLine(facts) {
    if (!facts) return '';
    const parts = [];
    if (facts.cycle_days) parts.push(`${facts.cycle_days}-day cycle`);
    // the timetable's own word for a slot: an hour-long one is an hour, never the engine's "slot"
    const unit = facts.unit || (facts.slot_minutes === 60 ? 'hours' : 'periods');
    if (facts.slots_per_day) parts.push(`${facts.slots_per_day} ${unit} a day`);
    if (facts.sample && facts.sample.events != null) parts.push(`${facts.sample.events} blocks`);
    return parts.join(' · ');
  }

  // ---------- decisions: a short recap of what has been chosen so far ----------
  let latestCandidates = [];   // the most recent preview batch this chat turn produced

  // A knob in plain words: "recess after: 4", "bands: yes".
  const knobLine = (k, v) => `${words(k)}: ${v === true ? 'yes' : v === false ? 'no' : v}`;
  const knobLines = (knobs) => Object.entries(knobs || {}).filter(([, v]) => v != null && v !== '').map(([k, v]) => knobLine(k, v));

  function decisionLines(candidates) {
    const lines = [];
    (candidates || []).forEach((c) => {
      const fl = factsLine(c.facts);
      lines.push(c.name ? (fl ? `${c.name}: ${fl}` : c.name) : fl);
      lines.push(...knobLines(c.knobs));
    });
    return lines.filter(Boolean);
  }

  // The recap lists one configuration's settings: the chosen one, or the only one on offer. Several
  // options at once each carry their own settings on their card instead of one long mixed list.
  function renderDecisions(candidates) {
    const box = el('wizard-decisions');
    box.textContent = '';
    const lines = (candidates || []).length === 1 ? decisionLines(candidates) : [];
    if (!lines.length) { box.hidden = true; return; }
    const list = node('ul');
    lines.forEach((l) => list.appendChild(node('li', null, l)));
    box.appendChild(list);
    box.hidden = false;
  }

  // ---------- candidate cards ----------
  async function chooseCandidate(btn, candidate) {
    btn.disabled = true;
    try {
      await api('/api/wizard/instantiate', { method: 'POST', body: JSON.stringify({ template: candidate.template, knobs: candidate.knobs }) });
      notify('good', 'Chosen. Fill in the workbook and drop it below.');
      latestCandidates = [];
      if (window.reloadModel) window.reloadModel().catch(() => {});
      if (window.loadPlan) window.loadPlan().catch(() => {});
      await loadWizard();
    } catch (e) { notify('error', e.message); }
    finally { btn.disabled = false; }
  }

  function candidateCard(c) {
    const card = node('div', 'wizard-candidate');
    card.appendChild(node('h4', null, c.name));
    const trade = (c.tradeoffs || [])[0];
    if (trade) card.appendChild(node('p', null, trade));
    const fl = factsLine(c.facts);
    if (fl) card.appendChild(node('div', 'wizard-facts', fl));
    const kl = knobLines(c.knobs);
    if (kl.length) {
      const list = node('ul', 'wizard-knobs');
      kl.forEach((l) => list.appendChild(node('li', null, l)));
      card.appendChild(list);
    }
    const btn = node('button', 'btn primary', 'Choose');
    btn.type = 'button';
    btn.addEventListener('click', () => chooseCandidate(btn, c));
    card.appendChild(btn);
    return card;
  }

  function renderCandidates(candidates) {
    const box = el('wizard-candidates');
    box.textContent = '';
    (candidates || []).forEach((c) => box.appendChild(candidateCard(c)));
  }

  // ---------- downloads ----------
  function renderDownloads(downloads) {
    const box = el('wizard-downloads');
    box.textContent = '';
    if (!downloads) { box.hidden = true; return; }
    const row = node('div', 'wizard-download-actions');
    const wb = node('button', 'btn', 'Workbook'); wb.type = 'button';
    wb.addEventListener('click', () => { window.location = downloads.workbook; });
    const sample = node('button', 'btn', 'Sample PDF'); sample.type = 'button';
    sample.addEventListener('click', () => { window.location = downloads.sample; });
    const guide = node('button', 'btn', 'Guide'); guide.type = 'button';
    guide.addEventListener('click', () => { window.open(downloads.guide, '_blank'); });
    row.appendChild(wb); row.appendChild(sample); row.appendChild(guide);
    box.appendChild(row);
    box.appendChild(node('p', 'wizard-note', 'Fill in the workbook and drop it below'));
    box.hidden = false;
  }

  // ---------- chat events: preview fills the card, done shows the downloads ----------
  // chat.js's handleEvents forwards every `{"kind": "wizard", ...}` event here.
  function renderWizardEvent(ev) {
    if (window.showIntakeTab) window.showIntakeTab('assistant');
    el('wizard').hidden = false;
    if (ev.stage === 'preview') {
      latestCandidates = ev.candidates || [];
      renderDecisions(latestCandidates);
      renderCandidates(latestCandidates);
      el('wizard-downloads').hidden = true;
    } else if (ev.stage === 'done') {
      latestCandidates = [];
      renderCandidates([]);
      renderDecisions([]);
      renderDownloads(ev.downloads || DOWNLOADS);
    }
  }
  window.renderWizardEvent = renderWizardEvent;

  // ---------- show/hide and initial load ----------
  // The card is shown when there is no live organisation and no plan yet, or once a wizard
  // record exists on this timetable and there is still no plan (spec §7).
  async function loadWizard() {
    // Any of the three failing (a dropped session, a network hiccup) must hide the card rather
    // than risk showing it over a live timetable it simply could not check: a fetch failure is
    // never grounds to believe there is no organisation and no plan.
    let record, solid, planData;
    try {
      [record, solid, planData] = await Promise.all([api('/api/wizard'), api('/api/solid'), api('/api/plan')]);
    } catch (e) {
      el('wizard').hidden = true;
      return;
    }
    const hasRecord = !!(record && record.template);
    const hasPlan = !!((planData.plan || {}).requirements || []).length;

    const show = !hasPlan && (solid.organisation == null || hasRecord);
    if (!show) {
      renderCandidates([]); renderDecisions([]); el('wizard-downloads').hidden = true;
      el('wizard').hidden = true;
      return;
    }

    if (hasRecord) {
      renderCandidates([]);
      renderDownloads(DOWNLOADS);
      try {
        const facts = await api('/api/wizard/preview', { method: 'POST', body: JSON.stringify({ template: record.template, knobs: record.knobs }) });
        renderDecisions([{ name: '', facts, knobs: record.knobs }]);
      } catch (e) { renderDecisions([]); }
      el('wizard').hidden = false;
    } else if (!latestCandidates.length) {
      renderCandidates([]);
      renderDecisions([]);
      el('wizard-downloads').hidden = true;
      el('wizard').hidden = true;             // nothing to show until the chat offers options
    }
  }

  window.loadWizard = loadWizard;

  loadWizard();
})();
