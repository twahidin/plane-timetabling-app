"""HTTP routes for the curriculum plan (spec docs/superpowers/specs/2026-09-19-curriculum-plan-design.md §5, §7).

Reads and writes the plan document (`db.get_value("plan")` / `db.set_value("plan", plan)`, already
scoped per timetable), computes issues fresh against the live organisation, and turns the plan into
a draft organisation on generate. No LLM is involved here; the workbook importer is deterministic."""
from __future__ import annotations

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile
from fastapi.responses import JSONResponse

from .. import auth
from ..config import MAX_UPLOAD
from . import generate as G
from . import importer as IMP
from . import model as M
from .issues import Issue, capped, has_blocks, plan_issues


def _issue_dict(i: Issue) -> dict:
    return {"level": i.level, "where": i.where, "text": i.text}


def _current_plan(db) -> dict:
    return db.get_value("plan") or M.empty_plan()


def _issues_for(db, plan: dict) -> list[Issue]:
    return plan_issues(plan, db.get_org("live"), db.get_settings())


def _decode_csv(data: bytes) -> str:
    try:
        return data.decode("utf-8-sig")
    except UnicodeDecodeError:
        return data.decode("cp1252", errors="replace")


def import_workbook(db, filename: str, data: bytes, sizes_texts: tuple[str, ...] = ()) -> tuple[dict, list[Issue], str]:
    """Import a deployment or generic duties workbook into the stored plan (merging with what is
    there), apply any sizes CSVs on top, store the result and return (plan, issues, note). Shared
    between the dedicated upload route and the general `/api/upload` handler's workbook routing.
    A generic workbook is detected by its sheet names (`is_generic_workbook`); everything else
    that reaches here is read as a staff-deployment workbook, same as before the start wizard."""
    org = db.get_org("live")
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

    @r.get("/api/plan/issues")
    def get_issues(sid: str = Depends(auth.require_session)):
        plan = _current_plan(db)
        return {"issues": [_issue_dict(i) for i in _issues_for(db, plan)]}

    return r
