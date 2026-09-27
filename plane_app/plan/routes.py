"""HTTP routes for the curriculum plan (spec docs/superpowers/specs/2026-09-19-curriculum-plan-design.md §5, §7).

Reads and writes the plan document (`db.get_value("plan")` / `db.set_value("plan", plan)`, already
scoped per timetable), computes issues fresh against the live organisation, and turns the plan into
a draft organisation on generate. No LLM is involved here; the workbook importer is deterministic."""
from __future__ import annotations

import json
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, File, HTTPException, Request, UploadFile
from fastapi.responses import JSONResponse, Response
from starlette.concurrency import run_in_threadpool

from .. import auth, users
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


def _issues_for(db, plan: dict, who: auth.Principal | None = None) -> list[Issue]:
    """The plan's issues as `who` sees them (None: the admin). The admin also hears which
    departments with an account have not submitted; a head of department hears only about their
    own department's requirements and teachers, and of a teacher shared in from another department
    only their share issues in this one (spec 2026-09-27 §4): nothing else of theirs is the head's."""
    issues = plan_issues(plan, db.get_org("live"), db.get_settings())
    if who is None or who.is_admin:
        return issues + _unsubmitted(db, plan)
    dept = who.dept
    mine = ({r.get("id") for r in plan.get("requirements") or [] if r.get("dept") == dept}
            | {s.get("id") for s in plan.get("staff") or [] if (s.get("dept") or "") == dept})
    shared = {s.get("id") for s in plan.get("staff") or []
              if (s.get("dept") or "") != dept and M.available_in(s, dept)}
    return [i for i in issues
            if (i.where in mine and i.dept in (None, dept)) or (i.where in shared and i.dept == dept)]


def _unsubmitted(db, plan: dict) -> list[Issue]:
    with_accounts = {u["dept"] for u in users.list_users(db) if u.get("active") and u.get("dept")}
    in_plan = {r.get("dept") for r in plan.get("requirements") or []}
    return [Issue("warn", d, f"{d} not yet submitted") for d in sorted(with_accounts & in_plan)
            if M.department_status(plan, d)["status"] != "submitted"]


def _decode_csv(data: bytes) -> str:
    try:
        return data.decode("utf-8-sig")
    except UnicodeDecodeError:
        return data.decode("cp1252", errors="replace")


# POST /api/plan/board/<action>: the board's edits, each a pure plan -> plan function
BOARD_ACTIONS = {"assign": B.assign, "unassign": B.unassign, "split": B.split, "lock": B.lock,
                 "row": B.add_row, "band": B.add_band, "staff": B.save_staff,
                 "submit": B.submit, "reopen": B.reopen}

# Board undo rows are the changes of one edit, not whole plans, and undo is per user: the history
# keeps more of them than db's default so one busy account does not push another's off the end.
BOARD_HISTORY_KEEP = 100
ACTIVITY_KEEP = 300          # kv "plan_activity" (per timetable): the last this many board changes


def _undo_row(row: dict, who: auth.Principal) -> bool:
    """A history row `who`'s undo takes: their own change, or a row from before per-user undo (a
    whole plan, no `changes`), which is only ever dropped, never applied."""
    return "changes" not in row or row.get("user") == who.user_id


def _who_changed(db, rev_at) -> str:
    """Who made the plan's rev `rev_at`, from the activity log; a change made off the board (an
    upload, a patch, chat) is the timetabler's."""
    log = db.get_value("plan_activity") or []
    for entry in reversed(log):
        if entry.get("rev") == rev_at:
            return entry.get("who") or "Someone else"
    oldest = next((e.get("rev") for e in log if isinstance(e.get("rev"), int)), None)
    return "The timetabler" if oldest is None or oldest < rev_at else "Someone else"   # else: off the log


def _record(db, who: auth.Principal, dept, text: str, rev: int, undo: bool = False) -> None:
    log = list(db.get_value("plan_activity") or [])
    entry = {"when": datetime.now(timezone.utc).isoformat(timespec="seconds"), "who": who.name, "user": who.user_id,
             "dept": dept or "", "text": text, "rev": rev}
    if undo:
        entry["undo"] = True
    log.append(entry)
    db.set_value("plan_activity", log[-ACTIVITY_KEEP:])


