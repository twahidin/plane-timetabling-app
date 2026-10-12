"""Apply a reading by section, and undo it (spec docs/superpowers/specs/2026-10-12-assistant-files-design.md
§3.3–3.4). Under the plan lock: the reading is merged into the plan as it is now and only the ticked
sections are taken; a timetable reading becomes the draft. Undo puts back what was there when nothing
has changed it since."""
from __future__ import annotations

import hashlib
import json
import time
from pathlib import Path

from ... import asc_import, journey
from ...intake import IntakeError
from .. import model as M
from ..issues import plan_issues
from . import inbox, readers, routes as RR, sections as S
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
    return inbox.view(_find(db, rid, tid))


def plan_status(db, tid: str | None = None) -> dict:
    """Where the plan stands, in a few plain lines: ready to build, or what blocks it."""
    plan = M.normalise(db.get_value("plan", tid) or M.empty_plan())
    issues = plan_issues(plan, db.get_org("live", tid) or None, db.get_settings(tid))
    blocks = [i.text for i in issues if i.level == "block"]
    return {"ready": not blocks and bool(plan["requirements"]), "blocks": blocks[:5], "block_count": len(blocks),
            "warn_count": sum(1 for i in issues if i.level == "warn")}


def plan_body(db, entry: dict, current: dict, tid: str | None) -> dict:
    """The reading as sections.merged takes it; an exact workbook merged again from its kept file, with the
    sizes CSVs given with it on top."""
    reading = {**entry["reading"], "file": entry.get("file")}
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
    return out


def _organisation(reading: dict, opts: dict) -> dict:
    """The draft a timetable reading makes: the model's organisation, or an aSc export read again from its
    kept files with the card's options."""
    if reading.get("organisation"):
        return reading["organisation"]
    try:
        datas = [Path(p).read_bytes() for p in reading.get("paths") or []]
    except OSError:
        raise ApplyError(409, "The file is gone: give it to the Assistant again.")
    try:
        return inbox.asc_org(datas, mode=opts["mode"], classes_as_planes=bool(opts["classes_as_planes"]))[0]
    except asc_import.AscFormatError as e:
        raise ApplyError(422, str(e))
    except IntakeError as e:
        raise ApplyError(422, f"The timetable export could not be turned into a draft: {e}")


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


def _apply_timetable(db, reading: dict, opts: dict, tid: str | None) -> dict:
    org = _organisation(reading, opts)
    before = db.get_org("draft", tid)
    db.set_org("draft", org, tid)
    return {"draft_before": before, "draft_after": _hash(db.get_org("draft", tid))}


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
        if view["kind"] == "timetable":
            opts = _options(reading, options)
            undo = {"kind": "timetable", **_apply_timetable(db, reading, opts, tid)}
            reading = {**reading, "options": opts}
        else:
            undo = {"kind": "plan", **_apply_plan(db, entry, names, tid)}
        applied = {n: int(secs[n].get("count") or 0) for n in names}
        entry = _update(db, rid, {**reading, "status": "applied", "applied_at": time.time(), "applied": applied,
                                  "undo": undo}, tid)
    extra = _saved_reader(db, entry) if view["kind"] != "timetable" else {}
    return {"ok": True, "applied": applied, "status": plan_status(db, tid), "reading": inbox.view(entry), **extra}


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
    return {"reading": inbox.view(entry)}


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
        else:
            now = db.get_value("plan", tid)
            # the plan this apply wrote: by its rev, or by its content when a later apply was undone since
            if (int((now or {}).get("rev") or 0) != undo.get("rev_after")
                    and (not undo.get("content_after") or _content(now) != undo["content_after"])):
                raise ApplyError(409, "The plan has changed since this was applied; undo left it as it is.")
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
        _update(db, rid, {**reading, "status": "skipped"}, tid)
    for p in gone:
        inbox._unlink(p)
    return {"ok": True}
