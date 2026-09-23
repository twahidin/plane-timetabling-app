"""Relief: absences, ranked cover candidates, cover cards, the fairness ledger and the dated overlay
(spec docs/superpowers/specs/2026-09-23-relief-agent-design.md §2–§4).

Everything is stored on the base timetable under `relief`, like bookings: a period timetable reads and
writes its base's record. Every write goes through `_mutate`, which reads, changes and saves the record
under one lock per base timetable, so two requests at once never lose each other's change; reads take
no lock. Covers never change an organisation; `overlay` swaps the covering teacher
in on a dated view only. Load and runs are counted here from the organisation dict (work slots are
events not at a rest location), so no engine call is needed to rank candidates."""
from __future__ import annotations

import copy
import hashlib
import re
import secrets
import threading
import time
from datetime import date as _date, timedelta

from . import calendar as cal, changes, names as names_mod, periods, proposals
from .db import THREAD
from .print import grid as grid_mod


class ReliefError(ValueError):
    pass


DEFAULT_SETTINGS = {"pool": [], "max_per_day": 2, "term_start": ""}
MAX_ABSENCE_DAYS = 31
_KEY = "relief"


# ---- the record -------------------------------------------------------------------------------

def _base(db, tid=None) -> str:
    return periods.base_tid(db, tid)


def _doc(db, tid=None) -> dict:
    v = db.get_value(_KEY, tid=_base(db, tid))
    v = v if isinstance(v, dict) else {}
    return {"absences": list(v.get("absences") or []), "covers": list(v.get("covers") or []),
            "settings": {**DEFAULT_SETTINGS, **(v.get("settings") or {})}}


def _save(db, doc: dict, base: str | None = None) -> None:
    db.set_value(_KEY, doc, tid=base or _base(db))


_LOCKS: dict[str, threading.Lock] = {}
_LOCKS_GUARD = threading.Lock()


def _lock_for(base: str) -> threading.Lock:
    """The lock of one base timetable's relief record."""
    with _LOCKS_GUARD:
        return _LOCKS.setdefault(base, threading.Lock())


def _mutate(db, fn):
    """Read the relief record, change it with `fn(doc)` and save it, under the base's lock; returns what
    `fn` returns. If `fn` raises, nothing is saved. The lock is a plain one: `fn` must not call another
    writer, so each writer checks what it is given (reads) before it gets here."""
    base = _base(db)                      # read once: a timetable switch mid-mutation must not move the save
    with _lock_for(base):
        doc = _doc(db, base)
        result = fn(doc)
        _save(db, doc, base)
        return result


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _parse(s, what="date") -> _date:
    try:
        return _date.fromisoformat(str(s).strip())
    except (TypeError, ValueError):
        raise ReliefError(f"{s!r} is not a {what} (YYYY-MM-DD)") from None


def iso_date(s) -> str:
    """`s` as an ISO date, or a ReliefError."""
    return _parse(s).isoformat()


def _base_org(db) -> dict | None:
    base = _base(db)
    return db.get_org("live", tid=base) or db.get_org("draft", tid=base)


def _person_ids(db) -> set[str]:
    return {p["id"] for p in (_base_org(db) or {}).get("persons") or []}


def _plan(db) -> dict | None:
    v = db.get_value("plan", tid=_base(db))
    return v if isinstance(v, dict) else None


def _base_settings(db) -> dict:
    return db.get_settings(tid=_base(db))


def _today() -> str:
    """Today (ISO). An absence that ended before today is past: its counts are stored, not re-read."""
    return _date.today().isoformat()


def _teacher_ids(db) -> set[str]:
    """The base timetable's teachers (a teacher role, or plan staff): the only persons an absence or
    the relief pool can name."""
    return {p["id"] for p in _teachers(_base_org(db) or {}, _plan(db))}


# ---- settings -----------------------------------------------------------------------------------

def settings(db) -> dict:
    return copy.deepcopy(_doc(db)["settings"])


def set_settings(db, patch: dict) -> dict:
    if not isinstance(patch, dict):
        raise ReliefError("relief settings must be an object")
    have_org = _base_org(db) is not None
    known = _teacher_ids(db)
    # A stored pool member who is no longer a teacher of the base timetable is dropped silently (they
    # are gone, and the page sends the stored pool back on every save); only a newly added id that is
    # not a teacher (a student, a group, no one) is refused. With no organisation to read (none built
    # or drafted yet) the stored pool is left as it is.
    upd: dict = {}
    if "pool" in patch:
        pool = patch["pool"] or []
        if not isinstance(pool, list):
            raise ReliefError("the relief pool is a list of person ids")
        stored = settings(db)["pool"]
        unknown = [str(p) for p in pool if str(p) not in known and str(p) not in stored]
        if unknown:
            raise ReliefError(f"not a teacher in the timetable: {', '.join(unknown)}")
        upd["pool"] = list(dict.fromkeys(str(p) for p in pool if str(p) in known or not have_org))
    if "max_per_day" in patch:
        try:
            n = int(patch["max_per_day"])
        except (TypeError, ValueError):
            raise ReliefError("max_per_day must be a whole number") from None
        if n < 1 or isinstance(patch["max_per_day"], bool):
            raise ReliefError("max_per_day must be at least 1")
        upd["max_per_day"] = n
    if "term_start" in patch:
        ts = str(patch["term_start"] or "").strip()
        upd["term_start"] = _parse(ts).isoformat() if ts else ""

    def change(doc):
        new = dict(doc["settings"])
        if have_org:
            new["pool"] = [p for p in new.get("pool") or [] if p in known]
        new.update(upd)
        doc["settings"] = new
        return copy.deepcopy(new)
    return _mutate(db, change)


