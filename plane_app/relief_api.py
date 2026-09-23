"""Relief over HTTP: absences, the cover plan, removing one cover, the relief settings and the ledger (spec
docs/superpowers/specs/2026-09-23-relief-agent-design.md §5). Everything reads and writes the base
timetable's record (`relief`), so a period timetable's page sees the same absences.

The plan route stages the cover cards in the timetable's chat thread (`db.THREAD`) under a fresh run
token and returns them; the page shows them in the chat as proposal cards, and they are applied the
way every proposal is: a click on Apply, or a yes."""
from __future__ import annotations

import secrets

from fastapi import APIRouter, Depends, HTTPException

from . import auth, relief
from .db import THREAD


def make_router(db) -> APIRouter:
    r = APIRouter()

    def _fail(e: relief.ReliefError) -> HTTPException:
        return HTTPException(400, str(e))

    def _settings_payload() -> dict:
        # `term_start`: the date the ledger counts from (the relief settings' own, else the calendar's)
        return {"settings": relief.settings(db), "pool_names": relief.pool_names(db), "term_start": relief.term_start(db)}

    @r.get("/api/relief")
    def overview(sid: str = Depends(auth.require_session)):
        # `day`: the slots a part-day absence can start or end at, by label, for the Add absence dialog
        return {"absences": relief.absence_rows(db), "day": relief.day_slots(db), **_settings_payload()}

    @r.post("/api/relief/absences")
    def add_absence(body: dict, sid: str = Depends(auth.require_session)):
        try:
            person = relief.person_id(db, body.get("person"))
            rec = relief.add_absence(db, person, body.get("from"), body.get("to"), body.get("slots"), body.get("reason") or "")
        except relief.ReliefError as e:
            raise _fail(e)
        return {"absence": rec}

    @r.delete("/api/relief/absences/{aid}")
    def remove_absence(aid: str, sid: str = Depends(auth.require_session)):
        rec = relief.remove_absence(db, aid)
        if rec is None:
            raise HTTPException(404, f"no absence {aid}")
        return {"removed": rec}

    @r.delete("/api/relief/covers/{cid}")
    def remove_cover(cid: str, sid: str = Depends(auth.require_session)):
        # Logged before it is removed, so Undo puts the cover back. No event: the page reloads the card.
        rec = relief.withdraw_cover(db, cid)
        if rec is None:
            raise HTTPException(404, f"no cover {cid}")
        absence = relief.get_absence(db, rec.get("absence"))
        return {"removed": rec, "row": relief.absence_row(db, absence) if absence else None}

    @r.get("/api/relief/plan/{aid}")
    def plan(aid: str, sid: str = Depends(auth.require_session)):
        if relief.get_absence(db, aid) is None:
            raise HTTPException(404, f"no absence {aid}")
        try:
            return {"items": relief.plan(db, aid, session_id=THREAD, run=secrets.token_hex(4))}
        except relief.ReliefError as e:
            raise _fail(e)

    @r.get("/api/relief/settings")
    def get_settings(sid: str = Depends(auth.require_session)):
        return _settings_payload()

    @r.put("/api/relief/settings")
    def put_settings(body: dict, sid: str = Depends(auth.require_session)):
        try:
            relief.set_settings(db, body)
        except relief.ReliefError as e:
            raise _fail(e)
        return _settings_payload()

    @r.get("/api/relief/ledger")
    def ledger(since: str | None = None, sid: str = Depends(auth.require_session)):
        try:
            return {"items": relief.ledger(db, since)}
        except relief.ReliefError as e:
            raise _fail(e)

    return r
