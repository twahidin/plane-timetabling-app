/* What the campus shows (spec 2026-10-11-campus-looks-design.md §2.3): the period bar (day, period, ‹ ›,
   play, ←/→), the legend, "All clashes", teacher search, the room and teacher cards (a bottom sheet when
   the view is narrow) and the hint. Plain DOM over the canvas; all text is set with textContent.
   createUI(host, h) → ui. `h` is main.js's: period(t), info.room(id), info.teacher(id), search(q),
   allClashes(on), select(eventId), focus(roomId), replay(), present(on), pause(), and onClose(): the user
   closed the card (× or Escape), so a redraw must not open it again. hideCard() closes it quietly.
   The journey (spec §3.3): "▶ Build" plays the build; "Present" (in full screen) is presentation mode —
   host gets .presenting, the bar, legend, hint and card hide and the counters (.campus-kpis) stay.
   The setup tabs (spec 2026-10-13-setup-campus §1): setMode(mode, {legend, counters, note}) — outside
   'build' the period bar (and with it play, ▶ Build, Present, All clashes and the search) is hidden, the
   legend and counters are the scene's, and its note sits above the bottom edge. showSetup(card) opens a
   text-only card with a "Show in the table" link. */
import { labelParts } from 'campus/data';

const el = (tag, cls, text) => { const e = document.createElement(tag); if (cls) e.className = cls; if (text != null) e.textContent = text; return e; };


