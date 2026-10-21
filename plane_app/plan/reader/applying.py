"""Apply a reading by section, and undo it (spec docs/superpowers/specs/2026-10-12-assistant-files-design.md
§3.3–3.4). Under the plan lock: the reading is merged into the plan as it is now and only the ticked
sections are taken; a timetable reading becomes the draft; a student list replaces the timetable's
students; a combinations reading replaces each level's combinations in `blocks_setup`; the five CSV files
write the plan's staff and rooms by key, the levels store and the students of their levels. Undo puts back
what was there when nothing has changed it since."""
from __future__ import annotations

import copy
import hashlib
import json
import time
from pathlib import Path

from ... import asc_import, blocks as BK, journey, levels as LV, students as ST, week as W, week_apply, week_save
from ...intake import IntakeError
from .. import model as M
from ..issues import plan_issues
from . import inbox, packmerge as PM, readers, routes as RR, sections as S
from . import recipes as R


class ApplyError(Exception):
    """A reading that cannot be applied, undone or skipped: `code` is the HTTP status, `message` plain words."""

    def __init__(self, code: int, message: str):
        super().__init__(message)
        self.code, self.message = code, message


def _hash(value) -> str:
    return hashlib.sha1(json.dumps(value, sort_keys=True, default=str).encode()).hexdigest()


def _content(plan) -> str:
    """The hash of what a stored plan says, its revs aside: the same after a write that changed nothing
    (undoing a later apply puts back the plan an earlier one wrote, under a new rev)."""
    def strip(value):
        if isinstance(value, dict):
            return {k: strip(v) for k, v in value.items() if k not in ("rev", "rev_at")}
        if isinstance(value, list):
            return [strip(v) for v in value]
        return value
    return _hash(strip(plan))


def _find(db, rid: str, tid: str | None) -> dict:
    entry = next((e for e in RR._stored(db, tid) if e.get("id") == rid), None)
    if entry is None:
        raise ApplyError(404, "no such reading: it was applied, discarded or replaced")
    return entry


def _update(db, rid: str, reading: dict, tid: str | None = None) -> dict:
    with RR._lock:
        stored = RR._stored(db, tid)
        for e in stored:
            if e["id"] == rid:
                e["reading"] = reading
        db.set_value(RR.KEY, stored, tid)
    return next(e for e in stored if e["id"] == rid)


def reading_view(db, rid: str, tid: str | None = None) -> dict:
    """What the page sees of reading `rid` now."""
    return inbox.view(_find(db, rid, tid), db.get_settings(tid))


def plan_status(db, tid: str | None = None) -> dict:
    """Where the plan stands, in a few plain lines: ready to build, or what blocks it."""
    plan = M.normalise(db.get_value("plan", tid) or M.empty_plan())
    issues = plan_issues(plan, db.get_org("live", tid) or None, db.get_settings(tid), LV.get(db, tid))
    blocks = [i.text for i in issues if i.level == "block"]
    return {"ready": not blocks and bool(plan["requirements"]), "blocks": blocks[:5], "block_count": len(blocks),
            "warn_count": sum(1 for i in issues if i.level == "warn")}


def plan_body(db, entry: dict, current: dict, tid: str | None) -> dict:
    """The reading as sections.merged takes it; an exact workbook merged again from its kept file, with the
    sizes CSVs given with it on top."""
    reading = {**entry["reading"], "file": entry.get("file")}
    if reading.get("via") == "exact" and not reading.get("path"):     # never kept (its Students sheet stayed in)
        raise ApplyError(409, f"{inbox.NOT_KEPT[0].upper()}{inbox.NOT_KEPT[1:]}: give the workbook again without it.")
    if reading.get("via") == "exact" and reading.get("path"):
        try:
            data = Path(reading["path"]).read_bytes()
            sizes = [(Path(p).name, Path(p).read_bytes()) for p in reading.get("sizes_paths") or []]
        except OSError:
            raise ApplyError(409, "The file is gone: give it to the Assistant again.")
        try:
            reading["merged"], _ = inbox.exact_merged(db, entry.get("file") or "", data, current, tid, sizes)
        except M.PlanError as e:
            raise ApplyError(409, f"the plan has changed and this reading no longer fits it: {e}")
        except Exception as e:
            raise ApplyError(422, f"The file could not be read again: {e}")
    return reading


