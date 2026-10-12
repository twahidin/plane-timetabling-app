/* The campus plays the journey (spec 2026-10-11-campus-looks-design.md §3.2): each step, as it happens, with
   the effects scene.js keeps (from the approved mockup's timeline). Steps play one after another; a burst of
   solver progress flickers at most every 0.4 s. Animations follow window.planeMotion: 'reduced' halves the
   tweens and drops the camera turn, the confetti and the crane's hook; 'off' jumps to the end state.
   No Three.js here: the scene and the controller come in as arguments. */

const motion = () => (typeof window !== 'undefined' && window.planeMotion ? window.planeMotion.level() : 'full');

/* ctl: {doc(), look(), reload(force), popLessons(seconds, live), unfinished(), clearForReplay(), setPins(on)} from main.js.
   reload() refreshes the data (rebuilding only a changed layout); reload(true) rebuilds the true school. */
export function createStages(scene, ctl) {
  let chain = Promise.resolve(), running = 0, token = 0, lastFlicker = 0, unfinished = [];   // steps queued or playing, in order
  const wait = (ms) => new Promise((r) => setTimeout(r, ms));

  async function run(detail, my, replaying) {
    const level = motion(), live = () => my === token;
    if (!ctl.doc() || !live()) return;
    const site = ctl.look() === 'site', reduced = level === 'reduced';
    if (level === 'off') {                      // end states only, on fresh data
      await ctl.reload(detail.stage === 'checked');
      return;
    }
    // the step plays on the school as it now is (a new plan may have new buildings)
    if (!replaying && (detail.stage === 'plan' || detail.stage === 'draft')) { await ctl.reload(); if (!live()) return; }
    scene.fx.speed(reduced ? 0.5 : 1);
    switch (detail.stage) {
      case 'files':
        await scene.fx.flyFiles((detail.files || []).map((f) => f.name).filter(Boolean));
        break;
      case 'plan':
        await (site ? scene.fx.siteRaise(4, !reduced) : scene.fx.riseAll(3));
        break;
      case 'draft':
        await ctl.popLessons(reduced ? 1 : 2, live);
        break;
      case 'timetable':
        await scene.fx.pulse();
        if (live()) await ctl.reload();             // re-lights the rooms; a changed layout is rebuilt
        break;
      case 'checked':
        if (site) ctl.unfinished();                 // the handover starts from the site, even if the data already says finished
        await scene.fx.sweep();
        if (!reduced && my === token) await scene.fx.turn(2);
        if (site && my === token) await scene.fx.handover(2.5);
        if (!reduced && my === token) await scene.fx.confetti();
        if (live()) await ctl.reload(true);          // the finished school, drawn from the data
        break;
      default:
        break;
    }
  }

  function play(detail) {
    if (!detail || !detail.stage) return chain;
    if (detail.stage === 'solving') {           // not queued: a flicker now, at most every 0.4 s
      const now = Date.now();
      if (motion() !== 'off' && now - lastFlicker > 400) { lastFlicker = now; scene.fx.speed(motion() === 'reduced' ? 0.5 : 1); scene.fx.flicker(4); }
      return chain;
    }
    const my = token;
    running++; unfinished.push(detail);
    const done = () => { if (my === token) { running--; unfinished = unfinished.filter((d) => d !== detail); } };
    chain = chain.then(() => run(detail, my)).catch(() => {}).then(done);
    return chain;
  }

  /* The whole story on the current data, about 20 s in Full (Task 5). */
  async function replay() {
    const doc = ctl.doc();
    if (!doc || motion() === 'off') return;
    stop();
    const my = token;
    ctl.clearForReplay();
    const go = (d) => (my === token ? run(d, my, true) : null);
    running++;
    try {
      const source = (doc.source_name || 'Your workbooks');
      await go({ stage: 'files', files: [{ name: source }] });
      await go({ stage: 'plan' });
      await go({ stage: 'draft' });
      for (let i = 0; i < 6 && my === token; i++) { scene.fx.flicker(5); await wait(500); }
      if (my !== token) return;
      await scene.fx.pulse();
      if (doc.checked_ok) await go({ stage: 'checked' });
      else { ctl.setPins(true); await ctl.reload(true); }
    } finally { if (my === token) running--; }
  }

  /* Anything in flight sees its token is old and ends. Returns the steps that had not finished, so a hidden
     campus can hold them and play them when it shows again. */
  function stop() { token++; running = 0; chain = Promise.resolve(); const left = unfinished; unfinished = []; return left; }

  return { play, replay, stop, busy: () => running > 0 };
}