def term_start(db) -> str:
    """The date the relief term starts (ISO), as the ledger and the fairness rank count from: the relief
    settings' term start, else the calendar's; "" when neither is set."""
    return settings(db)["term_start"] or str(_base_settings(db)["calendar"].get("term_start") or "")


# ---- absences -----------------------------------------------------------------------------------

def add_absence(db, person, date_from, date_to=None, slots=None, reason="") -> dict:
    """`slots` is `[first, one past last]` as offsets within the school day (0 = the day's first
    slot, a break counting like any slot: see `day_slots`), or None for the whole day. The person
    must be a teacher (a teacher role, or plan staff); an absence spans at most 31 days."""
    person = str(person or "").strip()
    if person not in _teacher_ids(db):
        if person in _person_ids(db):
            raise ReliefError(f"{person!r} is not a teacher: only a teacher's absence needs cover")
        raise ReliefError(f"{person!r} is not a person in the timetable")
    f = _parse(date_from)
    t = _parse(date_to) if date_to not in (None, "") else f
    if t < f:
        raise ReliefError("the absence ends before it starts")
    if (t - f).days + 1 > MAX_ABSENCE_DAYS:
        raise ReliefError(f"an absence can cover at most {MAX_ABSENCE_DAYS} days")
    if slots is not None:
        spd = int(_base_settings(db)["time"].get("slots_per_day") or 0)
        try:
            a, b = (int(x) for x in slots)
        except (TypeError, ValueError):
            raise ReliefError("slots are [first, one past last] within the day") from None
        if not (0 <= a < b and (not spd or b <= spd)):
            raise ReliefError(f"slots {a}–{b - 1} are not within a day of {spd} slots")
        slots = [a, b]
    rec = {"id": "abs-" + secrets.token_hex(3), "person": person, "from": f.isoformat(), "to": t.isoformat(),
           "slots": slots, "reason": str(reason or "").strip(), "created": _now()}
    _mutate(db, lambda doc: doc["absences"].append(rec))
    return rec


def list_absences(db) -> list[dict]:
    return _doc(db)["absences"]


def get_absence(db, aid) -> dict | None:
    return next((a for a in list_absences(db) if a["id"] == aid), None)


def remove_absence(db, aid) -> dict | None:
    """Remove an absence and every cover made for it."""
    def change(doc):
        rec = next((a for a in doc["absences"] if a["id"] == aid), None)
        if rec is not None:
            doc["absences"] = [a for a in doc["absences"] if a["id"] != aid]
            doc["covers"] = [c for c in doc["covers"] if c.get("absence") != aid]
        return rec
    return _mutate(db, change)


def day_labels(db) -> list[str]:
    """The labels of one school day's slots on the base timetable, without a day prefix ("P1", …,
    "Recess", …): an absence's part of the day is a range of offsets into this list."""
    t = _base_settings(db)["time"]
    labels = [str(x) for x in t.get("labels") or []]
    spd = int(t.get("slots_per_day") or len(labels) or 0)
    if not labels or spd <= 0:
        return [f"slot {i + 1}" for i in range(max(spd, 0))]
    first = grid_mod.day_slot_labels({"time_labels": labels, "rules": {"slots_per_day": spd}})[1][0]
    return first + [f"slot {i + 1}" for i in range(len(first), spd)]


def day_slots(db) -> list[dict]:
    """The slots an absence's part of the day can start or end at, as {slot: offset within the day,
    label}: every slot of the day but the live timetable's mandatory rest (a break), or every slot
    when there is no live timetable to read."""
    live = db.get_org("live", tid=_base(db))
    rest = {int(t) for t in ((live or {}).get("rules") or {}).get("mandatory_rest") or []}
    return [{"slot": i, "label": label} for i, label in enumerate(day_labels(db)) if i not in rest]


def slot_labels(db, slots, labels: list[str] | None = None) -> str:
    """An absence's part of the day in the day's labels ("P5 only", "P5 to P7"); "" for the whole day."""
    if not slots:
        return ""
    labels = day_labels(db) if labels is None else labels
    a, b = int(slots[0]), int(slots[1])

    def name(t):
        return labels[t] if 0 <= t < len(labels) else f"slot {t + 1}"
    return f"{name(a)} only" if b - a == 1 else f"{name(a)} to {name(b - 1)}"


def _store_counts(db, aid: str, left: int) -> None:
    """Keep an absence's lesson count (covered plus the `left` still to cover) and covered count on its
    record, with the day they were taken (`counted_on`), for the overview of an absence that has ended.
    The covered count is taken from the record under the lock, so a cover applied meanwhile counts."""
    def change(doc):
        covered = sum(1 for c in doc["covers"] if c.get("absence") == aid)
        for a in doc["absences"]:
            if a["id"] == aid:
                a.update(lessons=covered + left, covered=covered, counted_on=_today())
    _mutate(db, change)


def resolve_person(db, query) -> list[dict]:
    """The teacher `query` names, as a one-item list of `names.find` matches: a person id (any role),
    else the one teacher whose whole name it is, else the only teacher whose name contains it. Else
    every teacher that matches (none, or several for the user to choose from)."""
    q = str(query or "").strip()
    org = _base_org(db) or {}
    person = next((p for p in org.get("persons") or [] if p["id"] == q), None)
    if person is not None:
        return [{"id": person["id"], "name": person.get("name", person["id"]), "kind": "person",
                 "role": person.get("role", "")}]
    teachers = {**org, "persons": _teachers(org, _plan(db))}
    matches = names_mod.find(teachers, q, kinds=("person",))
    whole = [m for m in matches if m["name"].lower() == q.lower()]
    return whole if len(whole) == 1 else matches


