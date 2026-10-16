"""The on-page timetable grid: the printouts' grid as JSON, with event ids per cell and the last
check's problems marked on the cells they touch."""
from __future__ import annotations

from fastapi import APIRouter, Depends

from . import auth
from .print import grid as G
from .print.routes import student_grid_for, view_in_force


def bad_by_event(check: dict | None) -> dict[str, list[str]]:
    """event id -> the messages of every clash naming it (as `event` or in a tile), in check order."""
    out: dict[str, list[str]] = {}
    for c in (check or {}).get("clashes") or []:
        ids = [c.get("event")] + [t.split("@", 1)[0] for t in c.get("tiles") or []]
        msg = c.get("message") or c.get("type") or ""
        if not msg:
            continue
        for i in dict.fromkeys(i for i in ids if i):
            if msg not in out.setdefault(i, []):
                out[i].append(msg)
    return out


def grid_json(grid: G.Grid, bad: dict[str, list[str]], row_bad=None) -> dict:
    """`bad` marks every row's cells; `row_bad(row)`, when given, marks a row instead (a dated week's
    rows may come from different timetables, each with its own last check)."""
    days = []
    for d in grid.days:
        marks = bad if row_bad is None else row_bad(d)
        cells = []
        for c in d.cells:
            if c.kind == "continued":
                cells.append({"kind": "continued"})
                continue
            msgs: list[str] = list(c.bad)                      # a cell's own problem first (a lesson in a break band)
            for e in c.events:
                msgs.extend(m for m in marks.get(e, []) if m not in msgs)
            cells.append({"kind": c.kind, "text": c.text, "sub": c.sub, "span": c.span, "events": list(c.events), "bad": msgs})
        days.append({"label": d.label, "off": d.off, "slots": list(d.slots), "cells": cells})
    return {"title": grid.title, "subtitle": grid.subtitle, "kind": grid.kind, "id": grid.id,
            "slots": list(grid.slots), "days": days}


def make_router(db) -> APIRouter:
    r = APIRouter()

    def marks(tid: str):
        return bad_by_event(db.get_value("last_check", tid=tid))

    @r.get("/api/grid/student/{student_id}")
    def student(student_id: str, view: str = "cycle", sid: str = Depends(auth.require_session)):
        """One student's cycle grid (admin only: it carries the name). The last check's problems are marked on it."""
        grid = student_grid_for(db, student_id, view)
        return grid_json(grid, marks(db.working_timetable()))

    @r.get("/api/grid/{kind}/{id}")
    def one(kind: str, id: str, view: str = "cycle", date: str | None = None, who: auth.Principal = Depends(auth.require_user)):
        tid, grid = view_in_force(db, kind, id, view, date)     # a week in a period: that timetable's marks
        checks: dict[str, dict[str, list[str]]] = {}

        def bad_of(t: str) -> dict[str, list[str]]:
            if t not in checks:
                checks[t] = bad_by_event(db.get_value("last_check", tid=t))
            return checks[t]
        return grid_json(grid, bad_of(tid), lambda row: bad_of(row.timetable or tid))    # each weekday: its own timetable's

    return r
