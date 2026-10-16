/* The campus's pure logic (spec 2026-10-11-campus-looks-design.md §2.3): which lesson is in which room
   at slot t, who is free, where a teacher is, clashes at t, and the slot to open on. No DOM, no Three. */
const ends = (l) => l.t0 + (l.dur || 1);
const covers = (l, t) => l.t0 <= t && t < ends(l);

export const slot = (day, period, spd) => day * spd + period;
export const split = (t, spd) => ({ day: Math.floor(t / spd), period: t % spd });

export function occupancy(lessons, t) {
  const out = {};
  for (const [room, list] of Object.entries(lessons || {})) {
    const l = (list || []).find((x) => covers(x, t));
    if (l) out[room] = l;
  }
  return out;
}

export function whereIs(personId, lessons, t) {
  for (const [room, list] of Object.entries(lessons || {})) {
    const l = (list || []).find((x) => covers(x, t) && (x.teachers || []).includes(personId));
    if (l) return { room, lesson: l };
  }
  return null;
}

export function freeTeachers(people, lessons, t) {
  const busy = new Set();
  for (const list of Object.values(lessons || {})) for (const l of list || []) if (covers(l, t)) (l.teachers || []).forEach((p) => busy.add(p));
  return (people || []).filter((p) => !busy.has(p.id));
}

export const clashesAt = (clashes, t) => (clashes || []).filter((c) => (c.slots || []).includes(t));

export function clashCounts(clashes) {
  const out = {};
  for (const c of clashes || []) for (const r of c.rooms || []) out[r] = (out[r] || 0) + 1;
  return out;
}

export const nextLesson = (list, t) => (list || []).find((l) => l.t0 > t) || null;
export const usedPeriods = (list) => (list || []).reduce((n, l) => n + (l.dur || 1), 0);

/* The week's own positions (spec 2026-10-15-week-shape §1): `time.positions` is [{t, day, index, kind:
   "period" | "break" | "closed", period, label, start, end}] when the week has its own days; an old document
   or a legacy week has none, and every slot is a period as before. */
const positionsOf = (time) => (time && Array.isArray(time.positions) && time.positions.length ? time.positions : null);
const isPeriod = (positions, t) => !positions || !!(positions[t] && positions[t].kind === 'period');

/* The period index in `day` (0 … spd-1) that `personId` is free in; with positions, only the day's periods. */
export function freePeriods(personId, lessons, day, spd, positions) {
  const ps = Array.isArray(positions) && positions.length ? positions : null, out = [];
  for (let p = 0; p < spd; p++) if (isPeriod(ps, slot(day, p, spd)) && !whereIs(personId, lessons, slot(day, p, spd))) out.push(p);
  return out;
}

/* The next slot after `t` in direction `dir` (+1 or -1) that is a period, wrapping round the cycle; t itself
   when no other is (every slot is a period without positions). */
export function stepPeriod(time, t, dir) {
  const total = Math.max(0, ((time && time.slots_per_day) || 0) * ((time && time.days) || 0));
  if (!total) return 0;
  const ps = positionsOf(time), d = dir < 0 ? -1 : 1;
  let n = t;
  for (let k = 0; k < total; k++) {
    n = (((n + d) % total) + total) % total;
    if (isPeriod(ps, n)) return n;
  }
  return isPeriod(ps, t) ? t : 0;
}

/* The slots of `day`'s periods in order (every slot of the day without positions). */
export function dayPeriods(time, day) {
  const spd = (time && time.slots_per_day) || 0, ps = positionsOf(time), out = [];
  for (let p = 0; p < spd; p++) if (isPeriod(ps, slot(day, p, spd))) out.push(slot(day, p, spd));
  return out;
}

/* The periods a cycle: the open ones with positions, else every label. */
export function openCount(time) {
  const ps = positionsOf(time);
  return ps ? ps.filter((p) => p.kind === 'period').length : ((time && time.labels) || []).length;
}

/* The slot "now": weekday Mon–Fri maps to the cycle's day (Mon = 0, wrapping in a longer cycle is not
   known here, so days beyond the week start at 0), the period from minutes since `start`. 0 when outside
   the school day, at a weekend, or when the time grid is unknown. With positions: the period whose own
   clock holds `now` on that day (a rolling cycle's day is not known here: day 0), 0 in a break or after
   the day's last period. */
export function currentSlot(time, now) {
  const spd = time && time.slots_per_day, days = time && time.days, mins = time && time.slot_minutes;
  const ps = positionsOf(time);
  if (ps) {
    if (!days || !(now instanceof Date) || isNaN(now)) return 0;
    const wd = (now.getDay() + 6) % 7;
    if (wd > 4) return 0;
    const day = time.cycle === 'rolling' ? 0 : wd % days, at = now.getHours() * 60 + now.getMinutes();
    const hm = (x) => { const m = /^(\d{1,2}):(\d{2})$/.exec(x || ''); return m ? Number(m[1]) * 60 + Number(m[2]) : null; };
    const p = ps.find((x) => x && x.day === day && x.kind === 'period' && hm(x.start) !== null && hm(x.start) <= at && at < hm(x.end));
    return p ? p.t : 0;
  }
  const m = /^(\d{1,2}):(\d{2})$/.exec((time && time.start) || '');
  if (!spd || !days || !mins || !m || !(now instanceof Date) || isNaN(now)) return 0;
  const wd = (now.getDay() + 6) % 7;                    // Mon = 0
  if (wd > 4) return 0;
  const since = now.getHours() * 60 + now.getMinutes() - (Number(m[1]) * 60 + Number(m[2]));
  if (since < 0 || since >= spd * mins) return 0;
  return slot(wd % days, Math.floor(since / mins), spd);
}

/* A slot's day and period names from the time labels: "Mon P3" → {day: "Mon", period: "P3"} when the
   first word changes from day to day; otherwise "Day n" and the whole label ("Period 3"). With positions
   and the days' names: the position's day by name and its label without that name ("Mon Recess" → Recess). */
export function labelParts(labels, t, spd, days, positions, names) {
  const pos = Array.isArray(positions) ? positions[t] : null;
  if (pos && Number.isInteger(pos.day)) {
    const name = (Array.isArray(names) && names[pos.day] != null ? String(names[pos.day]) : '') || 'Day ' + (pos.day + 1);
    const label = String(pos.label || (labels || [])[t] || '');
    return { day: name, period: (label.startsWith(name + ' ') ? label.slice(name.length + 1) : label) || 'P' + ((t % spd) + 1) };
  }
  const l = (labels || [])[t] || '', first = (x) => String(x || '').trim().split(/\s+/)[0];
  const byDay = days > 1 && first(labels[0]) !== first(labels[spd]);
  const parts = String(l).trim().split(/\s+/);
  if (byDay && parts.length > 1) return { day: parts[0], period: parts.slice(1).join(' ') };
  return { day: 'Day ' + (Math.floor(t / spd) + 1), period: l || 'P' + ((t % spd) + 1) };
}