def person_id(db, query) -> str:
    """The id of the teacher `query` names (see `resolve_person`), or a ReliefError saying who matched."""
    matches = resolve_person(db, query)
    if len(matches) == 1:
        return matches[0]["id"]
    if not matches:
        raise ReliefError(f"no teacher matching {str(query or '').strip()!r}")
    raise ReliefError(f"{str(query).strip()!r} matches several teachers: "
                      + ", ".join(f"{m['name']} ({m['id']})" for m in matches) + ". Say which.")


def _lessons_count(db, absence: dict, covered: int, cache: dict) -> int | None:
    """The lessons that needed cover: those covered plus those still to cover. An absence that has
    ended reads the count stored on it instead of reading its dates again, but only a count taken
    after it ended (`counted_on` later than its last day): one taken while it ran (at planning) may
    have changed since. Otherwise the dates are read once more, and after the end the count is stored
    for good. None when the dates cannot be read."""
    past = absence["to"] < _today()
    if past and absence.get("lessons") is not None and str(absence.get("counted_on") or "") > absence["to"]:
        return max(int(absence["lessons"]), covered)
    try:
        left = sum(1 for x in lessons_needing_cover(db, absence, cache) if not x["already_staffed"])
    except ReliefError:                   # the dates cannot be read now: an earlier count beats nothing
        return max(int(absence["lessons"]), covered) if absence.get("lessons") is not None else None
    if past:
        _store_counts(db, absence["id"], left)
    return covered + left


def absence_row(db, absence: dict, doc: dict | None = None, names: dict | None = None,
                cache: dict | None = None) -> dict:
    """One absence with the absent person's name, the covers applied for it (`covered`, and `covers`:
    each as {id, date, covering, text: "Tue 6 Oct P3 Maths, set A — Mr Tan"}, by day), the lessons
    that needed cover (`lessons`: those covered plus those still to cover; lessons another teacher
    still teaches are not counted; None when the dates cannot be read), the cover cards for it waiting
    in the chat (`pending`) and its part of the day in the day's labels (`slot_labels`, "" for the
    whole day). `cache` is shared by the rows of one overview (see `lessons_needing_cover`)."""
    doc = _doc(db) if doc is None else doc
    cache = {} if cache is None else cache
    if names is None:
        names = {p["id"]: p.get("name", p["id"]) for p in (_base_org(db) or {}).get("persons") or []}
    covered = sum(1 for c in doc["covers"] if c.get("absence") == absence["id"])
    if "pending" not in cache:
        cache["pending"] = [c for c in proposals.pending(db, THREAD) if c.get("kind") == "cover"]
    if "labels" not in cache:
        cache["labels"] = day_labels(db)
    aid = absence["id"]
    pending = sum(1 for c in cache["pending"]
                  if c.get("absence") == aid or any(x.get("absence") == aid for x in c.get("also_for") or []))
    mine = sorted((c for c in doc["covers"] if c.get("absence") == aid),
                  key=lambda c: (str(c.get("date") or ""), int(c.get("slot") or 0)))
    covers = [{"id": c["id"], "date": c.get("date"), "covering": c.get("covering"),
               "text": cover_line(cover_parts(db, c, cache))} for c in mine]
    return {**absence, "name": names.get(absence["person"], absence["person"]), "covered": covered,
            "lessons": _lessons_count(db, absence, covered, cache), "pending": pending,
            "slot_labels": slot_labels(db, absence.get("slots"), cache["labels"]), "covers": covers}


def absence_rows(db) -> list[dict]:
    """Every absence as `absence_row` gives it, reading each date's timetable once for all of them."""
    doc = _doc(db)
    names = {p["id"]: p.get("name", p["id"]) for p in (_base_org(db) or {}).get("persons") or []}
    cache: dict = {}
    return [absence_row(db, a, doc, names, cache) for a in doc["absences"]]


def pool_names(db) -> list[str]:
    names = {p["id"]: p.get("name", p["id"]) for p in (_base_org(db) or {}).get("persons") or []}
    return [names.get(p, p) for p in settings(db)["pool"]]


def _dates(absence: dict) -> list[str]:
    f, t = _parse(absence["from"]), _parse(absence["to"])
    return [(f + timedelta(days=i)).isoformat() for i in range((t - f).days + 1)]


def _absent_slots(absence: dict, date: str, rng: tuple[int, int]) -> set[int] | None:
    """The slots of the day `rng` the absence takes on `date`, None when it does not cover that date."""
    if not absence["from"] <= date <= absence["to"]:
        return None
    lo, hi = rng
    if absence.get("slots") is None:
        return set(range(lo, hi))
    a, b = absence["slots"]
    return set(range(lo + a, min(lo + b, hi)))


def absent_on(db, date, tid=None) -> list[str]:
    """Persons with an absence (whole or part day) covering `date`."""
    return sorted({a["person"] for a in _doc(db, tid)["absences"] if a["from"] <= str(date) <= a["to"]})


# ---- covers -------------------------------------------------------------------------------------

def covers_for(db, date, tid=None) -> list[dict]:
    return [c for c in _doc(db, tid)["covers"] if c.get("date") == date]


