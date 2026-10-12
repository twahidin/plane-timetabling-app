"""The timetable's journey (spec docs/superpowers/specs/2026-10-11-campus-looks-design.md §3.1): five steps read
from the timetable's real state, when each was first reached (kv `journey`, per timetable), and the counters the
campus shows. A step reached once whose condition no longer holds is "warn", never green."""
from __future__ import annotations

import time

from fastapi import APIRouter, Depends

from . import auth
from .plan import issues as I

STEPS = (("files", "Files read", "project"), ("plan", "Plan ready", "requirements"), ("draft", "Draft", "build"),
         ("timetable", "Timetable", "build"), ("checked", "Checked", "consolidation"))
KEYS = tuple(k for k, _, _ in STEPS)
KEY = "journey"


def _d(v) -> dict:
    return v if isinstance(v, dict) else {}


def _rest(e: dict) -> bool:
    return str(e.get("id") or "").startswith("rest-")


def _placed(e: dict) -> bool:
    return e.get("loc") is not None and e.get("t0") is not None


def reached(db, tid=None) -> dict[str, bool]:
    plan = _d(db.get_value("plan", tid))
    live, draft = _d(db.get_org("live", tid)), _d(db.get_org("draft", tid))
    check = _d(db.get_value("last_check", tid)) if live else {}
    files = bool(plan.get("staff")) and bool(plan.get("requirements"))
    try:                                    # judged against the live timetable only, as the Plan tab and Generate do
        blocks = sum(1 for i in I.plan_issues(plan, live or None, db.get_settings(tid)) if getattr(i, "level", "") == "block")
    except Exception:                       # an odd plan is "not ready", never a 500
        blocks = 1
    return {"files": files, "plan": files and blocks == 0, "draft": bool(draft or live), "timetable": bool(live),
            "checked": bool(live and check.get("ok") and not check.get("clashes"))}


def stamp(db, step: str, tid=None, now: float | None = None) -> None:
    """Record when `step` was first reached; a time once set is kept."""
    if step not in KEYS:
        return
    if step == "files":                     # a reading with no staff or no requirements has not read the files yet
        plan = _d(db.get_value("plan", tid))
        if not (plan.get("staff") and plan.get("requirements")):
            return
    # a read-modify-write without a lock: a stamp lost to a race is re-stamped by the next GET, a little later
    j = dict(_d(db.get_value(KEY, tid)))
    if not j.get(step):
        j[step] = now if now is not None else time.time()
        db.set_value(KEY, j, tid)


def counters(db, tid=None) -> dict:
    live = _d(db.get_org("live", tid))
    org = live or _d(db.get_org("draft", tid))
    persons = {p.get("id") for p in org.get("persons") or [] if isinstance(p, dict) and isinstance(p.get("id"), str)}
    events = [e for e in org.get("events") or [] if isinstance(e, dict) and not _rest(e)]
    placed = [e for e in events if _placed(e)]
    deployed = {m for e in placed for m in (e.get("members") if isinstance(e.get("members"), list) else [])
                if isinstance(m, str) and m in persons}
    clashes = _d(db.get_value("last_check", tid)).get("clashes") if live else None
    return {"lessons_placed": len(placed), "lessons_total": len(events), "teachers_deployed": len(deployed),
            "teachers_total": len(persons), "clashes": len(clashes) if isinstance(clashes, list) else 0,
            "source": "live" if live else "draft" if org else None}


def journey(db, tid=None) -> dict:
    ok = reached(db, tid)
    for k in KEYS:                          # the lazy stamps: Plan ready and Checked are reached by many paths
        if ok[k]:
            stamp(db, k, tid)
    times = _d(db.get_value(KEY, tid))
    steps, current = [], False
    for key, label, tab in STEPS:
        state = "done" if ok[key] else "warn" if times.get(key) else "todo"
        if state == "todo" and not current:
            state, current = "current", True
        steps.append({"key": key, "label": label, "tab": tab, "state": state, "at": times.get(key)})
    return {"timetable": tid or db.working_timetable(), "steps": steps, "counters": counters(db, tid)}


def make_router(db) -> APIRouter:
    r = APIRouter()

    @r.get("/api/journey")
    def get_journey(sid: str = Depends(auth.require_session)):
        return journey(db)

    return r
