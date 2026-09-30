"""The decision log (spec docs/superpowers/specs/2026-09-30-learning-design.md §1.1): what people decided,
per timetable, as ids and the plan's own words — the raw material for suggestions and templates.

A row holds ids (events, people, rooms), slots and the plan's subject and grouping words. Never a
person's name, a class code typed by a user or any free text."""
from __future__ import annotations

import json
import logging
import time

MAX_ROWS = 5000                    # per timetable; `record` drops the oldest beyond it
log = logging.getLogger(__name__)


def record(db, kind: str, data: dict, tid: str | None = None) -> None:
    tid = tid or db.working_timetable()          # the selection for the admin; the pinned one for a department account
    with db._con() as con:
        con.execute("insert into decisions (timetable_id, kind, at, data) values (?, ?, ?, ?)",
                    (tid, kind, time.time(), json.dumps(data, separators=(",", ":"))))
        con.execute("delete from decisions where timetable_id = ? and id not in "
                    "(select id from decisions where timetable_id = ? order by id desc limit ?)",
                    (tid, tid, MAX_ROWS))


def safe_record(db, kind: str, data, tid=None) -> None:
    """For hooks: a failure to log never breaks the action being logged. `data` and `tid` may be
    callables, so that building them (which reads cards, plans and settings) is inside the guard too."""
    try:
        record(db, kind, data() if callable(data) else data, tid() if callable(tid) else tid)
    except Exception:  # noqa: BLE001
        log.warning("decision log write failed (%s)", kind, exc_info=True)


def rows(db, since: float = 0, kinds: tuple[str, ...] | None = None, tid: str | None = None,
         data: bool = True) -> list[dict]:
    """[{id, kind, at, data}] of one timetable, oldest first. `data=False` leaves each row's data `{}` and
    does not read or parse it (for a caller that only counts kinds)."""
    tid = tid or db.working_timetable()
    sql, args = (f"select id, kind, at, {'data' if data else '1 as data'} from decisions where timetable_id = ? and at >= ?"), [tid, since]
    if kinds:
        sql += f" and kind in ({','.join('?' * len(kinds))})"
        args += list(kinds)
    with db._con() as con:
        found = con.execute(sql + " order by id", args).fetchall()
    return [{"id": r["id"], "kind": r["kind"], "at": r["at"], "data": json.loads(r["data"]) if data else {}} for r in found]


def subject_of(plan: dict | None, event: dict) -> tuple[str, str]:
    """(subject, group) of an event. Plan-generated event ids are built from the requirement's id
    (plan/generate.py::_fit_id: "<requirement>-<length>-<n>"), so the requirement whose id is the
    longest whole-segment prefix of the event id names it: its subject, and its class code (grouping
    "class") or its grouping. Otherwise the event's name is split at its last space
    ("English Language 1A1" -> ("English Language", "1A1"))."""
    eid = str(event.get("id") or "")
    best = None
    for r in (plan or {}).get("requirements") or []:
        rid = str(r.get("id") or "")
        if rid and (eid == rid or eid.startswith(rid + "-")) and (best is None or len(rid) > len(best["id"])):
            best = r
    if best is not None:
        grouping = best.get("grouping")
        classes = best.get("classes") or []
        group = (classes[0] if classes else best["id"]) if grouping == "class" else str(grouping or "")
        return str(best.get("subject") or ""), group
    head, _, tail = str(event.get("name") or "").strip().rpartition(" ")
    return (head, tail) if head else (tail, "")


def slot_of(t0: int, slots_per_day: int) -> tuple[int, int]:
    """(day, slot) of a start time."""
    return divmod(int(t0), int(slots_per_day))