def add_cover(db, cover: dict) -> dict:
    """Record a cover (replacing one with the same id). The absent person is taken from its absence.
    The covering person must be in the timetable the cover names (a period may have staff of its
    own), else in the base's."""
    if not isinstance(cover, dict):
        raise ReliefError("a cover is an object")
    absence = get_absence(db, cover.get("absence"))
    if absence is None:
        raise ReliefError(f"no absence {cover.get('absence')!r}")
    covering = str(cover.get("covering") or "")
    try:
        own = db.get_org("live", tid=str(cover["timetable"])) if cover.get("timetable") else None
    except KeyError:                                   # the period was deleted since the card was made
        raise ReliefError(f"the timetable {cover['timetable']!r} no longer exists") from None
    known = {p["id"] for p in own.get("persons") or []} if own else _person_ids(db)
    if covering not in known:
        raise ReliefError(f"{covering!r} is not a person in the timetable")
    try:
        slot, dur = int(cover.get("slot")), int(cover.get("dur") or 1)
    except (TypeError, ValueError):
        raise ReliefError("a cover's slot and dur are whole numbers") from None
    rec = {"id": str(cover.get("id") or ("cov-" + secrets.token_hex(4))), "absence": absence["id"],
           "absent": absence["person"], "date": _parse(cover.get("date")).isoformat(),
           "timetable": str(cover.get("timetable") or _base(db)), "event": str(cover.get("event") or ""),
           "slot": slot, "dur": dur, "covering": covering, "applied": cover.get("applied") or _now()}

    def change(doc):
        if not any(a["id"] == rec["absence"] for a in doc["absences"]):
            raise ReliefError(f"no absence {rec['absence']!r}")        # removed while the cover was checked
        doc["covers"] = [c for c in doc["covers"] if c["id"] != rec["id"]] + [rec]
        _recount(doc, rec["absence"])
    _mutate(db, change)
    return rec


def _recount(doc: dict, aid) -> None:
    """Set the covered count on the absence `aid` from the covers of `doc`."""
    for a in doc["absences"]:
        if a["id"] == aid:
            a["covered"] = sum(1 for c in doc["covers"] if c.get("absence") == aid)


def list_covers(db) -> list[dict]:
    return _doc(db)["covers"]


def get_cover(db, cid) -> dict | None:
    return next((c for c in list_covers(db) if c["id"] == cid), None)


def remove_cover(db, cid, before_persist=None) -> dict | None:
    """Remove the cover `cid`, returning it (None when there is none). `before_persist(cover)`, if
    given, runs under the lock before the removal is saved (the change log's entry for it): if it
    raises, nothing is removed."""
    def change(doc):
        rec = next((c for c in doc["covers"] if c["id"] == cid), None)
        if rec is not None:
            if before_persist is not None:
                before_persist(rec)
            doc["covers"] = [c for c in doc["covers"] if c["id"] != cid]
            _recount(doc, rec.get("absence"))
        return rec
    return _mutate(db, change)


def restore_cover(db, cover: dict) -> dict | None:
    """Put back a cover exactly as it was (undoing its removal). The change log restores, it does not
    decide again: the covering teacher is not checked. Nothing is put back (None) when a cover with its
    id is already there, or its absence has been removed since (it would cover no one's lesson)."""
    rec = dict(cover)

    def change(doc):
        if any(c["id"] == rec.get("id") for c in doc["covers"]) \
                or not any(a["id"] == rec.get("absence") for a in doc["absences"]):
            return None
        doc["covers"].append(rec)
        _recount(doc, rec["absence"])
        return rec
    return _mutate(db, change)


def withdraw_cover(db, cid) -> dict | None:
    """Take one applied cover back (the Relief card's Remove, the chat's cover_remove), logging it
    first so Undo puts it back: "cover removed: Ms Lee's Maths, set A on Tue 6 Oct P3 by Mr Tan".
    Returns the removed cover, or None when there is none."""
    def log(rec):
        changes.record(db, "cover_removed", cover_removed_text(cover_parts(db, rec)), cover=rec)
    return remove_cover(db, cid, before_persist=log)


def cover_parts(db, cover: dict, cache: dict | None = None) -> dict:
    """The words for an applied cover, read from the timetable it names (else the base's): the absent
    and covering teachers' names, the lesson and when ("Tue 6 Oct P3"). `cache` is shared by one read
    of several covers."""
    cache = {} if cache is None else cache
    tid = str(cover.get("timetable") or _base(db))

    def live():
        try:
            return db.get_org("live", tid=tid)
        except KeyError:                               # the period was deleted since
            return None
    org = _cached(cache, ("live", tid), live) or _cached(cache, "base_org", lambda: _base_org(db)) or {}
    events = _cached(cache, ("events", tid), lambda: {e["id"]: e for e in org.get("events") or []})
    names = _cached(cache, ("names", tid), lambda: {p["id"]: p.get("name", p["id"]) for p in
                                                   ((_base_org(db) or {}).get("persons") or []) + (org.get("persons") or [])})
    e = events.get(cover.get("event"))
    lesson = _COVER_SUFFIX.sub("", str(e.get("name") or e["id"])) if e else str(cover.get("event") or "a lesson")
    st = _cached(cache, "settings", lambda: _base_settings(db))
    when = _when(db, org, str(cover.get("date")), int(cover.get("slot") or 0), int(cover.get("dur") or 1), st)
    return {"absent_name": names.get(cover.get("absent"), cover.get("absent") or "the absent teacher"),
            "lesson": lesson, "when": when, "covering_name": names.get(cover.get("covering"), cover.get("covering"))}


def cover_line(parts: dict) -> str:
    """One cover on the Relief card: "Tue 6 Oct P3 Maths, set A — Mr Tan"."""
    return f"{parts['when']} {parts['lesson']} — {parts['covering_name']}"


def cover_removed_text(parts: dict) -> str:
    return f"cover removed: {parts['absent_name']}'s {parts['lesson']} on {parts['when']} by {parts['covering_name']}"


def ledger(db, since=None) -> list[dict]:
    """Covers per covering person since `since` (default: the relief term start, else the calendar's),
    most first, then by name."""
    since = term_start(db) if since is None else (_parse(since).isoformat() if str(since).strip() else "")
    names = {p["id"]: p.get("name", p["id"]) for p in (_base_org(db) or {}).get("persons") or []}
    by: dict[str, list[str]] = {}
    for c in _doc(db)["covers"]:
        if c.get("date", "") >= since:
            by.setdefault(c["covering"], []).append(c["date"])
    rows = [{"person": p, "name": names.get(p, p), "count": len(ds), "dates": sorted(ds)} for p, ds in by.items()]
    return sorted(rows, key=lambda r: (-r["count"], r["name"]))


