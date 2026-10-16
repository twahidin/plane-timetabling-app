/* The week editor (spec docs/superpowers/specs/2026-10-15-week-shape-design.md §1): the period length, the
   cycle, one row a day of the cycle (its start, its periods and where the breaks fall), shortcuts to copy a
   day, a preview of each day's times and the problem said in place. Settings and the wizard mount it:
     window.WeekEditor.mount(host, time, {rules?, onChange?(time)})
       -> {value(), problem(), addBreak(day, after), removeBreak(day, i), copyDay(from, toList),
           makeAllLikeFirst(), setCycle(cycle, nDays)}
   WeekEditor.gridChanged(storedTime, storedRules, time), .saveQuestion(impact) and .changesText(week_changes)
   are the save's shared words; WeekEditor.editable(time, rules) says whether a time can be shown at all. The buttons call these same functions. Pure DOM; every data text is set through textContent.
   A time without `days` is shown as the week week.js reads (Week.fromLegacy). */
(function () {
  const WEEK_COUNTS = [[1, 'One day (every weekday alike)'], [5, '1 week'], [10, '2 weeks (Odd and Even)'], [15, '3 weeks'], [20, '4 weeks']];
  const NEW_BREAK = { name: 'Break', minutes: 20 };
  const isObj = (v) => v !== null && typeof v === 'object' && !Array.isArray(v);
  const toInt = (v) => {
    const s = String(v == null ? '' : v).trim();
    const n = Number(s);
    return s !== '' && Number.isInteger(n) ? n : null;
  };
  const intOr = (v, d) => (Number.isInteger(v) ? v : d);
  const plural = (n, word) => `${n} ${word}${n === 1 ? '' : 's'}`;
  const clockLabels = (labels) => Array.isArray(labels) && labels.length > 0
    && labels.every((l) => /^\d{1,2}:\d{2}$/.test(String(l).slice(String(l).lastIndexOf(' ') + 1)));

  function h(tag, props, kids) {
    const e = document.createElement(tag);
    Object.keys(props || {}).forEach((k) => {
      if (k === 'class') e.className = props[k];
      else if (k.startsWith('aria-') || k === 'role' || k === 'title') e.setAttribute(k, props[k]);
      else e[k] = props[k];
    });
    (kids || []).forEach((c) => { if (c) e.appendChild(c); });
    return e;
  }
  const cloneBreak = (b) => ({ after: b.after, name: b.name, minutes: b.minutes });
  const cloneDay = (d) => ({ name: d.name, start: d.start, periods: d.periods, breaks: d.breaks.map(cloneBreak) });

  function mount(host, time, opts) {
    opts = opts || {};
    const Week = window.Week;
    const t0 = isObj(time) ? time : {};
    const legacy = !(Array.isArray(t0.days) && t0.days.some(isObj));
    const base = legacy ? Week.fromLegacy(t0, opts.rules) : t0;
    const st = {
      slot_minutes: intOr(t0.slot_minutes, 40),
      start: String(t0.start || '07:30'),
      cycle: base.cycle === 'rolling' ? 'rolling' : 'weeks',
      label_style: t0.label_style || (legacy && clockLabels(t0.labels) ? 'clock' : 'period'),
      rolling_start_day: intOr(t0.rolling_start_day, 1),
      days: base.days.filter(isObj).map((d, k) => ({
        name: String(d.name || `Day ${k + 1}`),
        start: String(d.start || t0.start || '07:30'),
        periods: intOr(d.periods, null),
        breaks: (Array.isArray(d.breaks) ? d.breaks : []).filter(isObj)
          .map((b) => ({ after: intOr(b.after, null), name: String(b.name == null ? 'Break' : b.name), minutes: intOr(b.minutes, null) })),
      })),
    };
    let rowEls = [];
    let shownProblem = null;

    // ---- the period length and the cycle ----
    const minutesIn = h('input', { type: 'number', min: 5, max: 240, step: 1, value: String(st.slot_minutes ?? ''), class: 'we-minutes' });
    minutesIn.addEventListener('input', () => { st.slot_minutes = toInt(minutesIn.value); changed(); });
    const cycleSel = h('select', { class: 'we-cycle' }, [
      h('option', { value: 'weeks', textContent: 'Week-based (Mon to Fri)' }),
      h('option', { value: 'rolling', textContent: 'Rolling (Day 1 to Day N)' }),
    ]);
    cycleSel.addEventListener('change', () => {
      const n = st.days.length;
      if (cycleSel.value === 'rolling') setCycle('rolling', Math.min(Week.MAX_DAYS, Math.max(2, n)));
      else setCycle('weeks', Math.min(20, Math.max(5, Math.round(n / 5) * 5)));
    });
    const weeksSel = h('select', { class: 'we-weeks' });
    weeksSel.addEventListener('change', () => setCycle('weeks', toInt(weeksSel.value)));
    const daysIn = h('input', { type: 'number', min: 2, max: Week.MAX_DAYS, step: 1, class: 'we-days' });
    daysIn.addEventListener('change', () => setCycle('rolling', toInt(daysIn.value)));
    const weeksLabel = h('label', { textContent: 'Length' }, [weeksSel]);
    const daysLabel = h('label', { textContent: 'Days in the cycle' }, [daysIn]);
    const top = h('div', { class: 'we-top' }, [
      h('label', { textContent: 'Period length (minutes)' }, [minutesIn]),
      h('label', { textContent: 'Cycle' }, [cycleSel]),
      weeksLabel, daysLabel,
    ]);

    // ---- the rows, the shortcuts, the problem and the preview ----
    const head = h('div', { class: 'we-head', 'aria-hidden': 'true' }, ['Day', 'Starts', 'Periods', 'Periods and breaks']
      .map((w) => h('span', { textContent: w })));
    const rowsBox = h('div', { class: 'we-rows' });
    const allLike = h('button', { type: 'button', class: 'btn', textContent: 'Make all days like the first' });
    allLike.addEventListener('click', () => makeAllLikeFirst());
    const tools = h('div', { class: 'we-tools' }, [allLike]);
    const problemEl = h('p', { class: 'week-problem', role: 'alert', hidden: true });
    const preview = h('ul', { class: 'week-preview' });
    const totals = h('p', { class: 'week-totals' });
    host.classList.add('week-editor');
    host.replaceChildren(top, head, rowsBox, tools, problemEl, preview, totals);

    function renderTop() {
      cycleSel.value = st.cycle;
      const n = st.days.length;
      const counts = WEEK_COUNTS.slice();
      if (st.cycle === 'weeks' && !counts.some(([c]) => c === n)) counts.unshift([n, `${plural(n, 'day')} (choose a length)`]);
      weeksSel.replaceChildren(...counts.map(([c, text]) => h('option', { value: String(c), textContent: text })));
      weeksSel.value = String(n);
      daysIn.value = String(n);
      weeksLabel.hidden = st.cycle !== 'weeks';
      daysLabel.hidden = st.cycle !== 'rolling';
    }

    function renderRows() {
      rowEls = [];
      rowsBox.replaceChildren(...st.days.map((d, k) => row(k)));
    }

    function row(k) {
      const d = st.days[k];
      const name = h('span', { class: 'we-name', textContent: d.name });
      const start = h('input', { type: 'time', class: 'we-start', value: d.start || '', 'aria-label': `${d.name} starts at` });
      start.addEventListener('input', () => { st.days[k].start = start.value; changed(); });
      const periods = h('input', { type: 'number', class: 'we-periods', min: 1, max: Week.MAX_POSITIONS, step: 1,
        value: String(d.periods ?? ''), 'aria-label': `${d.name} periods` });
      periods.addEventListener('input', () => { st.days[k].periods = toInt(periods.value); renderStrip(k); changed(); });
      const strip = h('div', { class: 'we-strip' });
      const copyBtn = h('button', { type: 'button', class: 'btn ghost we-copy-btn', textContent: 'Copy this day to…',
        'aria-label': `Copy ${d.name} to other days`, 'aria-expanded': 'false' });
      const panel = h('div', { class: 'we-copy', hidden: true });
      copyBtn.addEventListener('click', () => {
        const open = panel.hidden;
        if (open) fillCopy(k, panel, copyBtn);
        panel.hidden = !open;
        copyBtn.setAttribute('aria-expanded', String(open));
      });
      const r = h('div', { class: 'we-row' }, [name, start, periods, strip, copyBtn, panel]);
      rowEls[k] = { row: r, strip, copyBtn };
      renderStrip(k);
      return r;
    }

    function fillCopy(k, panel, copyBtn) {
      const boxes = [];
      const list = h('div', { class: 'we-copy-list' }, st.days.map((d, j) => {
        if (j === k) return null;
        const box = h('input', { type: 'checkbox' });
        boxes.push([j, box]);
        return h('label', { class: 'check' }, [box, h('span', { textContent: d.name })]);
      }));
      const apply = h('button', { type: 'button', class: 'btn primary', textContent: 'Apply' });
      apply.addEventListener('click', () => {
        copyDay(k, boxes.filter(([, b]) => b.checked).map(([j]) => j));
        if (rowEls[k]) rowEls[k].copyBtn.focus();          // the rows were rebuilt: back to this day's button
      });
      const cancel = h('button', { type: 'button', class: 'btn ghost', textContent: 'Cancel' });
      cancel.addEventListener('click', () => { panel.hidden = true; copyBtn.setAttribute('aria-expanded', 'false'); });
      panel.replaceChildren(h('span', { class: 'we-copy-say', textContent: `Copy ${st.days[k].name}'s start, periods and breaks to:` }),
        list, h('div', { class: 'we-copy-actions' }, [apply, cancel]));
    }

    // where a break shows in the strip: after its period, clamped to the day (check() names a misplaced one)
    const placeOf = (after, n) => (after == null || after < 0 ? 0 : Math.min(after, n));

    function renderStrip(k) {
      const d = st.days[k], strip = rowEls[k].strip;
      const n = Math.max(0, Math.min(Week.MAX_POSITIONS, intOr(d.periods, 0)));
      const kids = [];
      for (let p = 0; p <= n; p++) {
        const here = d.breaks.map((b, i) => [b, i]).filter(([b]) => placeOf(b.after, n) === p);
        if (p > 0 && p < n && !here.length) {
          const gap = h('button', { type: 'button', class: 'gap', textContent: '+', 'aria-label': `Add a break after P${p}` });
          gap.addEventListener('click', () => addBreak(k, p));
          kids.push(gap);
        }
        here.forEach(([b, i]) => kids.push(breakChip(k, b, i)));
        if (p < n) kids.push(h('span', { class: 'chip', textContent: `P${p + 1}` }));
      }
      strip.replaceChildren(...kids);
    }

    function breakChip(k, b, i) {
      const where = `after P${b.after ?? '?'}`;
      const name = h('input', { type: 'text', class: 'we-brk-name', value: b.name, maxLength: 24, 'aria-label': `Break name, ${where}` });
      name.addEventListener('input', () => { b.name = name.value; changed(); });
      const mins = h('input', { type: 'number', class: 'we-brk-min', min: 1, max: 240, step: 1, value: String(b.minutes ?? ''),
        'aria-label': `Break minutes, ${where}` });
      mins.addEventListener('input', () => { b.minutes = toInt(mins.value); changed(); });
      const x = h('button', { type: 'button', class: 'we-x', textContent: '×', 'aria-label': `Remove the break ${where}` });
      x.addEventListener('click', () => removeBreak(k, i));
      return h('span', { class: 'chip brk' }, [name, mins, h('span', { class: 'we-unit', textContent: 'min' }), x]);
    }

    // ---- what the buttons do ----
    function addBreak(k, after) {
      const d = st.days[k];
      if (!d) return;
      const b = Object.assign({ after }, NEW_BREAK);
      d.breaks.push(b);
      d.breaks.sort((x, y) => (x.after ?? 0) - (y.after ?? 0));
      if (rowEls[k]) {
        renderStrip(k);
        const input = rowEls[k].strip.querySelectorAll('.we-brk-name')[d.breaks.indexOf(b)];
        if (input) { input.focus(); if (input.select) input.select(); }
      }
      changed();
    }
    function removeBreak(k, i) {
      const d = st.days[k];
      if (!d || !d.breaks[i]) return;
      d.breaks.splice(i, 1);
      if (rowEls[k]) renderStrip(k);
      changed();
    }
    function copyDay(from, toList) {
      const src = st.days[from];
      if (!src) return;
      (toList || []).forEach((j) => {
        if (j !== from && st.days[j]) st.days[j] = Object.assign(cloneDay(src), { name: st.days[j].name });
      });
      renderRows();
      changed();
    }
    function makeAllLikeFirst() { copyDay(0, st.days.map((_, j) => j).filter((j) => j > 0)); }
    function setCycle(cycle, n) {
      st.cycle = cycle === 'rolling' ? 'rolling' : 'weeks';
      if (st.cycle === 'rolling') n = Math.min(Week.MAX_DAYS, Math.max(2, intOr(n, 2)));
      else if (!WEEK_COUNTS.some(([c]) => c === n)) n = 5;
      const last = st.days[st.days.length - 1] || { start: st.start, periods: 8, breaks: [] };
      st.days = Week.dayNames(st.cycle, n).map((name, k) => Object.assign(cloneDay(st.days[k] || last), { name }));
      renderTop(); renderRows(); changed();
    }

    // ---- the value, its problem and the preview ----
    function value() {
      const out = { slot_minutes: st.slot_minutes, start: st.start, cycle: st.cycle, label_style: st.label_style,
        days: st.days.map(cloneDay) };
      if (st.cycle === 'rolling') out.rolling_start_day = Math.min(Math.max(st.rolling_start_day, 1), st.days.length);
      return out;
    }
    const problem = () => Week.check(value());

    function refresh() {
      const t = value(), p = Week.check(t);
      if (p !== shownProblem) { problemEl.textContent = p || ''; shownProblem = p; }
      problemEl.hidden = !p;
      const bad = p ? st.days.findIndex((d) => p.startsWith(`${String(d.name).trim() || 'A day'}:`)) : -1;
      rowEls.forEach((r, k) => r.row.classList.toggle('bad', k === bad));
      const s = Week.shape(t);
      preview.replaceChildren(...Week.describe(s).map((line) => h('li', { textContent: line })));
      const open = s.openPerDay.reduce((a, b) => a + b, 0);
      totals.textContent = `${plural(open, 'open period')} a cycle · longest day ${plural(s.spd, 'position')}`;
    }
    function changed() {
      refresh();
      if (opts.onChange) opts.onChange(value());
    }

    renderTop(); renderRows(); refresh();
    return { value, problem, addBreak, removeBreak, copyDay, makeAllLikeFirst, setCycle };
  }

  // What saving a changed week asks and says, shared by Settings and the wizard card.
  const some = (xs) => xs.slice(0, 5).join(', ') + (xs.length > 5 ? ` and ${xs.length - 5} more` : '');

  // As the server's _same_grid: other labels or other kinds of position, so lessons may have to move.
  function gridChanged(storedTime, storedRules, time) {
    const a = Week.shape(storedTime, storedRules), b = Week.shape(time);
    return a.labels.join('\n') !== b.labels.join('\n') || a.positions.map((p) => p.kind).join() !== b.positions.map((p) => p.kind).join();
  }

  // What a changed week costs (the answer of POST /api/week/impact), as a question, or null when it costs nothing.
  function saveQuestion(imp) {
    const lose = [];
    if (imp.lessons > 0) {
      const places = [...new Set(imp.where || [])].join(', ') + (imp.lessons > (imp.where || []).length ? ' and other periods' : '');
      lose.push(`${imp.lessons} ${imp.lessons === 1 ? 'lesson sits' : 'lessons sit'} in ${places}. They will show as clashes until you rebuild or move them.`);
    }
    const other = [imp.bookings > 0 ? plural(imp.bookings, 'room booking') : '', imp.pins > 0 ? plural(imp.pins, 'pinned lesson') : ''].filter(Boolean);
    if (other.length) lose.push(`It also affects ${other.join(' and ')} where the new week has no period.`);
    if (!lose.length) return null;
    return `${lose.join(' ')} Undo history from before this change will be cleared. Save the week?`;
  }

  // What a saved week moved or could not keep (`week_changes` of PUT /api/settings), in words.
  function changesText(w) {
    if (!w) return '';
    const out = [], unplaced = w.unplaced || [], bookings = w.bookings || [], pins = w.pins || [], avail = w.avail || [];
    if (unplaced.length) out.push(`${unplaced.length === 1 ? '1 lesson' : `${unplaced.length} lessons`} could not stay on their day and ${unplaced.length === 1 ? 'is' : 'are'} unplaced: ${some(unplaced)}.`);
    if (bookings.length) out.push(`${plural(bookings.length, 'room booking')} no longer fit and ${bookings.length === 1 ? 'was' : 'were'} removed.`);
    if (pins.length) out.push(`${plural(pins.length, 'pin')} could not be kept: ${some(pins)}.`);
    if (avail.length) out.push(`${avail.length === 1 ? "1 person's" : `${avail.length} people's`} availability no longer fits the week; check it: ${some(avail)}.`);
    return out.join(' ');
  }

  // What a save says after "Saved.": what the new week moved, and that undo went with a changed grid (the
  // server clears the change log whenever the grid changes, even when nothing had to move).
  function savedText(w, gridWasChanged) {
    return [changesText(w), gridWasChanged ? 'Undo history from before this change was cleared.' : ''].filter(Boolean).join(' ');
  }

  // Whether the editor can show this time: a stored week always; an old time without `days` when its reading
  // (Week.fromLegacy) passes the check. Otherwise (a 24-hour ward, more than 20 days) Settings keeps the old
  // Time grid inputs and the wizard shows no week step.
  function editable(time, rules) {
    const t = isObj(time) ? time : {};
    if (Array.isArray(t.days) && t.days.some(isObj)) return true;
    return window.Week.check(window.Week.fromLegacy(t, rules)) === null;
  }

  window.WeekEditor = { mount, gridChanged, saveQuestion, changesText, savedText, editable };
})();
