"""Period timetables over HTTP: the Periods card's list, create, rename/redate/rescope, refresh and
remove (spec docs/superpowers/specs/2026-09-23-period-timetables-design.md §5)."""
from __future__ import annotations

from datetime import date as _date

from fastapi import APIRouter, Depends, HTTPException

from . import auth, bookings, periods
from .engine_client import EngineError


def public(rec: dict) -> dict:
    """A period record as the page sees it: the carved ids stay on the server."""
    return {k: v for k, v in rec.items() if k != "carved"}


def _levels_and_classes(org: dict | None) -> tuple[list[str], list[str]]:
    seen: dict[str, str] = {}                                 # one spelling per class, the first met
    for g in (org or {}).get("groups") or []:
        for c in g.get("classes") or []:
            if str(c).strip():
                seen.setdefault(str(c).strip().lower(), str(c).strip())
    classes = [seen[k] for k in sorted(seen)]
    levels = sorted({periods.level_of(c) for c in classes} - {""}, key=int)
    return levels, classes


def make_router(db, engine, delete_instance) -> APIRouter:
    """`engine()` gives an engine client for the current settings; `delete_instance(tid)` deletes a
    timetable the way DELETE /api/timetables/{tid} does (uploads unlinked, locks pruned)."""
    r = APIRouter()

    def _fail(e: periods.PeriodError) -> HTTPException:
        return HTTPException(getattr(e, "status", 400), str(e))

    @r.get("/api/periods")
    def list_periods(sid: str = Depends(auth.require_session)):
        current = db.current_timetable()
        base = periods.base_tid(db, current)
        items = periods.list_for(db, base)
        today = _date.today().isoformat()
        hits = periods.in_force(db, today, base)
        levels, classes = _levels_and_classes(db.get_org("live", tid=base) or db.get_org("draft", tid=base))
        return {"base": base, "base_name": next((t["name"] for t in db.timetables() if t["id"] == base), base),
                "this": next((p["id"] for p in items if p["timetable"] == current), None),
                "items": [public(p) | {"base_changes": periods.base_changes(db, p, base)} for p in items],
                "today": hits[0]["id"] if hits else None,
                "today_date": today, "levels": levels, "classes": classes}

    @r.post("/api/periods")
    def create_period(body: dict, sid: str = Depends(auth.require_session)):
        body = body if isinstance(body, dict) else {}
        try:
            rec = periods.create(db, body.get("name"), body.get("from"), body.get("to"), body.get("scope"))
        except periods.PeriodError as e:
            raise _fail(e)
        db.select_timetable(rec["timetable"])            # the user is about to fill it
        return {"period": public(rec), "dropped": rec["dropped"], "current": rec["timetable"]}

    @r.patch("/api/periods/{pid}")
    def update_period(pid: str, body: dict, sid: str = Depends(auth.require_session)):
        fields = {k: body[k] for k in ("name", "from", "to", "scope") if isinstance(body, dict) and k in body}
        try:
            return public(periods.update(db, pid, **fields))
        except KeyError:
            raise HTTPException(404, f"no period {pid}")
        except periods.PeriodError as e:
            raise _fail(e)

    @r.post("/api/periods/{pid}/refresh")
    def refresh_period(pid: str, sid: str = Depends(auth.require_session)):
        base = periods.base_tid(db)
        rec = periods.get(db, pid, base)
        if rec is None:
            raise HTTPException(404, f"no period {pid}")
        try:
            res = periods.refresh(db, pid)
        except periods.PeriodError as e:
            raise _fail(e)
        draft = db.get_org("draft", tid=rec["timetable"])
        try:                                              # a check, not a build: nothing is stored
            clashes = len(engine().check(bookings.inject(draft, list(db.get_value("bookings", tid=base) or [])))["clashes"])
        except EngineError:
            clashes = None                                # the refresh stands; the engine could not say
        return {**res, "clashes": clashes}

    @r.delete("/api/periods/{pid}")
    def remove_period(pid: str, timetable: bool = False, sid: str = Depends(auth.require_session)):
        base = periods.base_tid(db)
        rec = periods.remove(db, pid)                  # unmarks the instance too, so a failed delete below
        if rec is None:                                # leaves an ordinary timetable, never an orphan period
            raise HTTPException(404, f"no period {pid}")
        if timetable:
            if db.current_timetable() == rec["timetable"]:
                db.select_timetable(base)                 # back to the normal timetable, not whichever is first
            try:
                delete_instance(rec["timetable"])
            except KeyError:
                pass                                      # the instance is already gone
        return {"removed": public(rec), "current": db.current_timetable()}

    return r