# ---- overlay ------------------------------------------------------------------------------------

def overlay(org: dict, covers: list[dict], absent=()) -> dict:
    """A deep copy of `org` with each cover applied: on the covered event, the absent person is
    replaced by the covering one and the name gains " (cover for <absent name>)". A cover whose event
    is gone or has moved off the cover's slot is not applied. Events of `absent` persons with no cover
    keep their members: the teacher is simply away. `absent` names the absent persons of the day, used
    for a cover record that does not carry its absent person."""
    out = copy.deepcopy(org)
    names = {p["id"]: p.get("name", p["id"]) for p in out.get("persons") or []}
    by_id = {e["id"]: e for e in out.get("events") or []}
    absent = set(absent or ())
    for c in covers:
        e = by_id.get(c.get("event"))
        if e is None or (c.get("slot") is not None and e.get("t0") != c["slot"]):
            continue
        members = list(e.get("members") or [])
        who = c.get("absent") or next((m for m in members if m in absent), None)
        if who not in members:
            continue
        new = []
        for m in members:
            m = c["covering"] if m == who else m
            if m not in new:
                new.append(m)
        e["members"] = new
        e["name"] = f"{e.get('name', e['id'])} (cover for {names.get(who, who)})"
    return out


# ---- subjects -----------------------------------------------------------------------------------

def _req_index(plan: dict | None) -> dict[str, dict]:
    """Event id -> the plan requirement it was generated from (the generator's own id scheme)."""
    if not plan:
        return {}
    from .plan.generate import _fit_id
    out = {}
    for r in plan.get("requirements") or []:
        for k, count in (r.get("lessons") or {}).items():
            for i in range(int(count or 0)):
                out[_fit_id(r["id"], str(k), str(i))] = r
    return out


def _subject(event: dict, index: dict[str, dict]) -> str:
    r = index.get(event.get("id"))
    if r is not None and (r.get("subject") or r.get("dept")):
        return str(r.get("subject") or r.get("dept"))
    name = str(event.get("name") or "").strip()
    parts = [p.strip() for p in name.split("·")]
    if len(parts) >= 3 and parts[1]:
        return parts[1]
    first = name.split()[0] if name.split() else ""
    return re.sub(r"[^\w&/+-]+$", "", first)


def subject_of_event(org: dict, plan: dict | None, event) -> str:
    """The plan requirement's subject (else dept) when the event came from the plan; else the middle
    part of a "classes · subject · grouping" name; else the first word of the name."""
    if not isinstance(event, dict):
        event = next((e for e in org.get("events") or [] if e["id"] == event), {"id": str(event), "name": ""})
    return _subject(event, _req_index(plan))


_COVER_SUFFIX = re.compile(r" \(cover for [^()]*\)$")


def _is_covered(e: dict) -> bool:
    """An event the dated overlay has given to a covering teacher (its name carries the suffix)."""
    return bool(_COVER_SUFFIX.search(str(e.get("name") or "")))


def _rest_locs(org: dict) -> set[str]:
    return {l["id"] for l in org.get("locations") or [] if l.get("rest")}


def _is_rest_event(e: dict, rest_locs: set[str]) -> bool:
    return str(e.get("id", "")).startswith("rest-") or (e.get("loc") is not None and e.get("loc") in rest_locs)


def _subjects_by_person(org: dict, plan: dict | None) -> dict[str, set[str]]:
    """Each person's subjects: the plan staff entry's dept, the subject in a "Teacher, <subject>" role,
    and the subjects of the (movable, non-rest) lessons they teach. A covered lesson in a dated view
    credits no one: covering it does not make the subject the covering teacher's. One pass over the
    events."""
    out: dict[str, set[str]] = {}
    for s in (plan or {}).get("staff") or []:
        if s.get("id") and s.get("dept"):
            out.setdefault(s["id"], set()).add(str(s["dept"]))
    for p in org.get("persons") or []:
        role = str(p.get("role") or "")
        if role.lower().startswith("teacher") and "," in role and role.split(",", 1)[1].strip():
            out.setdefault(p["id"], set()).add(role.split(",", 1)[1].strip())
    index, rest = _req_index(plan), _rest_locs(org)
    for e in org.get("events") or []:
        if e.get("fixed") or _is_rest_event(e, rest) or _is_covered(e):
            continue
        s = _subject(e, index)
        if s:
            for m in e.get("members") or []:
                out.setdefault(m, set()).add(s)
    return out


def subjects_of_person(org: dict, plan: dict | None, pid: str) -> set[str]:
    """The plan staff entry's dept, the subject in a "Teacher, <subject>" role, and the subjects of the
    lessons the person teaches."""
    return set(_subjects_by_person(org, plan).get(pid, set()))


# ---- the day ------------------------------------------------------------------------------------

def _day(db, date) -> tuple[int, int]:
    s = _base_settings(db)
    rng = cal.slot_range(s["calendar"], s["time"], str(date))
    if rng is None:
        raise ReliefError(cal.describe(s["calendar"], s["time"], str(date)))
    return rng


def _teachers(org: dict, plan: dict | None) -> list[dict]:
    staff = {s.get("id") for s in (plan or {}).get("staff") or []}
    return [p for p in org.get("persons") or []
            if str(p.get("role") or "").lower().startswith("teacher") or p["id"] in staff]


