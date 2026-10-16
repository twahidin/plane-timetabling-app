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
  const DEFAULT_MODEL = { anthropic: 'claude-opus-5-5' };
  // The Claude models Settings lists (mirrors plane_app.llm.anthropic_provider.FALLBACK_MODELS); any other
  // model, and every model of an OpenAI-compatible provider, is typed into the Model name box.
  const CLAUDE_MODELS = ['claude-opus-5-5', 'claude-sonnet-5-5', 'claude-fable-5-1'];
  function showModel(kind, model) {
    const listed = kind === 'anthropic' && CLAUDE_MODELS.includes(model);
    const choice = el('model_choice');
    choice.hidden = kind !== 'anthropic';
    choice.closest('label').hidden = kind !== 'anthropic';
    choice.value = listed ? model : 'other';
    el('model').value = listed ? '' : (model || '');
    el('model_label').hidden = listed;
    el('model_help').textContent = kind === 'anthropic' ? 'a Claude model ID, for example claude-haiku-4-5'
      : 'exactly as your provider lists it, for example an OpenAI model name';
  }
  function chosenModel() {
    const kind = el('provider_kind').value;
    if (kind === 'anthropic' && el('model_choice').value !== 'other') return el('model_choice').value;
    return el('model').value.trim();
  }
  // Mirrors plane_app.db.PRESETS (the server is the authority; these fill the inputs when a preset is picked)
  const RULES = ['spread', 'stability', 'compact', 'even_days', 'edge', 'venue'];
  const PRESETS = {
    close: { stability: 40, spread: 6, compact: 4, even_days: 3, edge: 2, venue: 1 },
    balanced: { spread: 10, stability: 8, compact: 6, even_days: 3, edge: 2, venue: 1 },
    quality: { stability: 0, spread: 14, compact: 8, even_days: 5, edge: 3, venue: 1 },
  };

  const msg = (text, cls) => { const m = el('msg'); m.textContent = text; m.className = 'msg-line ' + (cls || ''); };

  // ---- the week (week-editor.js) and the calendar fields that follow its cycle ----
  let editor = null;
  let stored = { time: {}, rules: {} };     // the week as saved, to tell a changed grid from an unchanged one
  let loadedWeek = '';                      // the week the editor showed when filled: unchanged, it is not sent

  function showCalendar(t, pick) {          // Odd/Even for a cycle of two or more weeks; a start day for a rolling one
    const n = t.days.length, rolling = t.cycle === 'rolling', rs = el('rolling-start');
    el('first-week').closest('label').hidden = rolling || n < 10;
    rs.closest('label').hidden = !rolling;
    const want = Math.min(Math.max(pick || +rs.value || 1, 1), Math.max(n, 1));
    if (rs.querySelectorAll('option').length !== n) {
      rs.replaceChildren(...Array.from({ length: n }, (_, k) => {
        const o = document.createElement('option');
        o.value = String(k + 1); o.textContent = `Day ${k + 1}`;
        return o;
      }));
    }
    rs.value = String(want);
  }

  // ---- an old time the week cannot read (a 24-hour ward, more than 20 days): the Time grid inputs, as before ----
  let grid = false;                         // the old Time grid inputs show instead of the week editor
  let labelsEdited = false;
  let labelsPerDay = 8;                     // the slots per day the labels in the field were made for
  const labelList = () => el('labels').value.split(',').map((x) => x.trim()).filter(Boolean);
  function showCycle() {                    // "5-day cycle · 40 labels" under the labels field
    const n = +el('slots_per_day').value, labels = labelList(), hint = el('cycle-hint');
    if (!hint) return;
    const problem = window.SettingsLabels.check(labels, n);
    hint.textContent = problem || `${window.SettingsLabels.cycleDays(labels, n)}-day cycle · ${labels.length} labels`;
    hint.classList.toggle('bad', !!problem);
  }
  function regenLabels() {                  // every day of the cycle, keeping day names and the period style
    if (!labelsEdited) {
      const mins = +el('slot_minutes').value, n = +el('slots_per_day').value;
      if (mins > 0 && n > 0 && n <= 64) {
        el('labels').value = window.SettingsLabels.regenerate(labelList(), labelsPerDay, n, mins, el('start').value || '00:00').join(', ');
        labelsPerDay = n;
      }
    }
    showCycle();
  }
  function fillGrid(t, r) {
    el('slot_minutes').value = t.slot_minutes ?? 40;
    el('slots_per_day').value = t.slots_per_day ?? 8;
    el('start').value = t.start || '07:30';
    el('labels').value = (t.labels || []).join(', ');
    el('mandatory_rest').value = (r.mandatory_rest || []).join(', ');
    labelsEdited = false;
    labelsPerDay = t.slots_per_day ?? 8;
    showCycle();
    el('first-week').closest('label').hidden = false;      // the calendar as before: Odd/Even, no rolling start day
    el('rolling-start').closest('label').hidden = true;
  }

  function weekOf() {                       // the time to send: the editor's week and, when rolling, the start day
    const t = editor.value();
    const time = { slot_minutes: t.slot_minutes, start: t.start, cycle: t.cycle, days: t.days, label_style: t.label_style };
    return Object.assign(time, t.cycle === 'rolling' ? { rolling_start_day: +el('rolling-start').value || 1 } : {});
  }

  function fill(s) {
    const t = s.time || {}, r = s.rules || {}, p = s.provider || {}, e = s.engine || {}, c = s.calendar || {};
    stored = { time: t, rules: r };
    grid = !window.WeekEditor.editable(t, r);
    el('week').hidden = grid;
    el('time-grid').hidden = !grid;
    el('mandatory_rest').closest('label').hidden = !grid;
    if (grid) {
      editor = null; loadedWeek = '';
      el('week-editor').replaceChildren();
      fillGrid(t, r);
    } else {
      editor = window.WeekEditor.mount(el('week-editor'), t, { rules: r, onChange: (w) => showCalendar(w) });
      showCalendar(editor.value(), t.rolling_start_day);
      loadedWeek = JSON.stringify(weekOf());
    }
    el('max_load').value = r.max_load ?? 6;
    el('max_run').value = r.max_run ?? 4;
    el('provider_kind').value = p.kind || 'anthropic';
    showModel(p.kind || 'anthropic', p.model || '');
    el('base_url').value = p.base_url || '';
    el('api_key').value = p.api_key || '';
    el('engine_url').value = e.url || '';
    el('engine_key').value = e.key || '';
    el('term-start').value = c.term_start || '';
    el('first-week').value = c.first_week || 'odd';
    el('non-teaching').value = (c.non_teaching_dates || []).join('\n');
    const sv = s.solve || {}, w = sv.weights || PRESETS.balanced;
    el('solve_preset').value = sv.preset || 'balanced';
    el('solve_time_limit').value = sv.time_limit ?? 300;
    RULES.forEach((r) => { el('w_' + r).value = w[r] ?? PRESETS.balanced[r]; });
    const lk = (s.look || {}).default || 'classic';
    document.querySelectorAll('input[name=look]').forEach((r) => { r.checked = r.value === lk; });
  }

  function collect() {
    const ints = (id) => {
      const v = el(id).value.trim();
      const n = Number(v);
      if (v === '' || !Number.isInteger(n)) throw new Error(el(id).parentElement.firstChild.textContent.trim() + ' must be a whole number');
      return n;
    };
    if (!editor && !grid) throw new Error('Settings have not loaded yet; reload the page.');
    let time = null, rest = null;
    if (grid) {                             // the old time grid, sent as before: labels for whole days, rest offsets
      const list = (id) => el(id).value.split(',').map((x) => x.trim()).filter(Boolean);
      rest = list('mandatory_rest').map(Number);
      if (rest.some((n) => !Number.isInteger(n) || n < 0)) throw new Error('Mandatory rest slots must be whole numbers');
      const labels = list('labels'), n = ints('slots_per_day');
      const bad = window.SettingsLabels.check(labels, n);
      if (bad) throw new Error(bad);
      time = { slot_minutes: ints('slot_minutes'), slots_per_day: n, start: el('start').value, labels };
    } else {
      // an unchanged week is not sent, so saving other settings never rewrites an old timetable's week
      const week = weekOf(), weekChanged = JSON.stringify(week) !== loadedWeek;
      const problem = weekChanged ? editor.problem() : null;
      if (problem) throw new Error(problem);
      if (weekChanged) time = week;
    }
    return {
      ...(time ? { time } : {}),
      rules: { max_load: ints('max_load'), max_run: ints('max_run'), ...(rest ? { mandatory_rest: rest } : {}) },
      provider: { kind: el('provider_kind').value, base_url: el('base_url').value.trim(), api_key: el('api_key').value, model: chosenModel() },
      engine: { url: el('engine_url').value.trim(), key: el('engine_key').value },
      solve: { preset: el('solve_preset').value, time_limit: solveLimit(), weights: Object.fromEntries(RULES.map((r) => [r, weight(r)])) },
      look: { default: (document.querySelector('input[name=look]:checked') || { value: 'classic' }).value },
      calendar: {
        term_start: el('term-start').value,
        // a hidden Odd/Even (a one-week or rolling cycle) is not sent: the stored value stays, unused
        ...(el('first-week').closest('label').hidden ? {} : { first_week: el('first-week').value }),
        non_teaching_dates: el('non-teaching').value.split('\n').map((x) => x.trim()).filter(Boolean),
      },
    };
    function solveLimit() {
      const n = ints('solve_time_limit');
      if (n < 10 || n > 900) throw new Error('Time limit must be between 10 and 900 seconds');
      return n;
    }
    function weight(r) {
      const n = ints('w_' + r);
      if (n < 0) throw new Error(el('w_' + r).parentElement.firstChild.textContent.trim() + ' must be 0 or more');
      return n;
    }
  }

  ['slot_minutes', 'slots_per_day', 'start'].forEach((id) => el(id).addEventListener('input', regenLabels));
  el('labels').addEventListener('input', () => { labelsEdited = true; showCycle(); });
  el('solve_preset').addEventListener('change', () => {
    const w = PRESETS[el('solve_preset').value];
    if (w) RULES.forEach((r) => { el('w_' + r).value = w[r]; });
  });
  RULES.forEach((r) => el('w_' + r).addEventListener('input', () => { el('solve_preset').value = 'custom'; }));
  el('model_choice').addEventListener('change', () => {
    const other = el('model_choice').value === 'other';
    el('model_label').hidden = !other;
    if (other) el('model').focus();
  });
  el('provider_kind').addEventListener('change', () => {
    const kind = el('provider_kind').value;
    el('base_url').value = DEFAULT_BASE_URLS[kind] || '';
    showModel(kind, DEFAULT_MODEL[kind] || '');
    el('api_key').value = '';   // a key belongs to one provider; never carry it across a switch
  });

  el('settings-form').addEventListener('submit', async (ev) => {
    ev.preventDefault();
    let body;
    try { body = collect(); } catch (e) { msg(e.message, 'bad'); return; }
    el('save').disabled = true; msg('Saving\u2026', '');
    try {
      const newGrid = !!body.time && !grid && window.WeekEditor.gridChanged(stored.time, stored.rules, body.time);
      if (newGrid) {                        // a new grid: say what it costs before saving
        const imp = await api('/api/week/impact', { method: 'POST', body: JSON.stringify({ time: body.time }) });
        const question = window.WeekEditor.saveQuestion(imp);
        if (question && !confirm(question)) { msg('Not saved.', ''); return; }
      }
      const saved = await api('/api/settings', { method: 'PUT', body: JSON.stringify(body) });
      fill(saved);
      document.documentElement.dataset.schoolLook = (saved.look || {}).default || 'classic';
      if (window.planeLook) window.planeLook.refresh();
      const changes = window.WeekEditor.savedText(saved.week_changes, newGrid);
      msg(changes ? `Saved. ${changes}` : 'Saved.', changes ? '' : 'ok');
    } catch (e) { msg(e.message, 'bad'); }
    finally { el('save').disabled = false; }
  });

  api('/api/settings').then(fill).catch((e) => msg(e.message, 'bad'));
})();
