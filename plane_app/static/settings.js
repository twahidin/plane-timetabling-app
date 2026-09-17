(function () {
  'use strict';

  const el = (id) => document.getElementById(id);
  const api = async (path, opts = {}) => {
    const r = await fetch(path, { headers: { 'Content-Type': 'application/json' }, ...opts });
    if (!r.ok) throw new Error((await r.json().catch(() => ({}))).detail || r.statusText);
    return r.json();
  };
  // Mirrors plane_app.llm.DEFAULT_BASE_URLS
  const DEFAULT_BASE_URLS = {
    openai: 'https://api.openai.com/v1',
    openrouter: 'https://openrouter.ai/api/v1',
    tokenrouter: 'https://api.tokenrouter.io/v1',
  };
  const DEFAULT_MODEL = { anthropic: 'claude-opus-5' };

  let labelsEdited = false;
  const msg = (text, cls) => { const m = el('msg'); m.textContent = text; m.className = 'msg-line ' + (cls || ''); };

  function makeLabels() {
    const mins = +el('slot_minutes').value, n = +el('slots_per_day').value, start = el('start').value || '00:00';
    if (!(mins > 0) || !(n > 0) || n > 64) return null;
    const [h, m] = start.split(':').map(Number);
    let t = (h || 0) * 60 + (m || 0);
    const out = [];
    for (let i = 0; i < n; i++) {
      const hh = Math.floor(t / 60) % 24, mm = t % 60;
      out.push(String(hh).padStart(2, '0') + ':' + String(mm).padStart(2, '0'));
      t += mins;
    }
    return out;
  }
  function regenLabels() {
    if (labelsEdited) return;
    const labels = makeLabels();
    if (labels) el('labels').value = labels.join(', ');
  }

  function fill(s) {
    const t = s.time || {}, r = s.rules || {}, p = s.provider || {}, e = s.engine || {};
    el('slot_minutes').value = t.slot_minutes ?? 40;
    el('slots_per_day').value = t.slots_per_day ?? 8;
    el('start').value = t.start || '07:30';
    el('labels').value = (t.labels || []).join(', ');
    el('max_load').value = r.max_load ?? 6;
    el('max_run').value = r.max_run ?? 4;
    el('mandatory_rest').value = (r.mandatory_rest || []).join(', ');
    el('provider_kind').value = p.kind || 'anthropic';
    el('model').value = p.model || '';
    el('base_url').value = p.base_url || '';
    el('api_key').value = p.api_key || '';
    el('engine_url').value = e.url || '';
    el('engine_key').value = e.key || '';
    labelsEdited = false;
  }

  function collect() {
    const ints = (id) => {
      const v = el(id).value.trim();
      const n = Number(v);
      if (v === '' || !Number.isInteger(n)) throw new Error(el(id).parentElement.firstChild.textContent.trim() + ' must be a whole number');
      return n;
    };
    const list = (id) => el(id).value.split(',').map((x) => x.trim()).filter(Boolean);
    const rest = list('mandatory_rest').map(Number);
    if (rest.some((n) => !Number.isInteger(n) || n < 0)) throw new Error('Mandatory rest slots must be whole numbers');
    const labels = list('labels');
    const n = ints('slots_per_day');
    if (labels.length !== n) throw new Error(`There are ${labels.length} labels for ${n} slots per day`);
    return {
      time: { slot_minutes: ints('slot_minutes'), slots_per_day: n, start: el('start').value, labels },
      rules: { max_load: ints('max_load'), max_run: ints('max_run'), mandatory_rest: rest },
      provider: { kind: el('provider_kind').value, base_url: el('base_url').value.trim(), api_key: el('api_key').value, model: el('model').value.trim() },
      engine: { url: el('engine_url').value.trim(), key: el('engine_key').value },
    };
  }

  ['slot_minutes', 'slots_per_day', 'start'].forEach((id) => el(id).addEventListener('input', regenLabels));
  el('labels').addEventListener('input', () => { labelsEdited = true; });
  el('provider_kind').addEventListener('change', () => {
    const kind = el('provider_kind').value;
    el('base_url').value = DEFAULT_BASE_URLS[kind] || '';
    el('model').value = DEFAULT_MODEL[kind] || '';
    el('api_key').value = '';   // a key belongs to one provider; never carry it across a switch
  });

  el('settings-form').addEventListener('submit', async (ev) => {
    ev.preventDefault();
    let body;
    try { body = collect(); } catch (e) { msg(e.message, 'bad'); return; }
    el('save').disabled = true; msg('Saving\u2026', '');
    try {
      const saved = await api('/api/settings', { method: 'PUT', body: JSON.stringify(body) });
      fill(saved);
      msg('Saved.', 'ok');
    } catch (e) { msg(e.message, 'bad'); }
    finally { el('save').disabled = false; }
  });

  api('/api/settings').then(fill).catch((e) => msg(e.message, 'bad'));
})();