def _options(reading: dict, options: dict | None) -> dict:
    out = {**inbox.OPTIONS, **(reading.get("options") or {})}
    given = options if isinstance(options, dict) else {}
    if given.get("mode") in ("keep", "rebuild"):
        out["mode"] = given["mode"]
    if "classes_as_planes" in given:
        out["classes_as_planes"] = bool(given["classes_as_planes"])
    if isinstance(reading.get("week"), dict) and given.get("week") in ("keep", "adopt"):
        out["week"] = given["week"]             # only a card that offers the choice carries it
    return out


def _timetable(reading: dict, opts: dict) -> tuple[dict, dict | None]:
    """The draft a timetable reading makes: the model's organisation, or an aSc export read again from its
    kept files with the card's options; and, for an aSc export, the week its grid proposes."""
    if reading.get("organisation"):
        return reading["organisation"], None
    try:
        datas = [Path(p).read_bytes() for p in reading.get("paths") or []]
    except OSError:
        raise ApplyError(409, "The file is gone: give it to the Assistant again.")
    try:
        org, _notes, week = inbox.asc_read(datas, mode=opts["mode"], classes_as_planes=bool(opts["classes_as_planes"]))
        return org, week
    except asc_import.AscFormatError as e:
        raise ApplyError(422, str(e))
    except IntakeError as e:
        raise ApplyError(422, f"The timetable export could not be turned into a draft: {e}")


def _organisation(reading: dict, opts: dict) -> dict:
    return _timetable(reading, opts)[0]


def _lay(org: dict, week: dict, time: dict) -> dict:
    """`org`, an export's draft on its own break-free grid (one position a period), laid on the saved week
    `time` by day and period number, so a break the week has moves the later periods along. Nothing written."""
    grid = W.derive({**week, "days": [{**d, "breaks": []} for d in week["days"]]})      # the file's own grid
    old, shape = W.shape(grid), W.shape(time)
    moved, _lost = week_apply.remap_org(org, W.position_map(old, shape), old, shape)
    try:
        return inbox.apply_org_patch(moved, {})
    except IntakeError as e:
        raise ApplyError(422, f"The timetable export does not fit the week: {e}")


def _apply_timetable(db, reading: dict, opts: dict, tid: str | None) -> dict:
    org, proposed = _timetable(reading, opts)
    moved = None
    if proposed is not None:
        settings = db.get_settings(tid)
        mine, theirs = W.shape(settings["time"], settings["rules"]), W.shape(W.derive(proposed))
        if not inbox._same_week(mine, theirs) and opts.get("week") == "adopt":
            if tid != db.working_timetable():
                raise ApplyError(409, "The selected timetable changed while this waited; open it again to adopt the file's week.")
            try:                                # every refusal and the remapped draft come before the first write
                _current, new = week_save.prepared(db, proposed)
            except week_save.WeekRefused as e:
                raise ApplyError(e.code, e.message)
            org = _lay(org, proposed, new)
            try:                                # the PUT /api/settings path: check, refuse, remap the stored timetable
                body, moved = week_save.save(db, proposed)
            except week_save.WeekRefused as e:
                raise ApplyError(e.code, e.message)
            db.store_settings(body)
        elif not mine["legacy"] and inbox._same_week(mine, theirs):
            org = _lay(org, proposed, settings["time"])     # the school's own week already: lie on it, write nothing
    before = db.get_org("draft", tid)
    db.set_org("draft", org, tid)
    out = {"draft_before": before, "draft_after": _hash(db.get_org("draft", tid))}
    if moved is not None:
        out["week_changes"] = moved
    return out


