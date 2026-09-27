"""HTTP routes for the curriculum plan (spec docs/superpowers/specs/2026-09-19-curriculum-plan-design.md §5, §7).

Reads and writes the plan document (`db.get_value("plan")` / `db.set_value("plan", plan)`, already
scoped per timetable), computes issues fresh against the live organisation, and turns the plan into
a draft organisation on generate. No LLM is involved here; the workbook importer is deterministic."""
from __future__ import annotations

import json

from fastapi import APIRouter, Depends, File, HTTPException, Request, UploadFile
from fastapi.responses import JSONResponse, Response

from .. import auth
from ..config import MAX_UPLOAD
from . import board as B
from . import export as X
from . import generate as G
from . import importer as IMP
from . import model as M
from .issues import Issue, capped, has_blocks, plan_issues


def _issue_dict(i: Issue) -> dict:
    return {"level": i.level, "where": i.where, "text": i.text}


# A board response carries the plan's issues for the panel above it; a school's plan can have
# thousands, and each board change sends them again, so it sends the first 50 (blocks first) with
# the counts of each level and of the rest, and the panel says "and N more".
BOARD_ISSUE_LIMIT = 50


def _board_issues(issues: list[Issue]) -> dict:
    blocks = [i for i in issues if i.level == "block"]
    rest = [i for i in issues if i.level != "block"]
    shown = (blocks + rest)[:BOARD_ISSUE_LIMIT]
    return {"issues": [_issue_dict(i) for i in shown],
            "issue_counts": {"block": len(blocks), "warn": sum(1 for i in rest if i.level == "warn"),
                             "more": len(issues) - len(shown)}}


def _current_plan(db) -> dict:
    return db.get_value("plan") or M.empty_plan()


def _issues_for(db, plan: dict) -> list[Issue]:
    return plan_issues(plan, db.get_org("live"), db.get_settings())


def _decode_csv(data: bytes) -> str:
    try:
        return data.decode("utf-8-sig")
    except UnicodeDecodeError:
        return data.decode("cp1252", errors="replace")


# POST /api/plan/board/<action>: the board's edits, each a pure plan -> plan function
BOARD_ACTIONS = {"assign": B.assign, "unassign": B.unassign, "split": B.split, "lock": B.lock,
                 "row": B.add_row, "band": B.add_band, "staff": B.save_staff}


async def _json_object(request: Request, *, optional: bool = False) -> dict:
    raw = await request.body()
    if optional and not raw.strip():
        return {}
    try:
        body = json.loads(raw)
    except ValueError:
        raise HTTPException(400, "the body must be JSON")
    if not isinstance(body, dict):
        raise HTTPException(400, "the body must be a JSON object")
    return body


def import_workbook(db, filename: str, data: bytes, sizes_texts: tuple[str, ...] = ()) -> tuple[dict, list[Issue], str]:
    """Import a deployment or generic duties workbook into the stored plan (merging with what is
    there), apply any sizes CSVs on top, store the result and return (plan, issues, note). Shared
    between the dedicated upload route and the general `/api/upload` handler's workbook routing.
    A generic workbook is detected by its sheet names (`is_generic_workbook`); everything else
    that reaches here is read as a staff-deployment workbook, same as before the start wizard."""
    org = db.get_org("live")
    # the stored plan is merged into, so one that does not read (a requirement stored before a
    # limit, or written directly) is a 422 naming it, never a 500; the plan stays as it was
    try:
        if IMP.is_generic_workbook(data):
            plan, parse_issues = IMP.read_generic_workbook(data, org, _current_plan(db), filename,
                                                           db.get_settings())
        else:
            plan, parse_issues = IMP.read_workbook(data, org, _current_plan(db), filename)
        sizes: dict[str, int] = {}
        for text in sizes_texts:
            sizes.update(IMP.read_sizes_csv(text))
        if sizes:
            plan = IMP.apply_sizes(plan, sizes)
    except M.PlanError as e:
        raise HTTPException(422, f"the plan does not read, so the workbook was not merged into it: {e}. "
                                 f"Fix it in the Tables view first")
    db.set_value("plan", plan)

    seen: set[tuple[str, str, str]] = set()
    issues: list[Issue] = []
    for i in (*parse_issues, *plan_issues(plan, org, db.get_settings())):
        key = (i.level, i.where, i.text)
        if key not in seen:
            seen.add(key)
            issues.append(i)

    note = (f"Imported {len(plan['requirements'])} requirements, {len(plan['staff'])} staff, "
            f"{len(plan['bands'])} bands from {filename}")
    return plan, issues, note


