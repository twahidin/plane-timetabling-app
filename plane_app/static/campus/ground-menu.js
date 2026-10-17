/* The campus's Ground menu: the admin's choice of the courtyard's ground for this timetable, a <select> beside the
   3D view's full-screen button (index.html #campus-ground; CSS shows it only while the campus is on, so never in
   Classic). "" follows the template ("From the template (…)"), or one of the four kinds. Rooms never change.
   createGroundMenu(select, {names, save(ground) → Promise<{ground, ground_template, ground_override}>, onSaved(g)})
     → {set(doc)}: set fills it from a campus document (either /api/campus or /api/campus/setup); a document without
   a known ground_template (none yet, an old server, set(null) on a timetable switch) hides it, and the next document
   shows it again with that timetable's value; a save still in flight then answers for the old timetable and moves
   nothing. While a save runs the menu is disabled; a failed save puts the last choice back and says so in the title.
   Imports nothing (Node-testable); every text is set with textContent. */
export function createGroundMenu(select, { names, save, onSaved }) {
  const kinds = Object.keys(names || {});
  let current = '';                                  // the override shown: "" or a kind
  let template = null;                               // the template's kind the first option names
  let busy = false;
  let gen = 0;                                       // moves on whenever the menu is hidden (another timetable)
  const title = select.getAttribute('title');
  const FAILED = "Couldn't change the ground; try again";
  const say = (text) => { if (text) select.setAttribute('title', text); else if (title != null) select.setAttribute('title', title); };

  function fill() {
    const opts = [['', `From the template (${names[template]})`]].concat(kinds.map((k) => [k, names[k]]));
    select.replaceChildren(...opts.map(([value, text]) => {
      const o = document.createElement('option'); o.value = value; o.textContent = text; return o;
    }));
    select.value = current;
  }

  function set(doc) {
    const t = doc && typeof doc === 'object' ? doc.ground_template : null;
    if (typeof t !== 'string' || !kinds.includes(t)) {
      template = null; select.hidden = true; gen++;
      busy = false; select.disabled = false; say('');
      return;
    }
    const o = doc.ground_override;
    const next = typeof o === 'string' && kinds.includes(o) ? o : '';
    if (busy) return;                                // a save in flight decides what shows
    if (t !== template || next !== current || !select.children.length) { template = t; current = next; fill(); }
    select.hidden = false;
  }

  select.addEventListener('change', () => {
    const want = kinds.includes(select.value) ? select.value : '';
    if (busy || want === current) return;
    busy = true; select.disabled = true;
    const mine = gen;
    Promise.resolve().then(() => save(want)).then((g) => {
      if (mine !== gen) return;                      // the timetable changed meanwhile: its own document decides
      busy = false; select.disabled = false; say('');
      current = want; set(g);
      if (onSaved) onSaved(g);
    }).catch((err) => {
      if (mine !== gen) return;
      console.warn('campus ground not saved:', (err && (err.message || err.name)) || String(err));
      busy = false; select.disabled = false;
      select.value = current;                        // not saved: the menu says what the campus still shows
      say(FAILED);
    });
  });

  return { set };
}