def _apply_plan(db, entry: dict, names: list[str], tid: str | None) -> dict:
    try:
        current = M.normalise(db.get_value("plan", tid) or M.empty_plan())
    except M.PlanError:
        raise ApplyError(422, RR.PLAN_UNREADABLE)
    before = db.get_value("plan", tid)
    try:
        new = S.select(current, S.merged(current, plan_body(db, entry, current, tid)), names)
    except S.SelectError as e:              # what to tick, or names gone from the plan: said as they are
        raise ApplyError(409, str(e))
    except M.PlanError as e:
        raise ApplyError(409, f"the plan has changed and this reading no longer fits it: {e}")
    db.set_value("plan", new, tid)
    journey.stamp(db, "files", tid)
    after = db.get_value("plan", tid) or {}
    return {"plan_before": before, "rev_after": int(after.get("rev") or 0), "content_after": _content(after)}


STUDENTS_CHANGED = "The students have changed since; undo is not possible."
SETUP_CHANGED = "The option-block setup has changed since this was applied; undo left it as it is."
PLAN_CHANGED = "The plan has changed since this was applied; undo left it as it is."


def plan_as_written(now, undo: dict) -> bool:
    """Whether the stored plan `now` is still the one an apply wrote (`undo`'s rev_after and content_after): by
    its rev, or by its content when a later apply was undone since."""
    return (int((now or {}).get("rev") or 0) == undo.get("rev_after")
            or bool(undo.get("content_after")) and _content(now) == undo["content_after"])


def students_as_written(db, undo: dict, tid: str | None = None) -> bool:
    """Whether the stored students are still the list an apply wrote (`undo`'s after_hash)."""
    return _hash(db.get_value("students", tid) or []) == undo.get("after_hash")


def _apply_students(db, reading: dict, tid: str | None) -> dict:
    """The timetable's students replaced by the list's; what undo needs to put them back."""
    if "students" not in reading:
        raise ApplyError(409, "This student list was cleared; give the file again.")
    before = db.get_value("students", tid)
    db.set_value("students", reading.get("students") or [], tid)
    return {"before": before, "after_hash": _hash(db.get_value("students", tid) or [])}


def setup_as_written(db, undo: dict, tid: str | None = None) -> bool:
    """Whether the stored `blocks_setup` is still the one an apply wrote (`undo`'s after_hash)."""
    return _hash(db.get_value("blocks_setup", tid) or {}) == undo.get("after_hash")


def _apply_combinations(db, reading: dict, tid: str | None) -> dict:
    """Each level's combinations in the reading replacing that level's in `blocks_setup`: a level with a
    setup keeps its limits and subjects, drops its placement and the overrides naming a code no longer
    offered (as the PUT route does); one without gets the default limits.
    What undo needs to put `blocks_setup` back exactly (None when there was none)."""
    found = reading.get("combinations")
    if not isinstance(found, dict) or not found:
        raise ApplyError(409, "This combinations sheet was cleared; give the file again.")
    with BK.SETUP_LOCK:                     # the setup route's read-modify-write lock, taken after the plan lock
        before = db.get_value("blocks_setup", tid)
        setups = copy.deepcopy(before) if isinstance(before, dict) else {}
        stored = db.get_value("students", tid)
        students, plan = (stored if isinstance(stored, list) else []), None
        for key, combinations in found.items():
            key = BK.canonical_key(key)
            try:                                            # under an old raw key it is moved to this one (R15)
                _stored, have = BK.find_setup(setups, key)
            except BK.SetupClash as e:                      # two stored setups of the level differ (R20): nothing written
                raise ApplyError(409, str(e))
            if isinstance(have, dict) and have.get("combinations"):
                plan = plan if plan is not None else _stored_plan(db, tid)
                remapped = PM.remapped_codes(have.get("combinations") or [], combinations, students, plan, key,
                                             have.get("overrides"))
                if remapped:                                # a code in use would name other subjects (R23): nothing written
                    raise ApplyError(409, PM.remap_refusal(remapped))
            if isinstance(have, dict) and have:
                offered = {ST.norm(c["code"]): c["code"] for c in combinations}
                kept = {sid: offered[ST.norm(code)] for sid, code in (have.get("overrides") or {}).items()
                        if isinstance(code, str) and ST.norm(code) in offered}      # as PUT does: only codes still offered
                setups = BK.put_setup(setups, key, {**have, "combinations": copy.deepcopy(combinations),
                                                    "overrides": kept, "placement": None})   # place again
            else:
                setups = BK.put_setup(setups, key, {
                    "year": {"level": key.partition(":")[2]}, "combinations": copy.deepcopy(combinations),
                    "limits": dict(BK.DEFAULT_LIMITS), "subjects": {}, "overrides": {}, "placement": None})
        db.set_value("blocks_setup", setups, tid)
        return {"before": before, "after_hash": _hash(db.get_value("blocks_setup", tid) or {})}


