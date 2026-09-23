"""Period timetables: a timetable instance in force for a date range and a scope, carved from its
base (spec docs/superpowers/specs/2026-09-23-period-timetables-design.md)."""
from __future__ import annotations

import calendar
import copy
import re
import secrets
import time
from datetime import date as _date

from . import bookings, changes


class PeriodError(ValueError):
    """A request the periods cannot take; `status` is the HTTP status the routes answer with
    (400, or 409 when it overlaps another period)."""

    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status


# ---- scope ----------------------------------------------------------------

def level_of(code: str) -> str:
    """The level of a class: the leading digits of its code ("4A1" -> "4"), "" when there are none."""
    m = re.match(r"\d+", str(code or "").strip())
    return m.group(0) if m else ""


def normalise_scope(scope) -> dict:
    """`{"all": True}` or `{"levels": [...], "classes": [...]}`, lower-cased, deduped and sorted."""
    if not isinstance(scope, dict):
        raise PeriodError("scope must be an object")
    if scope.get("all"):
        return {"all": True}
    levels = sorted({str(x).strip().lower() for x in (scope.get("levels") or []) if str(x).strip()})
    classes = sorted({str(x).strip().lower() for x in (scope.get("classes") or []) if str(x).strip()})
    if not levels and not classes:
        raise PeriodError("a period needs a scope: the whole school, some levels or some classes")
    return {"levels": levels, "classes": classes}


def scopes_intersect(a: dict, b: dict) -> bool:
    if a.get("all") or b.get("all"):
        return True
    al, ac = set(a.get("levels") or []), set(a.get("classes") or [])
    bl, bc = set(b.get("levels") or []), set(b.get("classes") or [])
    if al & bl or ac & bc:
        return True
    return any(level_of(c) in bl for c in ac) or any(level_of(c) in al for c in bc)


def _class_in_scope(cls: str, scope: dict) -> bool:
    c = str(cls).strip().lower()
    return bool(scope.get("all")) or c in (scope.get("classes") or []) or level_of(c) in (scope.get("levels") or [])


def in_scope(event: dict, org: dict, scope: dict) -> bool:
    """Whole school: every non-fixed event. Otherwise: any member is a group drawing from a class in
    scope. Fixed events (assemblies, bookings, rest tiles) are never in scope."""
    if event.get("fixed"):
        return False
    if scope.get("all"):
        return True
    groups = {g["id"]: g for g in org.get("groups") or []}
    for m in event.get("members") or []:
        g = groups.get(m)
        if g and any(_class_in_scope(c, scope) for c in g.get("classes") or []):
            return True
    return False


def _is_rest(e: dict) -> bool:
    return str(e.get("id", "")).startswith("rest-")


def carve(base: dict, scope: dict) -> dict:
    """The period's draft from its base: rest tiles and in-scope lessons dropped, every other lesson
    fixed where it is (an unplaced one is dropped too), the base's fixed events kept as they are.
    Every copied event carries `from_base: True`. Persons, locations, groups and bands are copied
    unchanged."""
    out = copy.deepcopy(base)
    kept = []
    for e in out.get("events") or []:
        if _is_rest(e):
            continue
        if not e.get("fixed"):
            if in_scope(e, base, scope) or e.get("t0") is None or e.get("loc") is None:
                continue
            e["fixed"] = True
        e["from_base"] = True
        kept.append(e)
    out["events"] = kept
    return out


def _dropped(source: dict, carved: dict) -> int:
    return sum(1 for e in source.get("events") or [] if not _is_rest(e)) - len(carved["events"])


def _carved_ids(org: dict) -> list[str]:
    return sorted(str(e["id"]) for e in org["events"] if e.get("from_base"))


# ---- records ----------------------------------------------------------------

def _parse(s) -> _date:
    try:
        return _date.fromisoformat(str(s).strip())
    except (TypeError, ValueError):
        raise PeriodError(f"{s!r} is not a date (YYYY-MM-DD)") from None


def base_tid(db, tid=None) -> str:
    """The base of a period timetable, else the timetable itself."""
    tid = tid or db.current_timetable()
    return db.get_value("period_base", tid=tid) or tid


def _records(db, base) -> list[dict]:
    return list((db.get_value("periods", tid=base) or {}).get("items") or [])


def _save(db, base, items) -> None:
    db.set_value("periods", {"items": items} if items else None, tid=base)


def list_for(db, tid=None) -> list[dict]:
    return _records(db, base_tid(db, tid))


def get(db, pid, tid=None) -> dict | None:
    return next((p for p in list_for(db, tid) if p["id"] == pid), None)


def _check_overlap(items, date_from, date_to, scope, ignore=None) -> None:
    for p in items:
        if p["id"] == ignore:
            continue
        if p["from"] <= date_to and date_from <= p["to"] and scopes_intersect(scope, p["scope"]):
            raise PeriodError(f"overlaps {p['name']} ({p['from']} to {p['to']}) for the same classes", status=409)