def _busy_slots(org: dict, rng: tuple[int, int]) -> dict[str, set[int]]:
    """Person id -> the slots of the day `rng` in which they work (rest tiles and rest-location events
    excluded). One pass over the events."""
    lo, hi = rng
    rest = _rest_locs(org)
    out: dict[str, set[int]] = {}
    for e in org.get("events") or []:
        if e.get("t0") is None or _is_rest_event(e, rest):
            continue
        slots = {t for t in range(e["t0"], e["t0"] + int(e.get("dur") or 1)) if lo <= t < hi}
        if slots:
            for m in e.get("members") or []:
                out.setdefault(m, set()).update(slots)
    return out


def _longest_run(slots: set[int]) -> int:
    best = run = 0
    prev = None
    for t in sorted(slots):
        run = run + 1 if prev is not None and t == prev + 1 else 1
        best = max(best, run)
        prev = t
    return best


def _available(person: dict, slots: set[int]) -> bool:
    avail = person.get("avail")
    if not avail:
        return True
    if avail and not isinstance(avail[0], (list, tuple)):
        avail = [avail]
    return all(any(a <= t < b for a, b in avail) for t in slots)


def _rules(db, org: dict) -> tuple[int, int]:
    r = {**(_base_settings(db).get("rules") or {}), **(org.get("rules") or {})}
    return int(r.get("max_load") or 10 ** 6), int(r.get("max_run") or 10 ** 6)


def _plural(n: int, word: str) -> str:
    return f"{n} {word}{'' if n == 1 else 's'}"


# ---- candidates ---------------------------------------------------------------------------------

def _assess(db, date, event, org, proposed=()) -> tuple[list[dict], list[dict]]:
    """(ranked candidates, misses) for covering `event` on `date` in `org`. `proposed` are covers not
    yet applied (cards of the same plan): their covering persons are busy at those slots and count
    toward the daily limit, like the applied covers of the day, and they count toward the term's
    covers for ranking (so one teacher does not top every day of a long absence), though the reason
    line still counts applied covers only."""
    date = _parse(date).isoformat()
    rng = _day(db, date)
    e = event if isinstance(event, dict) else next((x for x in org.get("events") or [] if x["id"] == event), None)
    if e is None or e.get("t0") is None:
        raise ReliefError(f"no placed lesson {event!r} in the timetable in force on {date}")
    lesson = set(range(e["t0"], e["t0"] + int(e.get("dur") or 1)))
    plan = _plan(db)
    st = settings(db)
    pool, max_per_day = set(st["pool"]), int(st["max_per_day"])
    max_load, max_run = _rules(db, org)
    subject = _subject(e, _req_index(plan)).lower()
    subjects = _subjects_by_person(org, plan)
    busy_of = _busy_slots(org, rng)
    since = term_start(db)
    doc = _doc(db)
    absences = [a for a in doc["absences"] if a["from"] <= date <= a["to"]]
    day_covers = [c for c in doc["covers"] if c.get("date") == date] + \
                 [c for c in proposed or () if isinstance(c, dict) and c.get("date", date) == date]
    term_counts: dict[str, int] = {}
    for c in doc["covers"]:
        if c.get("date", "") >= since:
            term_counts[c["covering"]] = term_counts.get(c["covering"], 0) + 1
    staged: dict[str, int] = {}
    for c in proposed or ():
        if isinstance(c, dict) and c.get("covering") and c.get("date", date) >= since:
            staged[c["covering"]] = staged.get(c["covering"], 0) + 1

    cands, misses = [], []
    for p in _teachers(org, plan):
        pid = p["id"]
        if pid in (e.get("members") or []):
            continue

        def miss(why, rank):
            misses.append({"person": pid, "name": p.get("name", pid), "why": why, "rank": rank})

        if any((s := _absent_slots(a, date, rng)) and s & lesson for a in absences if a["person"] == pid):
            miss("away", 4)
            continue
        if not _available(p, lesson):
            miss("not available then", 3)
            continue
        busy = busy_of.get(pid, set())
        mine = [c for c in day_covers if c.get("covering") == pid]
        cover_slots = {t for c in mine for t in range(int(c["slot"]), int(c["slot"]) + int(c.get("dur") or 1))
                       if rng[0] <= t < rng[1]}
        # In a dated view an applied cover is one of the covering teacher's events too: busy then
        # because of a cover is "covering another lesson", not "teaching".
        if (busy - cover_slots) & lesson:
            miss("teaching", 2)
            continue
        if cover_slots & lesson:
            miss("covering another lesson", 1)
            continue
        if len(mine) >= max_per_day:
            miss("over the daily limit", 0)
            continue
        work = busy | cover_slots
        if len(work | lesson) > max_load:
            miss(f"over the load limit ({len(work | lesson)} of {max_load})", 0)
            continue
        if _longest_run(work | lesson) > max_run:
            miss(f"over the run limit ({_longest_run(work | lesson)} in a row)", 0)
            continue
        in_pool = pid in pool
        same = bool(subject) and subject in {s.lower() for s in subjects.get(pid, ())}
        n = term_counts.get(pid, 0)
        reasons = (["in pool"] if in_pool else []) + (["same subject"] if same else []) + [f"{_plural(n, 'cover')} this term"]
        cands.append({"person": pid, "name": p.get("name", pid), "reasons": reasons, "in_pool": in_pool,
                      "same_subject": same, "covers": n, "day_load": len(work)})
    cands.sort(key=lambda c: (not c["in_pool"], not c["same_subject"], c["covers"] + staged.get(c["person"], 0),
                              c["day_load"], c["person"]))
    misses.sort(key=lambda m: (m["rank"], m["name"]))
    return cands, [{k: v for k, v in m.items() if k != "rank"} for m in misses]