def _own_undos(db, who: auth.Principal) -> frozenset:
    """The plan revs `who`'s own undos made (board.undo's `own`)."""
    return frozenset(e.get("rev") for e in db.get_value("plan_activity") or []
                     if e.get("undo") and e.get("user") == who.user_id)


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
    """Blocking (it takes the plan lock): an async route runs it with run_in_threadpool.
    Import a deployment or generic duties workbook into the stored plan (merging with what is
    there), apply any sizes CSVs on top, store the result and return (plan, issues, note). Shared
    between the dedicated upload route and the general `/api/upload` handler's workbook routing.
    A generic workbook is detected by its sheet names (`is_generic_workbook`); everything else
    that reaches here is read as a staff-deployment workbook, same as before the start wizard."""
    org = db.get_org("live")
    with db.plan_lock():
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
        with db.plan_lock():
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
        # in a worker thread: the plan lock may be held, and waiting for it must not stall the event loop
        plan, issues, note = await run_in_threadpool(import_workbook, db, file.filename or "upload", data,
                                                     tuple(sizes_texts))
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

    def _board(plan: dict, dept, level, who: auth.Principal | None = None) -> dict:
        """The board for (dept, level), or the first department and level when an edit or undo
        took that one away; with the plan's issues."""
        settings = db.get_settings()
        try:
            out = B.view(plan, settings, dept, level, principal=who)
        except B.BoardError:
            try:
                out = B.view(plan, settings, dept, principal=who)
            except B.BoardError:
                out = B.view(plan, settings, principal=who)
        out.update(_board_issues(_issues_for(db, plan, who)))
        return out

    def _stale(plan: dict, body: dict, who: auth.Principal, e: B.StaleError, tail: str) -> JSONResponse:
        """409 naming who changed the requirement, with the current board for the page to show."""
        detail = f"{_who_changed(db, e.rev_at)} changed {e.what} {tail}"
        return JSONResponse({"detail": detail, "board": _board(plan, *B.where(plan, body), who)}, status_code=409)

    @r.get("/api/plan/board")
    def get_board(dept: str | None = None, level: str | None = None, who: auth.Principal = Depends(auth.require_user)):
        plan = _current_plan(db)
        try:
            out = B.view(plan, db.get_settings(), dept or None, level or None, principal=who)
        except B.BoardError as e:
            raise HTTPException(e.status, e.message)
        out.update(_board_issues(_issues_for(db, plan, who)))
        return out

    def _dept_of(plan: dict, body: dict, who: auth.Principal):
        return who.dept if not who.is_admin else B.department_of(plan, body)

    def _edit(edit, action: str, body: dict, who: auth.Principal):
        """Run in a worker thread. Only the read-modify-write holds the plan lock; the board sent
        back (issues and all) is built after it is released."""
        with db.plan_lock():                  # the working timetable's: an HOD's and the admin's plan alike
            plan = _current_plan(db)
            try:
                new = edit(plan, db.get_settings(), body, principal=who)
            except B.StaleError as e:
                stale = e
            except B.BoardError as e:
                raise HTTPException(e.status, e.message)
            else:
                stale = None
                before = M.normalise(plan)    # the edit read it, so it reads
                dept = _dept_of(before, body, who) or ""
                text = B.summary(action, before, body, dept)
                db.set_board_plan(new)        # keeps the history (any other plan write clears it); moves the rev
                db.plan_history_push({"user": who.user_id, "rev": new["rev"], "dept": dept, "text": text,
                                      "changes": M.plan_changes(before, new)}, keep=BOARD_HISTORY_KEEP)
                _record(db, who, dept, text, new["rev"])
        if stale is not None:
            return _stale(plan, body, who, stale, "a moment ago")
        return _board(new, *B.where(plan, body), who)

    def _undo(body: dict, who: auth.Principal):
        """Undo `who`'s newest board change. The row leaves the history only when the undo is made,
        or when it is stale (StaleError, UndoGone: someone else's change is in the way, so it can
        never be undone); any other refusal (a lock, a share, the department) leaves it there to try
        again."""
        with db.plan_lock():
            plan = _current_plan(db)
            if not who.is_admin and M.department_status(plan, who.dept)["status"] == "submitted":
                raise HTTPException(403, f"{who.dept} is submitted: ask the timetabler to reopen it")
            while True:
                found = db.plan_history_last(match=lambda row: _undo_row(row, who))
                if found is None:
                    raise HTTPException(404, "nothing to undo")
                n, row = found
                if "changes" in row:
                    break
                db.plan_history_remove(n)     # a whole plan from before per-user undo: dropped, never applied
            stale = None
            try:
                new = B.undo(plan, row, principal=who, own=_own_undos(db, who), settings=db.get_settings())
            except B.StaleError as e:         # the row is dropped: someone else's change is in the way
                stale = e
            except B.UndoGone as e:           # the same, for an item without a rev_at to name who
                db.plan_history_remove(n)
                raise HTTPException(e.status, e.message)
            except B.BoardError as e:         # a lock, a share, the department today: the row stays
                raise HTTPException(e.status, e.message)
            db.plan_history_remove(n)
            if stale is None:
                dept, text = row.get("dept") or "", row.get("text") or "a change"
                db.set_board_plan(new)
                _record(db, who, dept, f"undid: {text}", new["rev"], undo=True)
        if stale is not None:
            return _stale(plan, body, who, stale, "after your change: it cannot be undone")
        return _board(new, *B.where(new, body), who)

    # before /api/plan/board/{action}, which would otherwise take "undo" as an action
    @r.post("/api/plan/board/undo")
    async def board_undo(request: Request, who: auth.Principal = Depends(auth.require_user)):
        body = await _json_object(request, optional=True)
        # in a worker thread (with the request's pin): the plan lock may be held by another writer
        return await run_in_threadpool(_undo, body, who)

    @r.post("/api/plan/board/{action}")
    async def board_edit(action: str, request: Request, who: auth.Principal = Depends(auth.require_user)):
        edit = BOARD_ACTIONS.get(action)
        if edit is None:
            raise HTTPException(404, f"no board action {action!r}")
        body = await _json_object(request)
        return await run_in_threadpool(_edit, edit, action, body, who)

    @r.get("/api/plan/activity")
    def plan_activity(dept: str | None = None, who: auth.Principal = Depends(auth.require_user)):
        """The board's changes, newest first: any department's (or all) for the admin, a head of
        department's own department's for them."""
        if not who.is_admin:
            dept = who.dept
        log = db.get_value("plan_activity") or []
        return {"activity": [a for a in reversed(log) if not dept or a.get("dept") == dept]}

    @r.get("/api/plan/issues")
    def get_issues(sid: str = Depends(auth.require_session)):
        plan = _current_plan(db)
        return {"issues": [_issue_dict(i) for i in _issues_for(db, plan)]}

    return r