def create(db, name, date_from, date_to, scope) -> dict:
    """Create the period's timetable instance from the selected timetable (its base), write the
    record on the base, and re-select the base. Returns the record, which carries `dropped` (the
    base's lessons in scope, or unplaced, that the period timetable does not carry) and `carved`
    (the ids of the copies taken from the base: the engine drops the events' `from_base` marker on
    every build, so refresh needs this list to tell the copies from the period's own events)."""
    name = str(name or "").strip()
    if not name:
        raise PeriodError("give the period a name")
    base = db.current_timetable()
    if db.get_value("period_base", tid=base):
        raise PeriodError("a period timetable cannot have periods of its own; open the normal timetable first")
    f, t = _parse(date_from), _parse(date_to)
    if f > t:
        raise PeriodError("the period ends before it starts")
    scope = normalise_scope(scope)
    items = _records(db, base)
    _check_overlap(items, f.isoformat(), t.isoformat(), scope)
    source = db.get_org("live", tid=base) or db.get_org("draft", tid=base)
    if source is None:
        raise PeriodError("build the normal timetable first")
    draft = carve(source, scope)
    draft["name"] = name
    settings = db.get_settings(tid=base)
    tid = db.create_timetable(name)                 # selects the new one
    rec = {"id": "per-" + secrets.token_hex(3), "timetable": tid, "name": name, "from": f.isoformat(), "to": t.isoformat(),
           "scope": scope, "created": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "dropped": _dropped(source, draft), "carved": _carved_ids(draft)}
    try:
        db.set_settings(settings, tid=tid)
        db.set_value("period_base", base, tid=tid)
        db.set_org("draft", draft, tid=tid)
        _save(db, base, items + [rec])
    except BaseException:
        try:
            db.delete_timetable(tid)                    # never leave a half-made instance behind
        finally:
            db.select_timetable(base)
        raise
    db.select_timetable(base)
    return rec


def update(db, pid, base=None, **fields) -> dict:
    """Change a period's name, dates or scope; a scope change re-carves like refresh. `base` names
    the base timetable when it is not the selected one's (the header renaming a period instance)."""
    base = base or base_tid(db)
    items = _records(db, base)
    rec = next((p for p in items if p["id"] == pid), None)
    if rec is None:
        raise KeyError(pid)
    new = dict(rec)
    if "name" in fields:
        new["name"] = str(fields["name"] or "").strip() or rec["name"]
    if "from" in fields:
        new["from"] = _parse(fields["from"]).isoformat()
    if "to" in fields:
        new["to"] = _parse(fields["to"]).isoformat()
    if new["from"] > new["to"]:
        raise PeriodError("the period ends before it starts")
    if "scope" in fields:
        new["scope"] = normalise_scope(fields["scope"])
    _check_overlap(items, new["from"], new["to"], new["scope"], ignore=pid)
    _save(db, base, [new if p["id"] == pid else p for p in items])
    if new["name"] != rec["name"]:
        db.rename_timetable(rec["timetable"], new["name"])
        for kind in ("draft", "live"):                # the printouts' subtitle reads the organisation's name
            org = db.get_org(kind, tid=rec["timetable"])
            if org is not None:
                org["name"] = new["name"]
                db.set_org(kind, org, tid=rec["timetable"])
    if new["scope"] != rec["scope"]:
        refresh(db, pid, base)                        # also brings the record's `dropped` up to date
        return get(db, pid, base)
    return new


def remove(db, pid, delete_timetable=False) -> dict | None:
    """Remove a period record. The instance stays (an ordinary timetable again) unless
    `delete_timetable`, in which case the caller deletes it (it owns unlinking uploads and locks)."""
    base = base_tid(db)
    items = _records(db, base)
    rec = next((p for p in items if p["id"] == pid), None)
    if rec is None:
        return None
    _save(db, base, [p for p in items if p["id"] != pid])
    if not delete_timetable:
        try:
            db.set_value("period_base", None, tid=rec["timetable"])
        except KeyError:                              # the instance is already gone
            pass
    return rec


def record_of(db, tid) -> tuple[str, dict] | tuple[None, None]:
    """(base, record) of the period whose timetable is `tid`; (None, None) when `tid` is no period."""
    base = db.get_value("period_base", tid=tid)
    try:
        rec = next((p for p in _records(db, base) if p["timetable"] == tid), None) if base else None
    except KeyError:                                  # its base is gone
        rec = None
    return (base, rec) if rec else (None, None)


def forget_instance(db, tid) -> dict | None:
    """Drop the record of the period whose timetable is `tid` from its base (called before that
    timetable is deleted, however it is deleted). Returns the record, None when there is none."""
    base, rec = record_of(db, tid)
    if rec is not None:
        _save(db, base, [p for p in _records(db, base) if p["id"] != rec["id"]])
    return rec