def candidates(db, date, event, org, *, exclude=()) -> list[dict]:
    """Teachers who can cover `event` (an id or event dict of `org`) on `date`, best first: in the
    pool, same subject, fewest covers since the term start, lightest day, then id. `exclude` are
    covers proposed but not applied (cover-like dicts with covering, slot, dur and date), which count
    as busy slots and toward the daily limit."""
    return _assess(db, date, event, org, exclude)[0]


# ---- lessons and the plan -----------------------------------------------------------------------

def _cached(cache: dict, key, make):
    if key not in cache:
        cache[key] = make()
    return cache[key]


def lessons_needing_cover(db, absence: dict, cache: dict | None = None) -> list[dict]:
    """Each lesson of the absent person on each teaching date of the absence, in the timetable in
    force that date (within the absence's slots when given). Rest tiles and events with no one but
    teachers in them (meetings, duties) are skipped; a lesson another present teacher also teaches
    is marked `already_staffed`. A lesson already covered no longer has the absent person in the
    dated view, so it is not listed again. `cache` is a dict kept for one read that changes nothing
    (an overview, a plan): each date's timetable in force, the settings, the plan and the absences
    are read once for every absence given it."""
    cache = {} if cache is None else cache
    pid = absence["person"]
    plan = _cached(cache, "plan", lambda: _plan(db))
    st = _cached(cache, "settings", lambda: _base_settings(db))     # not `s`: the walrus below binds that name
    all_absences = _cached(cache, "absences", lambda: _doc(db)["absences"])
    out = []
    for date in _dates(absence):
        rng = cal.slot_range(st["calendar"], st["time"], date)
        if rng is None:
            continue                                   # a weekend, a holiday, before the term
        tid, org = _cached(cache, ("org", date), lambda: periods.org_for(db, date))
        if org is None:
            continue
        away = _absent_slots(absence, date, rng) or set()
        teachers = {p["id"] for p in _teachers(org, plan)}
        rest = _rest_locs(org)
        others_away = [a for a in all_absences if a["person"] != pid and a["from"] <= date <= a["to"]]
        for e in sorted(org.get("events") or [], key=lambda x: (x.get("t0") or 0, x["id"])):
            members = e.get("members") or []
            if pid not in members or e.get("t0") is None or _is_rest_event(e, rest):
                continue
            slots = set(range(e["t0"], e["t0"] + int(e.get("dur") or 1)))
            if not (rng[0] <= e["t0"] < rng[1]) or not slots & away:
                continue
            if all(m in teachers for m in members):
                continue
            present = [m for m in members if m != pid and m in teachers
                       and not any((s := _absent_slots(a, date, rng)) and s & slots for a in others_away if a["person"] == m)]
            out.append({"date": date, "timetable": tid, "event": e["id"], "name": e.get("name", e["id"]),
                        "slot": e["t0"], "dur": int(e.get("dur") or 1), "already_staffed": bool(present)})
    return out


def _card_id(absence_id: str, date: str, event: str) -> str:
    return "cov-" + hashlib.sha256(f"{absence_id}|{date}|{event}".encode()).hexdigest()[:10]


def _when(db, org: dict, date: str, slot: int, dur: int, settings_: dict | None = None) -> str:
    s = _base_settings(db) if settings_ is None else settings_
    labels = org.get("time_labels") or s["time"].get("labels") or []
    day = cal.describe(s["calendar"], s["time"], date).split(":")[0]

    def label(t):
        return labels[t] if 0 <= t < len(labels) else f"slot {t}"
    return f"{day} {label(slot)}" + (f"–{label(slot + dur - 1)}" if dur > 1 else "")


def _fold(db, card: dict, absence: dict) -> None:
    """Mark `card` as covering its lesson for `absence` too (a co-taught lesson with both away)."""
    if any(x.get("absence") == absence["id"] for x in card.get("also_for") or []):
        return
    who = periods.org_for(db, card["date"])[1] or {}
    name = next((p.get("name", p["id"]) for p in who.get("persons") or [] if p["id"] == absence["person"]),
                absence["person"])
    card["also_for"] = list(card.get("also_for") or []) + [{"absence": absence["id"], "absent": absence["person"]}]
    card["text"] = f"{card['text']} {name} is away then too: this card covers the lesson for both."


