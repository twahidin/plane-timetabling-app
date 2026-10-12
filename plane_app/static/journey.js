/* The journey bar (spec 2026-10-11-campus-looks-design.md §3.1): Files read → Plan ready → Draft → Timetable →
   Checked, from /api/journey, refetched when the page changes the timetable. A step that turns done while the
   page is open is announced as plane:journey (Plan ready, Checked: they are reached by many paths); every
   fetch is shared as plane:journey-state. */
(function () {
  const bar = document.getElementById('journey');
  if (!bar) return;
  let last = null, timer = 0;
  const fmt = (at) => {
    if (!at) return '';
    const d = new Date(at * 1000), today = new Date().toDateString() === d.toDateString();
    return today ? d.toTimeString().slice(0, 5) : d.toLocaleDateString(undefined, { day: 'numeric', month: 'short' });
  };
  function render(j) {
    for (const s of j.steps) {
      const b = bar.querySelector(`[data-step="${s.key}"]`);
      if (!b) continue;
      b.className = 'journey-step ' + s.state;
      b.querySelector('.w').textContent = s.state === 'warn' ? 'needs attention' : fmt(s.at);
      if (s.state === 'current') b.setAttribute('aria-current', 'step'); else b.removeAttribute('aria-current');
      b.title = s.state === 'warn' ? `${s.label}: reached ${fmt(s.at)}, but not true any more` : s.label;
    }
  }
  function announce(j) {
    if (!last) return;
    for (const s of j.steps) {
      const was = last.steps.find((x) => x.key === s.key);
      if ((s.key === 'plan' || s.key === 'checked') && s.state === 'done' && was && was.state !== 'done') {
        document.dispatchEvent(new CustomEvent('plane:journey', { detail: { stage: s.key } }));
      }
    }
  }
  async function load() {
    const r = await fetch('/api/journey', { credentials: 'same-origin' });
    if (!r.ok) return;
    const j = await r.json();
    if (last && last.timetable !== j.timetable) last = null;   // another timetable: nothing here just happened
    render(j); announce(j); last = j;
    document.dispatchEvent(new CustomEvent('plane:journey-state', { detail: j }));
  }
  const reload = () => { clearTimeout(timer); timer = setTimeout(() => { load().catch(() => {}); }, 350); };
  bar.addEventListener('click', (e) => {
    const b = e.target.closest('[data-step]');
    if (b && window.showTab) window.showTab(b.dataset.tab);
  });
  ['plane:solid', 'plane:plan', 'plane:readings'].forEach((ev) => document.addEventListener(ev, reload));
  // solver progress changes nothing the bar shows until the solve ends (then 'timetable' and plane:solid do)
  document.addEventListener('plane:journey', (e) => { if (e.detail && e.detail.stage === 'solving') return; reload(); });
  window.planeJourney = { state: () => last, reload };
  document.addEventListener('DOMContentLoaded', () => { load().catch(() => {}); });
})();
