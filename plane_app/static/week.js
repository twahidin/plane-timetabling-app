/* The shape of the week (spec 2026-10-15-week-shape-design.md §3): the same reading as plane_app/week.py,
   in the browser. Positions, closed and rest lists, labels and day names for a settings `time`.
   Plain functions on window.Week; tests/test_week_js.py holds it to week.py case by case. */
(function () {
  const MAX_DAYS = 20, MAX_POSITIONS = 30, WEEKDAYS = ['Mon', 'Tue', 'Wed', 'Thu', 'Fri'], WEEK_DAY_COUNTS = [1, 5, 10, 15, 20];
  const int = (v, d = null) => (Number.isInteger(v) ? v : d);
  const isObj = (v) => v !== null && typeof v === 'object' && !Array.isArray(v);   // Python's dict
  // Python's `x or []`: an empty object is as falsy as an empty list there.
  const orList = (v) => (!v || (isObj(v) && !Object.keys(v).length) ? [] : v);
  function minutes(hm) {
    const m = /^(\d{1,2}):(\d{2})$/.exec(String(hm || ''));
    if (!m) return null;
    const h = Number(m[1]), mi = Number(m[2]);
    return h < 24 && mi < 60 ? h * 60 + mi : null;
  }
  function clock(total) {
    total = ((total % 1440) + 1440) % 1440;
    return String(Math.floor(total / 60)).padStart(2, '0') + ':' + String(total % 60).padStart(2, '0');
  }
  function dayNames(cycle, n) {
    if (n === 1) return ['Day'];
    if (cycle === 'rolling' || n % 5) return Array.from({ length: n }, (_, k) => `Day ${k + 1}`);
    const weeks = n / 5;
    if (weeks === 1) return WEEKDAYS.slice();
    if (weeks === 2) return ['Odd', 'Even'].flatMap((w) => WEEKDAYS.map((d) => `${w} ${d}`));
    return Array.from({ length: weeks }, (_, w) => WEEKDAYS.map((d) => `W${w + 1} ${d}`)).flat();
  }
  function fromLegacy(time, rules) {
    const labels = orList(time.labels).map(String);
    const spd = int(time.slots_per_day) || labels.length || 1;
    const n = labels.length ? Math.max(1, Math.floor(labels.length / spd)) : 1;
    const rest = [...new Set(orList((rules || {}).mandatory_rest).filter((o) => Number.isInteger(o) && o >= 0 && o < spd))].sort((a, b) => a - b);
    const mins = int(time.slot_minutes, 40) || 40;
    const breaks = rest.map((o, i) => ({ after: o - i, name: 'Break', minutes: mins }));
    const names = [];
    for (let k = 0; k < n; k++) {
      const first = k * spd < labels.length ? labels[k * spd] : '';
      names.push(first.includes(' ') ? first.slice(0, first.lastIndexOf(' ')) : (n > 1 ? `Day ${k + 1}` : 'Day'));
    }
    const start = String(time.start || '07:30');
    const out = Object.assign({}, time); delete out.days;
    let cycle = time.cycle || 'weeks';
    if (cycle === 'weeks' && !WEEK_DAY_COUNTS.includes(n)) cycle = 'rolling';   // a 6- or 8-day cycle is not whole weeks
    return Object.assign(out, { cycle,
      days: names.map((name) => ({ name, start, periods: spd - rest.length, breaks: breaks.map((b) => Object.assign({}, b)) })) });
  }
  function items(d) {
    const periods = int(d.periods, 0) || 0;
    const raw = orList(d.breaks);
    const breaks = (Array.isArray(raw) ? raw : []).filter(isObj).slice().sort((a, b) => int(a.after, 0) - int(b.after, 0));
    const out = []; let bi = 0;
    for (let p = 0; p <= periods; p++) {
      while (bi < breaks.length && int(breaks[bi].after, 0) === p) {
        out.push({ kind: 'break', name: String(breaks[bi].name || 'Break'), minutes: int(breaks[bi].minutes, 0) || 0 }); bi++;
      }
      if (p < periods) out.push({ kind: 'period', period: p + 1 });
    }
    return out;
  }
  function shape(time, rules) {
    time = Object.assign({}, time || {});
    const legacy = !(Array.isArray(time.days) && time.days.some(isObj));
    const week = legacy ? fromLegacy(time, rules) : time;
    const mins = int(time.slot_minutes, 40) || 40;
    const days = week.days.filter(isObj);
    const rows = days.map(items);
    const spd = rows.reduce((m, r) => Math.max(m, r.length), 0);
    const stored = orList(time.labels).map(String), style = time.label_style || 'period';
    const names = days.map((d, k) => String(d.name || `Day ${k + 1}`)), one = days.length === 1;
    const positions = [], closed = [], rests = [], openPerDay = [];
    days.forEach((d, k) => {
      let cur = minutes(d.start) != null ? minutes(d.start) : (minutes(time.start) || 450);
      const row = rows[k];
      for (let i = 0; i < spd; i++) {
        const t = k * spd + i, it = i < row.length ? row[i] : { kind: 'closed' };
        let start = null, end = null, word;
        if (it.kind === 'period') { start = clock(cur); end = clock(cur + mins); cur += mins; word = style === 'clock' ? start : `P${it.period}`; }
        else if (it.kind === 'break') { start = clock(cur); end = clock(cur + it.minutes); cur += it.minutes; word = it.name; rests.push(t); }
        else { word = '—'; closed.push(t); }
        const label = legacy && t < stored.length ? stored[t] : (one ? word : `${names[k]} ${word}`);
        positions.push({ t, day: k, index: i, kind: it.kind, period: it.period == null ? null : it.period,
          name: it.name == null ? null : it.name, label, start, end });
      }
      openPerDay.push(row.filter((it) => it.kind === 'period').length);
    });
    return { legacy, cycle: week.cycle || 'weeks', days: names, nDays: days.length, spd, positions, closed, rests, openPerDay,
      labels: positions.map((p) => p.label) };
  }
  function check(time) {
    const mins = int(time.slot_minutes);
    if (mins === null || mins < 5 || mins > 240) return 'Give a period length between 5 and 240 minutes.';
    const cycle = time.cycle || 'weeks';
    if (cycle !== 'weeks' && cycle !== 'rolling') return 'The cycle is week-based or rolling.';
    const days = time.days;
    if (!Array.isArray(days) || !days.length) return 'Give at least one day.';
    if (days.length > MAX_DAYS) return `A cycle has at most ${MAX_DAYS} days; this one has ${days.length}.`;
    if (cycle === 'rolling' && !(days.length >= 2 && days.length <= MAX_DAYS)) return `A rolling cycle has 2 to ${MAX_DAYS} days.`;
    if (cycle === 'weeks' && !WEEK_DAY_COUNTS.includes(days.length)) return 'A week-based cycle has 1, 5, 10, 15 or 20 days (one to four weeks).';
    for (const d of days) {
      if (!isObj(d)) return 'Each day is a row with a name, a start and periods.';
      const name = String(d.name || '').trim() || 'A day';
      const periods = int(d.periods);
      if (periods === null || periods < 1) return `${name}: give at least one period.`;
      const start = minutes(d.start);
      if (start === null) return `${name}: the start time must be HH:MM.`;
      const breaks = orList(d.breaks);
      if (!Array.isArray(breaks)) return `${name}: breaks are a list.`;
      if (periods + breaks.length > MAX_POSITIONS) return `${name}: ${MAX_POSITIONS} positions a day at most (periods and breaks); this day has ${periods + breaks.length}.`;
      let total = periods * mins;
      for (const b of breaks) {
        if (!isObj(b)) return `${name}: each break needs a position, a name and minutes.`;
        const after = int(b.after);
        if (after === null || after < 1 || after > periods - 1) return `${name}: a break must sit between two periods (after P1 to after P${periods - 1}).`;
        const bname = String(b.name || '').trim(), bm = int(b.minutes);
        if (!bname || bm === null || bm < 1 || bm > 240) return `${name}: ${bname || 'a break'} needs a name and 1 to 240 minutes.`;
        total += bm;
      }
      if (start + total >= 24 * 60) return `${name}: the day runs past midnight.`;
    }
    return null;
  }
  function describe(s) {
    return s.days.map((name, k) => {
      const ps = s.positions.filter((p) => p.day === k && p.kind !== 'closed');
      if (!ps.length) return `${name} · no periods`;
      const periods = ps.filter((p) => p.kind === 'period').length;
      let line = `${name} · ${ps[0].start}–${ps[ps.length - 1].end} · ${periods} period${periods !== 1 ? 's' : ''}`;
      let last = 0;
      for (const p of ps) { if (p.kind === 'period') last = p.period; else line += ` · ${p.name} after P${last}`; }
      return line;
    });
  }
  // The breaks of the week in a few short lines: one for each distinct pattern of breaks, with the days it
  // holds on when not every day has it ("Recess after P3 (Mon–Thu)"). Nothing when no day has a break.
  function breakLines(s) {
    const pattern = s.days.map((_, k) => {
      let last = 0; const parts = [];
      s.positions.filter((p) => p.day === k).forEach((p) => {
        if (p.kind === 'period') last = p.period; else if (p.kind === 'break') parts.push(`${p.name} after P${last}`);
      });
      return parts.join(', ');
    });
    const groups = [];
    pattern.forEach((text, k) => {
      if (!text) return;
      const g = groups.find((x) => x.text === text);
      if (g) g.days.push(k); else groups.push({ text, days: [k] });
    });
    const every = groups.length === 1 && groups[0].days.length === s.nDays;
    return groups.map((g) => {
      if (every) return g.text;
      const runs = [];
      g.days.forEach((k) => { const r = runs[runs.length - 1]; if (r && r[1] === k - 1) r[1] = k; else runs.push([k, k]); });
      const where = runs.map(([a, b]) => (a === b ? s.days[a] : b === a + 1 ? `${s.days[a]}, ${s.days[b]}` : `${s.days[a]}–${s.days[b]}`)).join(', ');
      return `${g.text} (${where})`;
    });
  }
  window.Week = { shape, check, dayNames, describe, breakLines, minutes, clock, fromLegacy, MAX_DAYS, MAX_POSITIONS };
})();
