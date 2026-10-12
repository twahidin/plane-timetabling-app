/* The campus's two looks (spec 2026-10-11-campus-looks-design.md §2.4), from the approved mockup
   (docs/design/2026-10-11-campus/mockup.html, THEMES.toy and THEMES.site). Construction draws the site
   with its own colours and, once the timetable is checked with no clashes, the *Final ones. Lessons are
   coloured from `dept` in legend order. */
const DEPT = ['#8b7cf6', '#2ec4b6', '#ffb020', '#ff6b9a', '#22c88a', '#5aa9ff', '#f97362', '#b07cf0'];

export const THEMES = {
  toy: {
    exposure: 1.05, round: 0.32,
    hemi: ['#ffffff', '#b9d7ff', 1.15], sun: ['#fff1dc', 2.4], ambient: 0.15,
    grass: '#a6dd8a', dirt: '#e2b98f', path: '#fff3d6', field: '#7fd06a', fieldLine: '#ffffff',
    wall: '#fff6ea', roofs: ['#ff8c7a', '#78c3ff', '#ffc85a'], trim: '#ffffff', winOff: '#cfe6ff', home: '#e9e3ff',
    dept: DEPT, emissive: 0.25, trunk: '#a8754b', leaves: ['#6fcf6f', '#93df7c', '#5bbf6a'], skin: '#ffd9b8', hair: '#5b3a29',
    rough: 0.55, metal: 0,
  },
  site: {
    exposure: 1.05, round: 0.3,
    hemi: ['#ffffff', '#ecd9b8', 1.1], sun: ['#fff1dc', 2.4], ambient: 0.15,
    grass: '#e9c88f', grassFinal: '#a6dd8a', dirt: '#b98858', path: '#dccfb7', field: '#dcb37c', fieldFinal: '#7fd06a',
    fieldLine: '#fff8ea', wall: '#e8e3da', wallFinal: '#fff6ea', roofs: ['#c9c3b8', '#c9c3b8', '#c9c3b8'],
    roofsFinal: ['#ff8c7a', '#78c3ff', '#ffc85a'], trim: '#ffc21a', winOff: '#9fb4c9', home: '#dfe9f6',
    dept: DEPT, emissive: 0.25, trunk: '#a8754b', leaves: ['#6fcf6f', '#93df7c', '#5bbf6a'], skin: '#ffd9b8', hair: '#5b3a29',
    rough: 0.6, metal: 0,
  },
};

/* The theme to draw: Toy town, the Construction site, or the finished school after a clean check. */
export function themeFor(look, finished) {
  if (look !== 'site') return { name: 'toy', site: false, T: THEMES.toy };
  const s = THEMES.site;
  if (!finished) return { name: 'site', site: true, T: s };
  return { name: 'site', site: false, T: { ...s, grass: s.grassFinal, field: s.fieldFinal, wall: s.wallFinal, roofs: s.roofsFinal, trim: '#ffffff' } };
}

/* colour key ("d:Maths" / "s:Biology") → its colour, in the legend's order. */
export function colours(legend) {
  const out = {};
  Object.keys(legend || {}).forEach((k, i) => { out[k] = DEPT[i % DEPT.length]; });
  return out;
}