# ---------------------------------------------------------------------------
# the five CSV files (csv-pack spec docs/superpowers/specs/2026-10-20-csv-pack-design.md, Calls §2 and
# §4): staff and venues update the plan by key, requirements the levels store, the level students list
# the students of its levels; combinations go through _apply_combinations. What each writes is
# packmerge's, the card's counts the same; undo is exact and refused once its target changed.
# ---------------------------------------------------------------------------

LEVELS_CHANGED = "The levels have changed since this was applied; undo left them as they are."
PACK_CLEARED = "This reading was cleared; give the file again."


def _stored_plan(db, tid: str | None) -> dict:
    try:
        return M.normalise(db.get_value("plan", tid) or M.empty_plan())
    except M.PlanError:
        raise ApplyError(422, RR.PLAN_UNREADABLE)


def _apply_pack_plan(db, reading: dict, kind: str, tid: str | None) -> dict:
    """staff.csv or venues.csv merged into the plan by key (nobody and no room removed), written as
    _apply_plan writes: one write that moves the rev, locks checked. What undo needs, as _apply_plan's."""
    items = reading.get("items")
    if not isinstance(items, list):
        raise ApplyError(409, PACK_CLEARED)
    current = _stored_plan(db, tid)
    before = db.get_value("plan", tid)
    new = copy.deepcopy(current)
    if kind == "staff":
        new["staff"] = PM.merge_staff(current["staff"], items)[0]
    else:
        new["venues"] = PM.merge_venues(current["venues"], items)[0]
    try:
        new = M.normalise(new)
        M.check_locks(current, new)
    except M.PlanError as e:
        raise ApplyError(409, f"the plan has changed and this file no longer fits it: {e}")
    db.set_value("plan", new, tid)
    journey.stamp(db, "files", tid)
    after = db.get_value("plan", tid) or {}
    return {"plan_before": before, "rev_after": int(after.get("rev") or 0), "content_after": _content(after)}


def levels_as_written(db, undo: dict, tid: str | None = None) -> bool:
    """Whether the stored levels are still those an apply wrote (`undo`'s after_hash)."""
    return _hash(db.get_value(LV.KEY, tid)) == undo.get("after_hash")


def _apply_levels(db, reading: dict, tid: str | None) -> dict:
    """requirements.csv: each of its levels' subject rows replacing that level's in the levels store."""
    found = reading.get("levels")
    if not isinstance(found, dict) or not found:
        raise ApplyError(409, PACK_CLEARED)
    before = db.get_value(LV.KEY, tid)
    try:
        LV.put(db, tid, PM.merge_levels(LV.get(db, tid), found)[0])
    except ValueError as e:                 # a code, never a value
        raise ApplyError(409, f"These levels could not be stored ({e}); give the file again.")
    return {"before": before, "after_hash": _hash(db.get_value(LV.KEY, tid))}


def _apply_level_students(db, reading: dict, tid: str | None) -> dict:
    """The level students list: the students of its levels (and any with one of its IDs) replaced by its
    own; students of other levels untouched. What undo needs, as _apply_students's."""
    if "students" not in reading:
        raise ApplyError(409, "This student list was cleared; give the file again.")
    plan = _stored_plan(db, tid)
    before = db.get_value("students", tid)
    stored = before if isinstance(before, list) else []
    db.set_value("students", PM.merge_students(stored, reading.get("students") or [], plan)[0], tid)
    return {"before": before, "after_hash": _hash(db.get_value("students", tid) or [])}


