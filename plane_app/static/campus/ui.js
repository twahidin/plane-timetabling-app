/* What the campus shows (spec 2026-10-11-campus-looks-design.md §2.3): the period bar (day, period, ‹ ›,
   play, ←/→), the legend, "All clashes", teacher search, the room and teacher cards (a bottom sheet when
   the view is narrow) and the hint. Plain DOM over the canvas; all text is set with textContent.
   createUI(host, h) → ui. `h` is main.js's: period(t), info.room(id), info.teacher(id), search(q),
   allClashes(on), select(eventId), focus(roomId). */
import { labelParts } from 'campus/data';

const el = (tag, cls, text) => { const e = document.createElement(tag); if (cls) e.className = cls; if (text != null) e.textContent = text; return e; };


export function createUI(host, h) {
  let doc = null, t = 0, spd = 1, days = 1, playing = 0, palette = {}, dismissed = null, open = null;

  const bar = el('div', 'campus-bar');
  const prev = el('button', 'btn icon', '‹'); prev.type = 'button'; prev.setAttribute('aria-label', 'Previous period');
  const next = el('button', 'btn icon', '›'); next.type = 'button'; next.setAttribute('aria-label', 'Next period');
  const daySel = el('select'); daySel.setAttribute('aria-label', 'Day');
  const perSel = el('select'); perSel.setAttribute('aria-label', 'Period');
  const play = el('button', 'btn', '▶ Play'); play.type = 'button';
  const all = el('button', 'btn', 'All clashes'); all.type = 'button'; all.setAttribute('aria-pressed', 'false');
  const find = el('div', 'campus-find');
  const search = el('input'); search.type = 'search'; search.placeholder = 'Teacher or room'; search.setAttribute('aria-label', 'Find a teacher or room');
  const results = el('ul', 'campus-results'); results.hidden = true;
  find.append(search, results);
  bar.append(prev, daySel, perSel, next, play, all, find);
  const legend = el('div', 'campus-legend');
  const hint = el('div', 'campus-hint'); hint.hidden = true;
  const hintText = el('span'); const hintClose = el('button', 'btn ghost icon', '×'); hintClose.type = 'button'; hintClose.setAttribute('aria-label', 'Dismiss');
  hint.append(hintText, hintClose);
  const card = el('aside', 'campus-card'); card.hidden = true;
  host.append(legend, hint, card, bar);

  function go(nt) {
    const total = spd * days;
    if (!total) return;
    t = ((nt % total) + total) % total;
    h.period(t);
  }
  prev.addEventListener('click', () => go(t - 1));
  next.addEventListener('click', () => go(t + 1));
  daySel.addEventListener('change', () => go(Number(daySel.value) * spd + (t % spd)));
  perSel.addEventListener('change', () => go(Math.floor(t / spd) * spd + Number(perSel.value)));
  play.addEventListener('click', () => {
    if (playing) { clearInterval(playing); playing = 0; play.textContent = '▶ Play'; return; }
    playing = setInterval(() => { if (!document.hidden) go(t + 1); }, 2500); play.textContent = '❚❚ Pause';
  });
  all.addEventListener('click', () => {
    const on = all.getAttribute('aria-pressed') !== 'true';
    all.setAttribute('aria-pressed', String(on)); all.classList.toggle('on', on); h.allClashes(on);
  });
  host.addEventListener('keydown', (e) => {
    if (e.key === 'Escape' && !card.hidden) { hideCard(); return; }
    if (e.altKey || e.ctrlKey || e.metaKey || e.target.closest('input,select,textarea')) return;
    if (e.key === 'ArrowLeft') { e.preventDefault(); go(t - 1); }
    if (e.key === 'ArrowRight') { e.preventDefault(); go(t + 1); }
  });
  hintClose.addEventListener('click', () => { dismissed = hintText.textContent; hint.hidden = true; });
  search.addEventListener('input', () => {
    results.replaceChildren();
    const q = search.value.trim().toLowerCase();
    const found = q ? h.search(q).slice(0, 6) : [];
    for (const p of found) {       // teachers and rooms: rooms get a keyboard path too
      const li = el('li'); const b = el('button', 'campus-result', p.kind === 'room' ? p.name + ' · room' : p.name); b.type = 'button';
      b.addEventListener('click', () => { results.hidden = true; search.value = p.name; if (p.kind === 'room') showRoom(p.id); else showTeacher(p.id); });
      li.append(b); results.append(li);
    }
    results.hidden = !found.length;
  });

  function rows(pairs) {
    const dl = el('div', 'rows');
    for (const [k, v] of pairs) { if (v == null || v === '') continue; const r = el('div'); r.append(el('span', null, k), el('span', null, v)); dl.append(r); }
    return dl;
  }
  function openCard(kind, title, sub) {
    card.replaceChildren();
    const x = el('button', 'x', '×'); x.type = 'button'; x.setAttribute('aria-label', 'Close'); x.addEventListener('click', hideCard);
    card.append(x, el('div', 'kind', kind), el('h3', null, title), el('div', 'sub', sub));
    card.hidden = false;
    relayout();
  }
  function relayout() {           // a bottom sheet above the period bar when the view is narrow
    const narrow = host.clientWidth < 600;
    card.classList.toggle('sheet', narrow);
    card.style.bottom = narrow ? bar.offsetHeight + 16 + 'px' : '';
  }
  const line = (...parts) => parts.filter(Boolean).join(' · ');
  function nowLine(colour, text, eventId) {
    const b = el('button', 'now'); b.type = 'button';
    const dot = el('span', 'dot'); dot.style.background = colour || 'var(--muted)';
    b.append(dot, el('span', null, text));
    if (eventId) { b.title = 'Show this lesson in the timetable'; b.addEventListener('click', () => h.select(eventId)); } else b.disabled = true;
    return b;
  }
  function showRoom(id, quiet) {
    const i = h.info.room(id); if (!i) return false;
    open = { kind: 'room', id };
    const where = [i.room.block ? 'Block ' + i.room.block : null, i.room.level ? 'Level ' + i.room.level : null, i.room.cap ? i.room.cap + ' seats' : null].filter(Boolean).join(' · ');
    openCard('Room', i.room.name, where || 'Room');
    card.append(i.now ? nowLine(palette[i.now.colour], line(i.now.subject, i.now.group, i.nowTeachers), i.now.event)
      : nowLine(null, 'Free this period', null));
    for (const m of i.clashes) card.append(el('div', 'campus-clash', '⚠ ' + m));
    const bar = el('div', 'bar'); const fill = el('i'); fill.style.width = Math.min(100, (i.used / Math.max(i.total, 1)) * 100) + '%'; bar.append(fill);
    card.append(el('div', 'muted', `Used ${i.used} of ${i.total} periods a week`), bar,
      rows([['Home of', i.room.home], ['Next', i.next ? line(i.next.subject, i.next.label) : 'Nothing later this week']]));
    if (!quiet) h.focus(id);
    return true;
  }
  function showTeacher(id, quiet) {
    const i = h.info.teacher(id); if (!i) return false;
    open = { kind: 'teacher', id };
    openCard('Teacher' + (i.person.dept ? ' · ' + i.person.dept : ''), i.person.name, i.where ? 'Teaching in ' + i.where.roomName : 'Free this period');
    if (i.where) card.append(nowLine(palette[i.where.lesson.colour], line(i.where.lesson.subject, i.where.lesson.group, i.where.roomName), i.where.lesson.event));
    if (i.load && i.load.max == null) card.append(el('div', 'muted', `Load ${i.load.load} periods`));
    else if (i.load) {
      const bar = el('div', 'bar'); const fill = el('i'); fill.style.width = Math.min(100, (i.load.load / Math.max(i.load.max, 1)) * 100) + '%'; bar.append(fill);
      card.append(el('div', 'muted', `Load ${i.load.load} of ${i.load.max} periods`), bar);
    }
    card.append(rows([['Next', i.next ? line(i.next.subject, i.next.roomName, i.next.label) : 'Nothing later today'], ['Free today', i.free.join(', ') || 'None']]));
    if (i.where && !quiet) h.focus(i.where.room);
    return true;
  }
  function hideCard() { open = null; card.hidden = true; card.replaceChildren(); }

  function fillSelects() {
    daySel.replaceChildren(); perSel.replaceChildren();
    const labels = (doc && doc.time && doc.time.labels) || [];
    for (let d = 0; d < days; d++) daySel.append(new Option(labelParts(labels, d * spd, spd, days).day, String(d)));
    for (let p = 0; p < spd; p++) perSel.append(new Option(labelParts(labels, p, spd, days).period, String(p)));
  }

  return {
    setDoc(next, colours) {
      doc = next; palette = colours || {};
      spd = Math.max(1, (doc.time && doc.time.slots_per_day) || 1);
      days = Math.max(1, (doc.time && doc.time.days) || 1);
      fillSelects();
      legend.replaceChildren();
      for (const [key, name] of Object.entries(doc.legend || {})) {
        const item = el('span', 'campus-key'); const dot = el('span', 'dot'); dot.style.background = palette[key] || 'var(--muted)';
        item.append(dot, el('span', null, name)); legend.append(item);
      }
      this.setHint(doc.hint);
      const none = !(doc.time && doc.time.labels && doc.time.labels.length);
      [prev, next, daySel, perSel, play].forEach((c) => { c.disabled = none; });
      all.hidden = !(doc.clashes && doc.clashes.length);
      this.refresh();
    },
    refresh() {                                   // an open card follows the period and the data; gone → closed
      const o = open;
      if (o && !(o.kind === 'room' ? showRoom(o.id, true) : showTeacher(o.id, true))) hideCard();
    },
    setPeriod(nt) {
      t = nt;
      daySel.value = String(Math.floor(t / spd)); perSel.value = String(t % spd);
    },
    showRoom, showTeacher, hideCard,
    setLegend() {},
    setHint(text) { hintText.textContent = text || ''; hint.hidden = !text || text === dismissed; },
    relayout() { if (!card.hidden) relayout(); },
    stop() { if (playing) { clearInterval(playing); playing = 0; play.textContent = '▶ Play'; } },
  };
}