def make_router(db) -> APIRouter:
    r = APIRouter()

    @r.get("/api/plan")
    def get_plan(sid: str = Depends(auth.require_session)):
        plan = _current_plan(db)
        return {"plan": plan, "issues": [_issue_dict(i) for i in _issues_for(db, plan)]}

    @r.patch("/api/plan")
    def patch_plan(body: dict, sid: str = Depends(auth.require_session)):
        plan = _current_plan(db)
        try:
            new = M.apply_patch(plan, (body or {}).get("patch") or {})
        except M.PlanError as e:
            raise HTTPException(422, str(e))
        db.set_value("plan", new)
        return {"plan": new, "issues": [_issue_dict(i) for i in _issues_for(db, new)]}

    @r.post("/api/plan/upload")
    async def upload_plan(file: UploadFile = File(...), sizes: list[UploadFile] = File(default=[]),
                          sid: str = Depends(auth.require_session)):
        data = await file.read()
        if len(data) > MAX_UPLOAD:
            raise HTTPException(400, f"{file.filename or 'file'} is over 20 MB")
        if not (IMP.is_deployment_workbook(data) or IMP.is_generic_workbook(data)):
            raise HTTPException(400, f"{file.filename or 'file'} is not a staff-deployment or duties workbook")
        sizes_texts = []
        for s in sizes:
            sdata = await s.read()
            if len(sdata) > MAX_UPLOAD:
                raise HTTPException(400, f"{s.filename or 'file'} is over 20 MB")
            sizes_texts.append(_decode_csv(sdata))
        plan, issues, note = import_workbook(db, file.filename or "upload", data, tuple(sizes_texts))
        return {"plan": plan, "issues": [_issue_dict(i) for i in issues], "note": note}

    @r.post("/api/plan/generate")
    def generate_plan(sid: str = Depends(auth.require_session)):
        plan = _current_plan(db)
        settings = db.get_settings()
        base_org = db.get_org("live")
        issues = plan_issues(plan, base_org, settings)
        if has_blocks(issues):
            blocks = [i.text for i in issues if i.level == "block"]
            return JSONResponse({"detail": f"{len(blocks)} blocking issues", "blocks": capped(blocks)}, status_code=409)
        org, summary = G.generate(plan, base_org, settings)
        db.set_org("draft", org)
        return {"ok": True, "summary": summary, "issues": [_issue_dict(i) for i in issues]}

    def _xlsx(data: bytes, filename: str) -> Response:
        return Response(data, media_type=X.MEDIA_TYPE,
                        headers={"Content-Disposition": f'attachment; filename="{filename}"'})

    @r.get("/api/plan/template.xlsx")
    def plan_template(sid: str = Depends(auth.require_session)):
        blank = {**M.empty_plan(), "vocabulary": M.vocabulary_of(db)}
        return _xlsx(X.write_workbook(blank, db.get_settings(), example=True), "plan-template.xlsx")

    @r.get("/api/plan/export.xlsx")
    def plan_export(sid: str = Depends(auth.require_session)):
        tid = db.current_timetable()
        name = next((t["name"] for t in db.timetables() if t["id"] == tid), "") or tid
        try:
            data = X.write_workbook(_current_plan(db), db.get_settings())
        except M.PlanError as e:
            raise HTTPException(422, str(e))
        return _xlsx(data, f"plan-{M.plan_slug(name)}.xlsx")

    def _board(plan: dict, dept, level) -> dict:
        """The board for (dept, level), or the first department and level when an edit or undo
        took that one away; with the plan's issues."""
        settings = db.get_settings()
        try:
            out = B.view(plan, settings, dept, level)
        except B.BoardError:
            try:
                out = B.view(plan, settings, dept)
            except B.BoardError:
                out = B.view(plan, settings)
        out.update(_board_issues(_issues_for(db, plan)))
        return out

    @r.get("/api/plan/board")
    def get_board(dept: str | None = None, level: str | None = None, sid: str = Depends(auth.require_session)):
        plan = _current_plan(db)
        try:
            out = B.view(plan, db.get_settings(), dept or None, level or None)
        except B.BoardError as e:
            raise HTTPException(e.status, e.message)
        out.update(_board_issues(_issues_for(db, plan)))
        return out

    # before /api/plan/board/{action}, which would otherwise take "undo" as an action
    @r.post("/api/plan/board/undo")
    async def board_undo(request: Request, sid: str = Depends(auth.require_session)):
        body = await _json_object(request, optional=True)
        plan = db.plan_history_pop()
        if plan is None:
            raise HTTPException(404, "nothing to undo")
        db.set_board_plan(plan)
        return _board(plan, *B.where(plan, body))

    @r.post("/api/plan/board/{action}")
    async def board_edit(action: str, request: Request, sid: str = Depends(auth.require_session)):
        edit = BOARD_ACTIONS.get(action)
        if edit is None:
            raise HTTPException(404, f"no board action {action!r}")
        body = await _json_object(request)
        plan = _current_plan(db)
        try:
            new = edit(plan, db.get_settings(), body)
        except B.BoardError as e:
            raise HTTPException(e.status, e.message)
        db.plan_history_push(plan)          # one snapshot row; the oldest past 20 is deleted
        db.set_board_plan(new)              # keeps the history (any other plan write clears it)
        return _board(new, *B.where(plan, body))

    @r.get("/api/plan/issues")
    def get_issues(sid: str = Depends(auth.require_session)):
        plan = _current_plan(db)
        return {"issues": [_issue_dict(i) for i in _issues_for(db, plan)]}

    return r