export function createUI(host, h) {
  let doc = null, t = 0, spd = 1, days = 1, playing = 0, palette = {}, dismissed = null, open = null, mode = 'build';

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
  const build = el('button', 'btn', '▶ Build'); build.type = 'button'; build.title = 'Play the build from the files to the checked timetable';
  const present = el('button', 'btn campus-present', 'Present'); present.type = 'button'; present.title = 'Presentation mode: Space pauses, Esc leaves';
  bar.append(prev, daySel, perSel, next, play, all, build, present, find);
  const kpis = el('div', 'campus-kpis'); kpis.setAttribute('aria-live', 'polite');
  const KPI = [['lessons', 'Lessons placed'], ['teachers', 'Teachers deployed'], ['rooms', 'Rooms in use now'], ['clashes', 'Clashes']];
  const kpi = {};
  for (const [k, name] of KPI) {
    const c = el('div', 'campus-kpi'); const v = el('b', null, '–'); c.append(v, el('span', null, name)); kpis.append(c);
    kpi[k] = { c, v, n: null, of: null, raf: 0 };
  }
  kpis.hidden = true;
  const legend = el('div', 'campus-legend');
  const hint = el('div', 'campus-hint'); hint.hidden = true;
  const hintText = el('span'); const hintClose = el('button', 'btn ghost icon', '×'); hintClose.type = 'button'; hintClose.setAttribute('aria-label', 'Dismiss');
  hint.append(hintText, hintClose);
  const card = el('aside', 'campus-card'); card.hidden = true;
  const skpis = el('div', 'campus-kpis'); skpis.setAttribute('aria-live', 'polite'); skpis.hidden = true;   // a setup scene's counters
  const sceneNote = el('div', 'campus-hint campus-scene-note'); sceneNote.setAttribute('role', 'status'); sceneNote.hidden = true;
  host.append(legend, kpis, skpis, hint, sceneNote, card, bar);
  const shownKpis = () => (mode === 'build' ? kpis : skpis);

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
  build.addEventListener('click', () => h.replay());
  present.addEventListener('click', () => h.present(!host.classList.contains('presenting')));
  function presentKeys(e) {                       // while presenting, from anywhere on the page
    if (e.repeat || e.ctrlKey || e.metaKey || e.altKey || document.querySelector('dialog[open]')) return;
    if (e.key === 'Escape') { e.preventDefault(); h.present(false); }
    else if (e.key === ' ' && !e.target.closest('input,select,textarea,a,summary,[contenteditable],[role=button]')
             && (!e.target.closest('button') || e.target === present)) { e.preventDefault(); h.pause(); }
  }
  function count(k, n, of, instant) {             // counts up from the last value over 0.6 s
    const x = kpi[k]; cancelAnimationFrame(x.raf);
    const text = (v) => (of == null ? String(v) : `${v} / ${of}`);
    const from = x.n == null || instant ? n : x.cur;  // from what is on screen, even mid-count
    x.n = n; x.of = of;
    if (from === n) { x.cur = n; x.v.textContent = text(n); return; }
    const t0 = performance.now();
    const step = (now) => {
      const k2 = Math.min(1, (now - t0) / 600);
      x.cur = Math.round(from + (n - from) * (1 - Math.pow(1 - k2, 3)));
      x.v.textContent = text(x.cur);
      if (k2 < 1) x.raf = requestAnimationFrame(step);
    };
    x.raf = requestAnimationFrame(step);
  }
  all.addEventListener('click', () => {
    const on = all.getAttribute('aria-pressed') !== 'true';
    all.setAttribute('aria-pressed', String(on)); all.classList.toggle('on', on); h.allClashes(on);
  });
  host.addEventListener('keydown', (e) => {
    if (e.key === 'Escape' && !card.hidden) { closeCard(); return; }
    if (mode !== 'build' || e.altKey || e.ctrlKey || e.metaKey || e.target.closest('input,select,textarea')) return;
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
    const x = el('button', 'x', '×'); x.type = 'button'; x.setAttribute('aria-label', 'Close'); x.addEventListener('click', closeCard);
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
  function closeCard() { const was = !card.hidden; hideCard(); if (was && h.onClose) h.onClose(); }   // the user's close
  /* a setup card: {kind, title, sub, rows: [[name, value]], lines: [text], link: {text, go}} — all text;
     go() answers (or resolves) false when the row is not in the table, and the link says so */
  function showSetup(c) {
    if (!c) { hideCard(); return false; }
    open = { kind: 'setup' };
    openCard(c.kind || '', c.title || '', c.sub || '');
    if (c.rows && c.rows.length) card.append(rows(c.rows));
    if (c.lines && c.lines.length) {
      const ul = el('ul', 'campus-lines');
      for (const x of c.lines) ul.append(el('li', null, x));
      card.append(ul);
    }
    if (c.link) {
      const b = el('button', 'btn campus-show', c.link.text || 'Show in the table'); b.type = 'button';
      b.addEventListener('click', () => {
        Promise.resolve().then(() => c.link.go()).then((ok) => {
          if (ok === false) { b.textContent = 'Not in the table yet'; b.disabled = true; }
        }).catch(() => { b.textContent = 'Not in the table yet'; b.disabled = true; });
      });
      card.append(b);
    }
    return true;
  }
  function drawLegend(items) {                    // [[name, colour]]
    legend.replaceChildren();
    for (const [name, colour] of items) {
      if (colour === null) { legend.append(el('span', 'campus-key more', String(name))); continue; }   // "and N more": no dot
      const item = el('span', 'campus-key'); const dot = el('span', 'dot'); dot.style.background = colour || 'var(--muted)';
      item.append(dot, el('span', null, String(name))); legend.append(item);
    }
  }
  const buildLegend = () => Object.entries((doc && doc.legend) || {}).map(([key, name]) => [name, palette[key]]);

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
      if (mode === 'build') { drawLegend(buildLegend()); this.setHint(doc.hint); }
      const none = !(doc.time && doc.time.labels && doc.time.labels.length);
      [prev, next, daySel, perSel, play].forEach((c) => { c.disabled = none; });
      all.hidden = !(doc.clashes && doc.clashes.length);
      this.refresh(); this.relayout();
    },
    refresh() {                                   // an open card follows the period and the data; gone → closed
      const o = open;
      if (o && o.kind !== 'setup' && !(o.kind === 'room' ? showRoom(o.id, true) : showTeacher(o.id, true))) hideCard();
    },
    setPeriod(nt) {
      t = nt;
      daySel.value = String(Math.floor(t / spd)); perSel.value = String(t % spd);
    },
    showRoom, showTeacher, hideCard, closeCard, showSetup,
    setLegend() {},
    setHint(text) { hintText.textContent = text || ''; hint.hidden = mode !== 'build' || !text || text === dismissed; },
    /* 'build' (the period bar, Build's legend, counters and hint) or a setup tab's scene */
    setMode(next, out) {
      const was = mode;
      mode = next && next !== 'build' ? String(next) : 'build';
      const setup = mode !== 'build';
      host.classList.toggle('setup-mode', setup);
      bar.hidden = setup;
      results.hidden = true;
      if (was !== mode && open) hideCard();
      if (setup) {
        this.stop();
        const o = out || {};
        hint.hidden = true; kpis.hidden = true;
        drawLegend(o.legend || []);
        skpis.replaceChildren();
        for (const [value, name, bad] of o.counters || []) {
          const c = el('div', 'campus-kpi' + (bad ? ' bad' : '')); c.append(el('b', null, String(value)), el('span', null, String(name))); skpis.append(c);
        }
        skpis.hidden = !skpis.childElementCount;
        sceneNote.textContent = o.note || ''; sceneNote.hidden = !o.note;
      } else {
        skpis.hidden = true; sceneNote.hidden = true;
        kpis.hidden = !kpis.dataset.placed;
        drawLegend(buildLegend());
        this.setHint(doc && doc.hint);
      }
      this.relayout();
    },
    relayout() {                                  // counters: top right, or one row under the legend when narrow
      const K = shownKpis(), other = K === kpis ? skpis : kpis;
      other.classList.remove('row'); other.style.top = '';
      let row = host.clientWidth < 700;
      if (!row && !K.hidden) {                      // wide, but a long legend would sit under them: the row too
        K.classList.remove('row'); K.style.top = '';
        const a = legend.getBoundingClientRect(), b = K.getBoundingClientRect();
        row = a.width > 0 && a.right > b.left - 8 && a.top < b.bottom;
      }
      K.classList.toggle('row', row);
      hint.style.bottom = bar.offsetHeight + 24 + 'px';   // the hint sits just above the period bar, clear of the legend and counters
      K.style.top = row ? legend.offsetTop + legend.offsetHeight + 8 + 'px' : '';
      if (!card.hidden) relayout();
    },
    stop() { if (playing) { clearInterval(playing); playing = 0; play.textContent = '▶ Play'; } },
    setMotion(level) { build.hidden = level === 'off'; },
    setCounters(c, instant) {                     // {lessons: [n, total], teachers: [n, total], rooms: [n, total], clashes: n}
      if (!c) return;
      kpis.hidden = mode !== 'build';               // a setup scene shows its own; Build's come back with Build
      if (!kpis.dataset.placed) { kpis.dataset.placed = '1'; this.relayout(); }
      for (const k of ['lessons', 'teachers', 'rooms']) {
        if (c[k]) count(k, c[k][0], c[k][1], instant);
        else { cancelAnimationFrame(kpi[k].raf); kpi[k].n = kpi[k].cur = null; kpi[k].v.textContent = '–'; }   // nothing known any more
      }
      if (c.clashes != null) { count('clashes', c.clashes, null, instant); kpi.clashes.c.classList.toggle('bad', c.clashes > 0); }
    },
    setPresenting(on) {
      host.classList.toggle('presenting', !!on);
      present.textContent = on ? 'Leave' : 'Present';
      if (on) { this.stop(); hideCard(); present.blur(); document.addEventListener('keydown', presentKeys); }   // Space pauses, never clicks Leave
      else document.removeEventListener('keydown', presentKeys);
    },
    dispose() { this.setPresenting(false); for (const k in kpi) cancelAnimationFrame(kpi[k].raf); },
  };
}
