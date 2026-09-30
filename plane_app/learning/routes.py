"""The learning routes (spec docs/superpowers/specs/2026-09-30-learning-design.md §1.2, §1.4): the suggestions,
the rules already learned and the edits-after-a-timetable measure, and the three things the Constraints card
does with them: accept a suggestion, hide one, switch a learned rule off or on. Also the school's own wizard
templates (§1.3): save the current timetable as one, list them, delete one. Admin only, like the plan's
own routes. Ids contain `:` and may contain a space or a slash, hence the `path` converter."""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException

from .. import auth
from ..wizard.library import WizardError
from . import habits
from . import templates

NOT_NOW_DAYS = habits.HIDE_DAYS


def make_router(db) -> APIRouter:
    r = APIRouter()

    @r.get("/api/learning")
    def overview(sid: str = Depends(auth.require_session)):
        return {"suggestions": habits.suggestions(db), "learned": habits.learned(db), "edits": habits.edits_after_solves(db)}

    @r.post("/api/learning/suggestions/{sid:path}/accept")
    def accept(sid: str, session: str = Depends(auth.require_session)):
        try:
            return habits.accept(db, sid)
        except habits.LearningError as e:
            raise HTTPException(404 if str(e) == "no such suggestion" else 409, str(e))

    @r.post("/api/learning/suggestions/{sid:path}/hide")
    def hide(sid: str, body: dict | None = None, session: str = Depends(auth.require_session)):
        never = bool((body or {}).get("never")) if isinstance(body, dict) else False
        habits.hide(db, sid, None if never else NOT_NOW_DAYS)
        return {"ok": True}

    @r.post("/api/learning/learned/{lid:path}/switch")
    def switch(lid: str, body: dict, session: str = Depends(auth.require_session)):
        try:
            return habits.switch(db, lid, bool(body.get("on")))
        except habits.LearningError as e:
            raise HTTPException(404 if str(e) == "no such learned rule" else 409, str(e))

    @r.get("/api/templates/local")
    def local_templates(session: str = Depends(auth.require_session)):
        return templates.local_templates(db)

    @r.post("/api/templates/local")
    def save_template(body: dict, session: str = Depends(auth.require_session)):
        try:
            return templates.save_local(db, templates.export_template(db, body))
        except WizardError as e:
            raise HTTPException(400, str(e))

    @r.delete("/api/templates/local/{template_id}")
    def delete_template(template_id: str, session: str = Depends(auth.require_session)):
        try:
            templates.delete_local(db, template_id)
        except KeyError:
            raise HTTPException(404, f"no saved template {template_id!r}")
        return {"ok": True}

    return r
