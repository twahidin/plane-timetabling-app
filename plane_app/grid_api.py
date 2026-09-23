"""The on-page timetable grid: the printouts' grid as JSON, with event ids per cell and the last
check's problems marked on the cells they touch."""
from __future__ import annotations

from fastapi import APIRouter, Depends

from . import auth
from .print import grid as G
from .print.routes import view_in_force


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


def grid_json(grid: G.Grid, bad: dict[str, list[str]]) -> dict:
    days = []
    for d in grid.days:
        cells = []
        for c in d.cells:
            if c.kind == "continued":
                cells.append({"kind": "continued"})
                continue
            msgs: list[str] = []
            for e in c.events:
                msgs.extend(m for m in bad.get(e, []) if m not in msgs)
            cells.append({"kind": c.kind, "text": c.text, "sub": c.sub, "span": c.span, "events": list(c.events), "bad": msgs})
        days.append({"label": d.label, "off": d.off, "cells": cells})
    return {"title": grid.title, "subtitle": grid.subtitle, "kind": grid.kind, "id": grid.id,
            "slots": list(grid.slots), "days": days}


def make_router(db) -> APIRouter:
    r = APIRouter()

    @r.get("/api/grid/{kind}/{id}")
    def one(kind: str, id: str, view: str = "cycle", date: str | None = None, sid: str = Depends(auth.require_session)):
        tid, grid = view_in_force(db, kind, id, view, date)     # a week in a period: that timetable's marks
        return grid_json(grid, bad_by_event(db.get_value("last_check", tid=tid)))

    return r
