// Settings: Learning (spec docs/superpowers/specs/2026-09-30-learning-design.md §3.3). One switch, whether this
// school shares solved problems (the kind of clash and the kind of fix, with a count) with other schools.
// It has its own Save button and message line, so it never waits on the rest of the page's form.
(function () {
  'use strict';

  const el = (id) => document.getElementById(id);
  const box = el('share-fixes');
  const save = el('learning-save');
  if (!box || !save) return;
  const say = (text, cls) => { const m = el('learning-msg'); m.textContent = text || ''; m.className = 'msg-line ' + (cls || ''); };
  async function api(path, opts = {}) {
    const r = await fetch(path, { headers: { 'Content-Type': 'application/json' }, ...opts });
    const data = await r.json().catch(() => ({}));
    if (!r.ok) throw new Error(typeof data.detail === 'string' ? data.detail : r.statusText);
    return data;
  }

  save.addEventListener('click', async () => {
    save.disabled = true;
    say('Saving…', '');
    try {
      const got = await api('/api/learning/sharing', { method: 'PUT', body: JSON.stringify({ fixes: box.checked }) });
      box.checked = got.fixes === true;
      say(box.checked ? 'Saved. Solved problems are shared.' : 'Saved. Nothing more is sent.', 'ok');
    } catch (e) {
      say(e.message, 'bad');
    } finally {
      save.disabled = false;
    }
  });

  api('/api/learning/sharing').then((got) => { box.checked = got.fixes === true; }).catch((e) => say(e.message, 'bad'));
})();