def forget_students(db, tid: str | None = None) -> int:
    """Every student-list reading of `tid` without the names it holds (its students and its undo's
    `before`), marked `removed`: an applied card no longer offers Undo, a waiting one is skipped. Called
    when all the students are removed. The number of readings changed."""
    n = 0
    with RR._lock:
        stored = RR._stored(db, tid)
        for e in stored:
            r = e["reading"]
            if r.get("kind") not in ("students", "level_students") or not ("students" in r or "undo" in r):
                continue
            r = {k: v for k, v in r.items() if k not in ("students", "undo")}
            r["removed"] = True
            if r.get("status") in (None, "waiting"):
                r["status"] = "skipped"
            e["reading"] = r
            n += 1
        if n:
            db.set_value(RR.KEY, stored, tid)
    return n


def _saved_reader(db, entry: dict) -> dict:
    """A plan read by a saved reader counts its use; one read afresh by the model is saved as a reader. The
    plan is applied already: a reader that cannot be kept is a note, not a failure."""
    reading = entry["reading"]
    out: dict = {}
    reader_sig = reading.get("reader_signature")
    if reader_sig:
        kept = readers.used(db, reader_sig)        # count the use and remember this layout; never save it again
        if kept is not None:
            sig = reading.get("signature")
            if sig and sig != reader_sig:
                readers.add_alias(db, reader_sig, sig)
            out["reader"] = kept.get("name") or "Saved reader"
    elif reading.get("recipes") and reading.get("signature"):
        name = reading.get("summary") or entry.get("file") or "Saved reader"
        try:
            saved = readers.save(db, reading["signature"], name, reading["recipes"])
            readers.used(db, saved["signature"])
            out["reader"] = saved["name"]
        except R.RecipeError as e:
            out["note"] = f"The reader was not saved: {e}"
    return out


def apply(db, rid: str, ticked: list[str] | None, options: dict | None = None, tid: str | None = None) -> dict:
    """Blocking (it takes the plan lock). Reading `rid` applied: its ticked sections (None: those the card
    shows ticked) into the plan, or a timetable reading into the draft with `options` over its own."""
    tid = tid or db.working_timetable()
    with db.plan_lock(tid):
        entry = _find(db, rid, tid)
        view = inbox.view(entry)
        if view["status"] == "applied":
            raise ApplyError(409, "Already applied.")
        if view["status"] == "skipped":
            raise ApplyError(409, "This reading was skipped.")
        if view["status"] != "waiting":
            raise ApplyError(409, view["error"] or "This reading could not be read; try it again or give the file again.")
        reading = entry["reading"]
        secs = view["sections"]
        wanted = ticked if ticked is not None else [k for k, s in secs.items() if s.get("ticked")]
        names = [n for n in wanted if n in secs]
        if not names:
            raise ApplyError(409, "Nothing ticked to apply.")
        week_changes = None
        if view["kind"] == "timetable":
            opts = _options(reading, options)
            undo = {"kind": "timetable", **_apply_timetable(db, reading, opts, tid)}
            week_changes = undo.pop("week_changes", None)      # undo puts the draft back; a saved week stays
            undo["week_adopted"] = week_changes is not None
            reading = {**reading, "options": opts}
        elif view["kind"] == "students":
            undo = {"kind": "students", **_apply_students(db, reading, tid)}
        elif view["kind"] == "combinations":
            undo = {"kind": "combinations", **_apply_combinations(db, reading, tid)}
        elif view["kind"] in ("staff", "venues"):
            undo = {"kind": "plan", **_apply_pack_plan(db, reading, view["kind"], tid)}
        elif view["kind"] == "requirements":
            undo = {"kind": "levels", **_apply_levels(db, reading, tid)}
        elif view["kind"] == "level_students":
            undo = {"kind": "students", **_apply_level_students(db, reading, tid)}
        else:
            undo = {"kind": "plan", **_apply_plan(db, entry, names, tid)}
        applied = {n: int(secs[n].get("count") or 0) for n in names}
        entry = _update(db, rid, {**reading, "status": "applied", "applied_at": time.time(), "applied": applied,
                                  "undo": undo}, tid)
    extra = _saved_reader(db, entry) if view["kind"] == "plan" else {}
    return {"ok": True, "applied": applied, "status": plan_status(db, tid), "reading": inbox.view(entry, db.get_settings(tid)), **extra,
            **({"week_changes": week_changes} if view["kind"] == "timetable" and week_changes is not None else {})}


