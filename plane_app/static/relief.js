// Relief: the Relief card (absences with how many of their lessons are covered or have cards waiting in
// the chat and the covers applied for them, each with its own Remove, Add absence, Plan cover, Remove,
// the relief settings with the term start the ledger counts from, and the Ledger link) and the Add absence
// dialog, whose part of the day is picked by the timetable's own labels. Plan cover stages one
// cover card per lesson in the chat thread; the cards appear in the chat and are applied there like
// every proposal. Relief lives on the base timetable, so a period timetable shows the same absences.
(function () {
  'use strict';
  const el = (id) => document.getElementById(id);
  const api = async (path, opts = {}) => {
    const r = await fetch(path, { headers: { 'Content-Type': 'application/json' }, ...opts });
    if (!r.ok) throw new Error((await r.json().catch(() => ({}))).detail || r.statusText);
    return r.json();
  };
  const node = (tag, cls, text) => { const n = document.createElement(tag); if (cls) n.className = cls; if (text != null) n.textContent = text; return n; };
  const button = (text, title) => { const b = node('button', 'btn', text); b.type = 'button'; if (title) b.title = title; return b; };

  const DAYS = ['Sun', 'Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat'];
  const MONTHS = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec'];
  const parse = (iso) => { const [y, m, d] = iso.split('-').map(Number); return new Date(y, m - 1, d); };
  const dayDate = (iso) => { const d = parse(iso); return `${DAYS[d.getDay()]} ${d.getDate()} ${MONTHS[d.getMonth()]}`; };
  const dates = (a) => (a.from === a.to ? dayDate(a.from) : `${dayDate(a.from)} to ${dayDate(a.to)}`);
  const today = () => { const d = new Date(); return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, '0')}-${String(d.getDate()).padStart(2, '0')}`; };
  const plural = (n, one, many) => `${n} ${n === 1 ? one : many}`;

  function status(a) {                           // "open", "2 cards waiting in the chat", "1 of 2 covered", "all 2 covered"
    if (a.lessons == null) return { cls: 'quiet', text: 'lessons unknown: check the term calendar in Settings' };
    if (a.lessons === 0) return { cls: 'quiet', text: 'no lessons to cover' };
    if (a.covered >= a.lessons) return { cls: '', text: a.lessons === 1 ? 'covered' : `all ${a.lessons} covered` };
    if (a.pending > 0) {                         // planned: cover cards staged, not all applied yet
      return { cls: 'warn', text: (a.covered > 0 ? `${a.covered} of ${a.lessons} covered · ` : '') +
        `${plural(a.pending, 'card', 'cards')} waiting in the chat` };
    }
    if (a.covered > 0) return { cls: 'warn', text: `${a.covered} of ${a.lessons} covered` };
    return { cls: 'warn', text: `open · ${plural(a.lessons, 'lesson', 'lessons')} to cover` };
  }

  let data = null, teachers = [], unlisted = [];
  const note = (text) => { const n = el('relief-note'); n.textContent = text || ''; n.hidden = !text; };

  async function planCover(a, btn) {
    btn.disabled = true;
    try {
      const r = await api(`/api/relief/plan/${encodeURIComponent(a.id)}`);
      const mine = (r.items || []).filter((c) => c.absence === a.id);
      if (!r.items || !r.items.length) { note(`${a.name}: no lessons to cover on those dates.`); return; }
      if (window.handleEvents) window.handleEvents([{ kind: 'proposals', items: r.items }]);
      const open = mine.filter((c) => !c.uncovered).length;
      if (!mine.length) note(`${a.name}: Its lessons are covered by cards already in the chat (lessons taught together with another absent teacher).`);
      else note(`${a.name}: ${plural(mine.length, 'cover card', 'cover cards')} in the chat` +
        (open < mine.length ? `, ${mine.length - open} with no teacher free to take it` : '') +
        '. Apply the ones you want; nothing changes until you do.');
      await window.loadRelief(true);              // the absence now shows its cards waiting in the chat
      const chat = el('chat');
      if (chat && chat.scrollIntoView) chat.scrollIntoView({ behavior: 'smooth', block: 'start' });
    } catch (e) { note(e.message); }
    finally { btn.disabled = false; }
  }

  async function remove(a, btns) {
    btns.querySelectorAll('button').forEach((b) => { b.disabled = true; });
    try {
      await api(`/api/relief/absences/${encodeURIComponent(a.id)}`, { method: 'DELETE' });
      note(`${a.name}'s absence removed` + (a.covered ? `, with its ${plural(a.covered, 'cover', 'covers')}.` : '.'));
      if (a.covered && window.reloadModel) window.reloadModel().catch(() => {});
      await window.loadRelief(true);
    } catch (e) { note(e.message); btns.querySelectorAll('button').forEach((b) => { b.disabled = false; }); }
  }

  // Remove asks inline, never window.confirm (embedded browsers suppress native popups), and only when
  // covers would go with the absence.
  function askRemove(a, btns) {
    if (!a.covered) { remove(a, btns); return; }
    btns.textContent = '';
    btns.appendChild(node('span', 'relief-ask', `Remove the absence and its ${plural(a.covered, 'cover', 'covers')}?`));
    const yes = button('Remove'), no = button('Keep');
    yes.addEventListener('click', () => remove(a, btns));
    no.addEventListener('click', renderList);
    btns.append(yes, no);
  }

  // One cover taken back: logged first, so Undo puts it back. The dated views may show it: reload them.
  async function removeCover(c, row) {
    row.querySelectorAll('button').forEach((b) => { b.disabled = true; });
    try {
      await api(`/api/relief/covers/${encodeURIComponent(c.id)}`, { method: 'DELETE' });
      note(`Cover removed: ${c.text}. Undo puts it back.`);
      if (window.reloadModel) window.reloadModel().catch(() => {});
      await window.loadRelief(true);
    } catch (e) { note(e.message); row.querySelectorAll('button').forEach((b) => { b.disabled = false; }); }
  }

  function askRemoveCover(c, row) {             // asks inline, like the absence's Remove
    row.textContent = '';
    row.appendChild(node('span', 'relief-ask', 'Remove this cover?'));
    const yes = button('Remove'), no = button('Keep');
    yes.addEventListener('click', () => removeCover(c, row));
    no.addEventListener('click', renderList);
    row.append(yes, no);
  }

  function renderList() {
    const list = el('relief-list');
    list.textContent = '';
    el('absence-new').disabled = !teachers.length;
    el('absence-new').title = teachers.length ? 'Say who is away and when' : 'Build a timetable first: an absence is a teacher of the live timetable';
    if (!data || !data.absences.length) {
      list.appendChild(node('li', 'empty', 'No absences. Add one when a teacher is away, then Plan cover: the assistant offers a teacher for each lesson, the relief pool first.'));
      return;
    }
    const sorted = data.absences.slice().sort((x, y) => (x.from < y.from ? -1 : x.from > y.from ? 1 : 0));
    sorted.forEach((a) => {
      const li = node('li', 'absence');
      const head = node('div', 'absence-head');
      head.appendChild(node('b', null, a.name));
      const st = status(a);
      head.appendChild(node('span', 'badge' + (st.cls ? ' ' + st.cls : ''), st.text));
      li.appendChild(head);
      const meta = [dates(a), a.slot_labels, a.reason].filter(Boolean).join(' · ');   // "P5 to P7": the day's own labels
      li.appendChild(node('div', 'absence-meta', meta));
      (a.covers || []).forEach((c) => {           // "Tue 6 Oct P3 Maths, set A — Mr Tan"
        const row = node('div', 'absence-btns absence-cover');
        row.appendChild(node('span', 'absence-meta', c.text));
        const rm = button('Remove', 'Take this cover back (Undo puts it back)');
        rm.addEventListener('click', () => askRemoveCover(c, row));
        row.appendChild(rm);
        li.appendChild(row);
      });
      const btns = node('div', 'absence-btns');
      const plan = button('Plan cover', 'Offer a teacher for each lesson, as cards in the chat');
      plan.disabled = !a.lessons || a.covered >= a.lessons;
      plan.addEventListener('click', () => planCover(a, plan));
      const rm = button('Remove', a.covered ? 'Remove the absence and the covers made for it' : 'Remove the absence');
      rm.addEventListener('click', () => askRemove(a, btns));
      btns.append(plan, rm);
      li.appendChild(btns);
      list.appendChild(li);
    });
  }

  function renderSettings() {
    const s = (data && data.settings) || { pool: [], max_per_day: 2, term_start: '' };
    const pool = el('relief-pool');
    pool.textContent = '';
    teachers.forEach((t) => {
      const o = node('option', null, t.name); o.value = t.id; o.selected = s.pool.includes(t.id);
      pool.appendChild(o);
    });
    // Pool members that are not teachers of the live timetable (another role, or no live timetable
    // yet) have no option: they are kept on save rather than silently dropped.
    unlisted = s.pool.filter((id) => !teachers.some((t) => t.id === id));
    el('relief-settings-save').disabled = !teachers.length;
    el('relief-max-per-day').value = s.max_per_day;
    el('relief-term-start').value = s.term_start || '';            // blank: the term calendar's
    const since = data && data.term_start;
    el('relief-since').textContent = !data ? '' : since ? `Ledger counts covers since ${dayDate(since)} ${since.slice(0, 4)}.`
      : 'Ledger counts every cover: no term start is set (Relief settings, or the term calendar in Settings).';
    const names = (data && data.pool_names) || [];
    el('relief-settings-summary').textContent = (names.length ? `pool: ${names.join(', ')}` : 'no relief pool') +
      ` · at most ${s.max_per_day} a day`;
  }

  // `keepNote` is for this card's own reloads, right after it wrote a note; any other reload (a cover
  // applied or undone, a timetable switch) clears the note, which may no longer be true.
  window.loadRelief = async (keepNote) => {
    if (keepNote !== true) note('');
    try { data = await api('/api/relief'); }
    catch (e) { data = null; }
    try { teachers = (await api('/api/print/targets')).teachers || []; }
    catch (e) { teachers = []; }                  // no live timetable yet
    renderList();
    renderSettings();
  };

  // ---------- relief settings ----------
  el('relief-settings-save').addEventListener('click', async () => {
    const btn = el('relief-settings-save'), msg = el('relief-settings-note');
    const body = { pool: Array.from(el('relief-pool').selectedOptions).map((o) => o.value).concat(unlisted),
      max_per_day: Number(el('relief-max-per-day').value), term_start: el('relief-term-start').value };
    btn.disabled = true; msg.textContent = '';
    try {
      const r = await api('/api/relief/settings', { method: 'PUT', body: JSON.stringify(body) });
      if (data) { data.settings = r.settings; data.pool_names = r.pool_names; data.term_start = r.term_start; }
      renderSettings();
      msg.textContent = 'Saved.';
    } catch (e) { msg.textContent = e.message; }
    finally { btn.disabled = false; }
  });

  // ---------- Add absence dialog ----------
  // The part of the day is picked by the timetable's labels (`day` of /api/relief: every slot of the
  // day but a break), each option's value its offset within the day, so "P5" is the right slot even
  // with a Recess before it. A blank start is the start of the day, a blank end the end of the day.
  const dlg = el('absence-dialog'), form = el('absence-form'), errBox = el('absence-error');
  function fillSlots() {
    const day = (data && data.day) || [];
    ['absence-slots-from', 'absence-slots-to'].forEach((id) => {
      const sel = el(id);
      sel.textContent = '';
      const blank = node('option', null, id === 'absence-slots-from' ? 'the start of the day' : 'the end of the day');
      blank.value = '';
      sel.appendChild(blank);
      day.forEach((s) => { const o = node('option', null, s.label); o.value = String(s.slot); sel.appendChild(o); });
      sel.disabled = !day.length;
    });
  }
  el('absence-new').addEventListener('click', async () => {
    await window.loadRelief(true);
    if (!teachers.length) return;
    form.reset();
    const sel = el('absence-person');
    sel.textContent = '';
    teachers.forEach((t) => { const o = node('option', null, t.name); o.value = t.id; sel.appendChild(o); });
    el('absence-from').value = today(); el('absence-to').value = today();
    fillSlots();
    errBox.hidden = true; errBox.textContent = '';
    if (typeof dlg.showModal === 'function') dlg.showModal(); else dlg.setAttribute('open', '');
    sel.focus();
  });
  const close = () => { if (dlg.open) dlg.close(); else dlg.removeAttribute('open'); };
  el('absence-cancel').addEventListener('click', close);
  el('absence-from').addEventListener('change', () => {
    if (!el('absence-to').value || el('absence-to').value < el('absence-from').value) el('absence-to').value = el('absence-from').value;
  });
  form.addEventListener('submit', async (ev) => {
    ev.preventDefault();
    const fail = (msg) => { errBox.textContent = msg; errBox.hidden = false; };
    const body = { person: el('absence-person').value, from: el('absence-from').value, to: el('absence-to').value || el('absence-from').value,
      reason: el('absence-reason').value.trim() };
    if (!body.person) return fail('Pick the teacher who is away.');
    if (!body.from) return fail('Pick the first day.');
    const lo = el('absence-slots-from').value, hi = el('absence-slots-to').value;
    const day = (data && data.day) || [];
    if ((lo || hi) && day.length) {              // option values are offsets within the day; the API takes [first, one past last]
      const a = lo ? Number(lo) : day[0].slot, b = hi ? Number(hi) : day[day.length - 1].slot;
      if (b < a) return fail('The part of the day ends before it starts.');
      body.slots = [a, b + 1];
    }
    el('absence-ok').disabled = true;
    try {
      const r = await api('/api/relief/absences', { method: 'POST', body: JSON.stringify(body) });
      close();
      const name = (teachers.find((t) => t.id === r.absence.person) || {}).name || r.absence.person;
      note(`${name}'s absence added. Press Plan cover to offer a teacher for each lesson.`);
      await window.loadRelief(true);
    } catch (e) { fail(e.message); }
    finally { el('absence-ok').disabled = false; }
  });

  window.loadRelief();
})();
