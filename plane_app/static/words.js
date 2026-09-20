(function () {
  'use strict';
  // The timetable's own words (spec docs/superpowers/specs/2026-09-20-plain-language-view-design.md §2):
  // education's by default; the start wizard sets another domain's. /api/solid carries the current set.
  const DEFAULT = { person: 'teacher', group: 'class', requirement: 'lesson', venue: 'room' };
  let current = { ...DEFAULT };

  // A plain-English plural: "nurse" -> "nurses", "coach" -> "coaches", "match" -> "matches",
  // "party" -> "parties", "consulting room" -> "consulting rooms" (only the last word takes the plural).
  function pluralize(word) {
    const parts = String(word || '').split(' ');
    const last = parts.pop();
    const plural = /[sxz]$|[cs]h$/i.test(last) ? last + 'es' : /[^aeiou]y$/i.test(last) ? last.slice(0, -1) + 'ies' : last + 's';
    return parts.concat([plural]).join(' ');
  }
  function capitalize(word) { return word ? word.charAt(0).toUpperCase() + word.slice(1) : word; }
  function set(v) { current = { ...DEFAULT }; Object.keys(DEFAULT).forEach((k) => { if (v && v[k]) current[k] = String(v[k]); }); }
  function get() { return { ...current }; }
  function isDefault(v) { return Object.keys(DEFAULT).every((k) => (v || {})[k] === DEFAULT[k]); }
  function word(key, opts) {
    const o = opts || {};
    let w = current[key] || DEFAULT[key] || String(key);
    if (o.plural) w = pluralize(w);
    if (o.cap) w = capitalize(w);
    return w;
  }
  // Rewrites every element marked data-word="<key>" (data-word-plural / data-word-cap pick the form).
  function apply(root) {
    (root || document).querySelectorAll('[data-word]').forEach((n) => {
      n.textContent = word(n.dataset.word, { plural: 'wordPlural' in n.dataset, cap: 'wordCap' in n.dataset });
    });
  }
  window.Words = { DEFAULT, pluralize, capitalize, set, get, isDefault, word, apply };
})();