def release_instances(db, base) -> None:
    """Unmark every period timetable of `base` (called before `base` is deleted): they stay as
    ordinary timetables rather than periods of a base that no longer exists."""
    for p in _records(db, base):
        try:
            db.set_value("period_base", None, tid=p["timetable"])
        except KeyError:
            pass


# ---- which timetable is in force ----------------------------------------------

def in_force(db, date, tid=None) -> list[dict]:
    d = _parse(date).isoformat()
    return [p for p in list_for(db, tid) if p["from"] <= d <= p["to"]]


def timetable_for(db, date, tid=None) -> str:
    """The period's timetable when one is in force on `date`, else the base."""
    base = base_tid(db, tid)
    hits = in_force(db, date, base)
    return hits[0]["timetable"] if hits else base


def org_for(db, date, tid=None) -> tuple[str, dict | None]:
    """(tid, live organisation of the timetable in force on `date` with the base's bookings
    injected and that date's relief covers overlaid); the organisation is None when that timetable
    has no live one."""
    from . import relief                              # relief imports this module: import lazily
    base = base_tid(db, tid)
    t = timetable_for(db, date, base)
    org = db.get_org("live", tid=t)
    if org is None:
        return t, None
    org = bookings.inject(org, list(db.get_value("bookings", tid=base) or []))
    covers = [c for c in relief.covers_for(db, date, base) if c.get("timetable") in (None, "", t)]
    if covers:
        org = relief.overlay(org, covers, absent=relief.absent_on(db, date, base))
    return t, org


def no_live_message(db, tid, default: str) -> str:
    """Why the timetable `tid`, in force on some date, has nothing to answer from: a period names
    itself; anything else gets `default`."""
    _base, rec = record_of(db, tid)
    return f"{rec['name']} has no built timetable yet: open it and build it" if rec else default


def base_changes(db, rec: dict, base=None) -> int:
    """How many of the base's change-log entries that changed its organisation were made after the
    period `rec` was created (bookings and covers live on the base for both, so they do not count)."""
    base = base or base_tid(db)
    try:
        since = calendar.timegm(time.strptime(rec["created"], "%Y-%m-%dT%H:%M:%SZ"))
    except (KeyError, TypeError, ValueError):
        return 0
    shared = changes.BOOKING_ADDED + changes.BOOKING_REMOVED + changes.COVER_ADDED
    return sum(1 for e in changes.list_all(db, tid=base) if (e.get("when") or 0) > since and e.get("kind") not in shared)


OWN_LISTS = ("persons", "locations", "groups", "bands")


def refresh(db, pid, base=None) -> dict:
    """Re-carve the base's current organisation into the period's draft, keeping the period's own
    events. A carved copy is an event marked `from_base` or whose id is in the record's `carved`
    list (the marker does not survive an engine build; the list does); every other event of the
    period's current draft (or live), fixed or not, is its own, rest tiles excluded. So a copy of a
    lesson the base has since deleted goes, and an exam paper pinned by hand stays. When an own event
    reuses the id of a lesson in the new carve, the own event wins and that carved copy is skipped.
    The period's own persons, locations, groups and bands (ids the base lacks) are merged back in.
    Returns {"replaced": carved count, "kept": own-event count}. `base` as for update."""
    base = base or base_tid(db)
    rec = get(db, pid, base)
    if rec is None:
        raise KeyError(pid)
    source = db.get_org("live", tid=base) or db.get_org("draft", tid=base)
    if source is None:
        raise PeriodError("the normal timetable has no organisation to refresh from")
    tid = rec["timetable"]
    current = db.get_org("draft", tid=tid) or db.get_org("live", tid=tid) or {"events": []}
    draft = carve(source, rec["scope"])
    draft["name"] = rec["name"]
    for key in OWN_LISTS:
        mine = current.get(key) or []
        if mine:
            have = {x.get("id") for x in draft.get(key) or []}
            draft[key] = list(draft.get(key) or []) + [copy.deepcopy(x) for x in mine if x.get("id") not in have]
    was_carved = set(rec.get("carved") or [])
    own = [copy.deepcopy(e) for e in current.get("events") or []
           if not e.get("from_base") and e.get("id") not in was_carved and not _is_rest(e)]
    own_ids = {e.get("id") for e in own}
    draft["events"] = [e for e in draft["events"] if e["id"] not in own_ids]
    replaced = len(draft["events"])
    carved = _carved_ids(draft)
    dropped = _dropped(source, draft)
    draft["events"].extend(own)
    db.set_org("draft", draft, tid=tid)
    _save(db, base, [{**p, "dropped": dropped, "carved": carved} if p["id"] == pid else p for p in _records(db, base)])
    return {"replaced": replaced, "kept": len(own)}
