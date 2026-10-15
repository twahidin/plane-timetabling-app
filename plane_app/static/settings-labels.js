/* Slot labels for a whole cycle. A timetable stores one label a slot for every day of its cycle
   ("Odd Mon P1" … "Odd Fri P8" for a 5-day, 8-period cycle; "07:30, 08:10, …" for a one-day cycle).
   The settings page checks and regenerates them with these helpers, so a wizard timetable keeps its
   cycle, its day names and its period style. Plain functions on window.SettingsLabels. */
(function () {
  const lastSpace = (label) => String(label).lastIndexOf(' ');
  const prefixOf = (label) => (lastSpace(label) > 0 ? String(label).slice(0, lastSpace(label)) : '');
  const slotOf = (label) => String(label).slice(lastSpace(label) + 1);

  function cycleDays(labels, perDay) {
    const n = (labels || []).length;
    return perDay > 0 && n > 0 && n % perDay === 0 ? n / perDay : 1;
  }

  function check(labels, perDay) {
    const n = (labels || []).length;
    if (!(perDay > 0)) return 'Slots per day must be a whole number above 0';
    if (!n) return 'Give the slot labels (one for each slot of each day of the cycle)';
    if (n % perDay) return `There are ${n} slot labels for ${perDay} slots per day. Give one label for each slot of each day of the cycle, so the count must be a multiple of ${perDay}.`;
    return null;
  }

  function clock(start, i, minutes) {
    const [h, m] = String(start || '00:00').split(':').map(Number);
    const t = (h || 0) * 60 + (m || 0) + i * minutes;
    return String(Math.floor(t / 60) % 24).padStart(2, '0') + ':' + String(t % 60).padStart(2, '0');
  }

  /* New labels for `perDay` slots a day, keeping the old cycle's length, each day's name and the
     period style (P1, P2… or clock times from `start` every `minutes`). */
  function regenerate(oldLabels, oldPerDay, perDay, minutes, start) {
    const old = oldLabels || [];
    const days = cycleDays(old, oldPerDay);
    const periods = old.length > 0 && old.every((l) => /^P\d+$/i.test(slotOf(l)));
    const out = [];
    for (let d = 0; d < days; d++) {
      const prefix = days > 1 || old.length ? prefixOf(old[d * oldPerDay] || '') : '';
      for (let i = 0; i < perDay; i++) {
        const slot = periods ? `P${i + 1}` : clock(start, i, minutes);
        out.push(prefix ? `${prefix} ${slot}` : slot);
      }
    }
    return out;
  }

  window.SettingsLabels = { check, cycleDays, regenerate, prefixOf };
})();