def choose(db, rid: str, sections: dict | None, options: dict | None = None, tid: str | None = None) -> dict:
    """Blocking. A waiting reading's ticks (and a timetable reading's options) as its card shows them, so a
    yes in the chat applies what the card shows. Section names the reading lacks are ignored."""
    tid = tid or db.working_timetable()
    with db.plan_lock(tid):
        entry = _find(db, rid, tid)
        if inbox.view(entry)["status"] != "waiting":
            raise ApplyError(409, "Only a waiting reading's ticks can change.")
        reading = entry["reading"]
        secs = {name: dict(s) for name, s in (reading.get("sections") or {}).items()}
        for name, on in (sections or {}).items():
            if name in secs and isinstance(on, bool):
                secs[name]["ticked"] = on
        reading = {**reading, "sections": secs, "updated_at": time.time()}    # the card the user changed last
        if reading.get("kind") == "timetable" and isinstance(options, dict):
            reading["options"] = _options(reading, options)
        entry = _update(db, rid, reading, tid)
    return {"reading": inbox.view(entry, db.get_settings(tid))}


def undo(db, rid: str, tid: str | None = None) -> dict:
    """Blocking. What applying `rid` changed put back, when nothing has changed it since; else refused and
    left as it is."""
    tid = tid or db.working_timetable()
    with db.plan_lock(tid):
        entry = _find(db, rid, tid)
        reading = entry["reading"]
        undo = reading.get("undo")
        if reading.get("status") != "applied" or not isinstance(undo, dict):
            raise ApplyError(409, "Nothing to undo.")
        if undo.get("kind") == "timetable":
            if _hash(db.get_org("draft", tid)) != undo.get("draft_after"):
                raise ApplyError(409, "The draft has changed since this was applied; undo left it as it is.")
            db.set_org("draft", undo.get("draft_before"), tid)
        elif undo.get("kind") == "students":
            if not students_as_written(db, undo, tid):
                raise ApplyError(409, STUDENTS_CHANGED)
            db.set_value("students", undo.get("before") or [], tid)
        elif undo.get("kind") == "combinations":
            with BK.SETUP_LOCK:
                if not setup_as_written(db, undo, tid):
                    raise ApplyError(409, SETUP_CHANGED)
                db.set_value("blocks_setup", undo.get("before"), tid)      # None: there was none
        elif undo.get("kind") == "levels":
            if not levels_as_written(db, undo, tid):
                raise ApplyError(409, LEVELS_CHANGED)
            db.set_value(LV.KEY, undo.get("before"), tid)                  # None: there were none
        else:
            if not plan_as_written(db.get_value("plan", tid), undo):
                raise ApplyError(409, PLAN_CHANGED)
            # an empty plan rather than none: the rev keeps moving forward
            db.set_value("plan", undo.get("plan_before") or M.empty_plan(), tid)
        back = {k: v for k, v in reading.items() if k not in ("undo", "applied", "applied_at")}
        _update(db, rid, {**back, "status": "waiting", "updated_at": time.time()}, tid)
    return {"ok": True}


def skip(db, rid: str, tid: str | None = None) -> dict:
    """Blocking. Reading `rid` set aside: its card stays, marked skipped, and its kept files go."""
    tid = tid or db.working_timetable()
    with db.plan_lock(tid):
        entry = _find(db, rid, tid)
        reading = entry["reading"]
        if reading.get("status") == "applied":
            raise ApplyError(409, "Already applied.")
        gone = inbox._paths(reading)
        # a skipped student list keeps its card, not its students
        _update(db, rid, {k: v for k, v in reading.items() if k != "students"} | {"status": "skipped"}, tid)
    for p in gone:
        inbox._unlink(p)
    return {"ok": True}