def plan(db, absence_id, choices=None, session_id="", run="") -> list[dict]:
    """Stage one cover card per lesson of the absence that needs one (already-staffed lessons get no
    card). Each card preselects the best candidate, or the one `choices` names for its event (which
    must be one of those lessons, and the person a candidate); a lesson with no candidate is an
    `uncovered` card naming the nearest misses. The pending cover cards of other absences are kept
    and their covers count as proposed, so two absences planned one after the other do not give one
    teacher two lessons at once; this absence's earlier cards, and any other kind of card, are
    replaced. A co-taught lesson another absence's card already carries gets no second card: that
    card is marked `also_for` this absence instead; and a card this absence makes for a lesson another
    live absence also needs, with no card of its own for it, is marked `also_for` that absence (so
    replanning the absence that holds a merged card keeps the other's note). Returns the whole
    pending list as stored, this absence's cards first (so a plain "yes" applies the first of them)."""
    absence = get_absence(db, absence_id)
    if absence is None:
        raise ReliefError(f"no absence {absence_id!r}")
    choices = dict(choices or {})
    cache: dict = {}                                   # nothing is written until the cards are staged
    lessons = [x for x in lessons_needing_cover(db, absence, cache) if not x["already_staffed"]]
    unknown = sorted(set(map(str, choices)) - {x["event"] for x in lessons})
    if unknown:
        raise ReliefError(f"not a lesson of this absence that needs cover: {', '.join(unknown)}")
    live_absences = {a["id"] for a in list_absences(db)}
    kept = [c for c in proposals.pending(db, session_id) if c.get("kind") == "cover"
            and c.get("absence") != absence["id"] and c.get("absence") in live_absences]
    orgs: dict[str, dict] = {}
    proposed: list[dict] = [{"date": c["date"], "slot": c["slot"], "dur": c["dur"], "covering": c["covering"]}
                            for c in kept if c.get("covering")]
    by_lesson = {(c["date"], c["event"]): c for c in kept}
    dates = set(_dates(absence))
    shared: dict[tuple[str, str], list[dict]] = {}     # (date, event) -> other absences that need the lesson too
    for other in list_absences(db):
        if other["id"] == absence["id"] or other["to"] < absence["from"] or other["from"] > absence["to"]:
            continue
        for x in lessons_needing_cover(db, other, cache):
            if x["date"] in dates and not x["already_staffed"]:
                shared.setdefault((x["date"], x["event"]), []).append(other)
    cards = []
    for lesson in lessons:
        date = lesson["date"]
        held = by_lesson.get((date, lesson["event"]))
        if held is not None:                           # co-taught, and the other teacher's absence already has a card
            _fold(db, held, absence)
            continue
        if date not in orgs:
            orgs[date] = periods.org_for(db, date)[1]
        org = orgs[date]
        names = {p["id"]: p.get("name", p["id"]) for p in org.get("persons") or []}
        cands, misses = _assess(db, date, lesson["event"], org, proposed)
        chosen = None
        if lesson["event"] in choices:
            want = str(choices[lesson["event"]])
            chosen = next((c for c in cands if c["person"] == want), None)
            if chosen is None:
                why = next((m["why"] for m in misses if m["person"] == want), "not a teacher who can take it")
                raise ReliefError(f"{names.get(want, want)} cannot cover {lesson['name']} on {date}: {why}")
        elif cands:
            chosen = cands[0]
        absent_name = names.get(absence["person"], absence["person"])
        when = _when(db, org, date, lesson["slot"], lesson["dur"])
        if chosen is not None:
            proposed.append({"date": date, "slot": lesson["slot"], "dur": lesson["dur"], "covering": chosen["person"]})
            others = [c["name"] for c in cands if c["person"] != chosen["person"]][:2]
            text = (f"Cover {absent_name}'s {lesson['name']} on {when} with {chosen['name']} "
                    f"({' · '.join(chosen['reasons'])})." + (f" Also free: {', '.join(others)}." if others else ""))
        else:
            near = "; ".join(f"{m['name']} is {m['why']}" for m in misses[:3]) or "there is no other teacher"
            text = f"No cover for {absent_name}'s {lesson['name']} on {when} — {near}."
        top = cands[:3]
        if chosen is not None and chosen not in top:
            top = [chosen] + cands[:2]
        cards.append({"id": _card_id(absence["id"], date, lesson["event"]), "kind": "cover", "absence": absence["id"],
                      "absent": absence["person"], "date": date, "timetable": lesson["timetable"],
                      "event": lesson["event"], "slot": lesson["slot"], "dur": lesson["dur"],
                      "covering": chosen["person"] if chosen else None, "candidates": top,
                      "uncovered": chosen is None, "text": text, "review": [],
                      "absent_name": absent_name, "lesson": lesson["name"], "when": when})
        for other in shared.get((date, lesson["event"]), ()):
            _fold(db, cards[-1], other)
    _store_counts(db, absence["id"], len(lessons))
    return proposals.set_pending(db, cards + kept, session_id, run)


# ---- applying a card ----------------------------------------------------------------------------

def cover_text(card: dict) -> str:
    """The change log's words for an applied cover card: "cover: Ms Lee's Maths, set A on Tue 6 Oct P3
    by Mr Tan" (a card staged before cards carried their parts keeps its own text)."""
    who = next((c.get("name") for c in card.get("candidates") or [] if c.get("person") == card.get("covering")),
               card.get("covering"))
    if not all(card.get(k) for k in ("absent_name", "lesson", "when")):
        return str(card.get("text") or f"cover by {who}")
    return f"cover: {card['absent_name']}'s {card['lesson']} on {card['when']} by {who}"


def cover_of(card: dict) -> dict:
    """The cover record a staged card makes (the card id is the cover id)."""
    return {k: card.get(k) for k in ("id", "absence", "date", "timetable", "event", "slot", "dur", "covering")}


def refusal(db, card: dict) -> str | None:
    """Why a staged cover card cannot be applied now, or None. No engine check: a cover changes no
    organisation, so what can go wrong is the card itself (uncovered), the lesson (already covered,
    moved, gone) or the covering teacher, who must still be a candidate."""
    if not card.get("covering") or card.get("uncovered"):
        return "There is no cover to apply: no teacher can take this lesson."
    if get_absence(db, card.get("absence")) is None:
        return "That absence has been removed."
    date, eid = str(card.get("date")), card.get("event")
    if any(c.get("event") == eid for c in covers_for(db, date)):
        return f"That lesson is already covered on {date}."
    try:
        tid, org = periods.org_for(db, date)
    except ValueError as ex:
        return str(ex)
    if org is None:
        return periods.no_live_message(db, tid, "There is no live timetable yet.")
    e = next((x for x in org.get("events") or [] if x["id"] == eid), None)
    if e is None or tid != card.get("timetable") or e.get("t0") != card.get("slot") \
            or card.get("absent") not in (e.get("members") or []):
        return proposals.STALE
    try:
        cands, misses = _assess(db, date, e, org)
    except ReliefError as ex:
        return str(ex)
    who = card["covering"]
    if any(c["person"] == who for c in cands):
        return None
    names = {p["id"]: p.get("name", p["id"]) for p in org.get("persons") or []}
    why = next((m["why"] for m in misses if m["person"] == who), "not a teacher who can take it")
    return f"{names.get(who, who)} is no longer free then: {why}."
