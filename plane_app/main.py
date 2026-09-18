"""The app: pages, API, and the wiring between db, engine and model provider."""
from __future__ import annotations

import asyncio
import os
import threading
import time
from collections import defaultdict, deque
from pathlib import Path

from fastapi import Depends, FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from . import auth
from . import bookings
from . import calendar as cal_mod
from . import changes
from .bookings import draft_for_engine, live_for_engine     # re-exported: every route/tool that sends an organisation to the engine uses these
from .chat import run_chat
from .config import Config
from .db import DEFAULT_SOFT, MAX_TIME_LIMIT, MIN_TIME_LIMIT, PRESETS, RULES, Db, mask_settings
from .engine_client import EngineClient, EngineError
from .extract import UnsupportedFile, extract
from . import asc_import
from .intake import IntakeError, apply_patch, clone_for_rebuild, empty_organisation, extract_organisation, summarise
from .llm import ProviderError, make_provider
from . import proposals
from .promote import promote_build

HERE = Path(__file__).parent
MAX_UPLOAD = 20 * 1024 * 1024
LOGIN_MAX_FAILURES, LOGIN_WINDOW = 10, 15 * 60      # failed logins per client ip before a 429, seconds


def _unlink_quietly(path: str) -> None:
    try:
        Path(path).unlink(missing_ok=True)
    except OSError:
        pass


def _clean_solve_settings(given: dict, current: dict) -> dict:
    """Keep the solve group well-formed whatever the client sent: a known preset, a time limit within the
    engine's cap, and integer weights for the six rules (anything else falls back to the stored value)."""
    out = {}
    preset = given.get("preset", current.get("preset", "balanced"))
    out["preset"] = preset if preset in PRESETS or preset == "custom" else current.get("preset", "balanced")
    try:
        limit = int(given.get("time_limit", current.get("time_limit", 300)))
    except (TypeError, ValueError):
        limit = int(current.get("time_limit", 300))
    out["time_limit"] = min(max(limit, MIN_TIME_LIMIT), MAX_TIME_LIMIT)
    weights = given.get("weights")
    base = dict(current.get("weights") or DEFAULT_SOFT)
    if isinstance(weights, dict):
        for r in RULES:
            v = weights.get(r, base.get(r, DEFAULT_SOFT[r]))
            base[r] = max(int(v), 0) if isinstance(v, (int, float)) and not isinstance(v, bool) else base.get(r, DEFAULT_SOFT[r])
    out["weights"] = {r: base.get(r, DEFAULT_SOFT[r]) for r in RULES}
    return out


def _default_engine_factory(config: Config):
    def factory(settings: dict) -> EngineClient:
        return EngineClient(settings["engine"]["url"] or config.engine_url, settings["engine"]["key"] or config.engine_key)
    return factory


def create_app(config: Config, db: Db, engine_factory=None, provider_factory=None) -> FastAPI:
    app = FastAPI(title="plane-app", docs_url=None, redoc_url=None)
    app.state.config, app.state.db = config, db
    app.state.engine_factory = engine_factory or _default_engine_factory(config)
    app.state.provider_factory = provider_factory or make_provider
    app.state.chat_times: dict[str, deque] = defaultdict(deque)
    app.state.login_failures: dict[str, deque] = defaultdict(deque)     # client ip -> failed-attempt times
    app.state.solve_locks: dict[str, threading.Lock] = {}                # timetable id -> lock around start and promotion
    solve_locks_guard = threading.Lock()

    def solve_lock() -> threading.Lock:
        with solve_locks_guard:
            return app.state.solve_locks.setdefault(db.current_timetable(), threading.Lock())
    templates = Jinja2Templates(directory=str(HERE / "templates"))
    static = HERE / "static"
    static.mkdir(exist_ok=True)
    app.mount("/static", StaticFiles(directory=str(static)), name="static")
    (config.data_dir / "uploads").mkdir(parents=True, exist_ok=True)

    def engine() -> EngineClient:
        return app.state.engine_factory(db.get_settings())

    def provider():
        try:
            return app.state.provider_factory(db.get_settings())
        except ProviderError as e:
            raise HTTPException(400, str(e))

    @app.exception_handler(HTTPException)
    async def _http_exc(request: Request, exc: HTTPException):
        if exc.status_code == 303:
            return RedirectResponse(exc.headers["Location"], status_code=303)
        return JSONResponse({"detail": exc.detail}, status_code=exc.status_code)

    # ---- health and auth ---------------------------------------------------
    @app.get("/healthz")
    def healthz():
        return {"ok": True}

    @app.get("/login", response_class=HTMLResponse)
    def login_page(request: Request):
        return templates.TemplateResponse(request, "login.html", {"error": None})

    def _client_ip(request: Request) -> str:
        forwarded = request.headers.get("x-forwarded-for", "").split(",")[0].strip()
        return forwarded or (request.client.host if request.client else "unknown")

    @app.post("/login")
    async def login(request: Request, password: str = Form("")):
        ip, now = _client_ip(request), time.monotonic()
        failures = app.state.login_failures[ip]
        while failures and now - failures[0] > LOGIN_WINDOW:
            failures.popleft()
        if len(failures) >= LOGIN_MAX_FAILURES:
            return templates.TemplateResponse(request, "login.html", {"error": "Too many attempts, try again later."}, status_code=429)
        if not auth.constant_time_equals(password, config.admin_password):
            failures.append(now)
            if len(app.state.login_failures) > 1000:       # forget clients whose window has passed
                for k in [k for k, dq in app.state.login_failures.items() if not dq or now - dq[-1] > LOGIN_WINDOW]:
                    del app.state.login_failures[k]
            await asyncio.sleep(1)
            return templates.TemplateResponse(request, "login.html", {"error": "Wrong password."}, status_code=200)
        app.state.login_failures.pop(ip, None)
        resp = RedirectResponse("/", status_code=303)
        forwarded_proto = request.headers.get("x-forwarded-proto", "").split(",")[0].strip()
        secure = request.url.scheme == "https" or forwarded_proto == "https"
        resp.set_cookie(auth.COOKIE, auth.make_session_cookie(config.secret_key, auth.new_session_id()),
                        max_age=auth.MAX_AGE, httponly=True, samesite="lax", secure=secure)
        return resp

    @app.post("/logout")
    def logout():
        resp = RedirectResponse("/login", status_code=303)
        resp.delete_cookie(auth.COOKIE)
        return resp

    # ---- pages ---------------------------------------------------------------
    @app.get("/", response_class=HTMLResponse)
    def index(request: Request, sid: str = Depends(auth.require_session)):
        return templates.TemplateResponse(request, "index.html", {})

    @app.get("/settings", response_class=HTMLResponse)
    def settings_page(request: Request, sid: str = Depends(auth.require_session)):
        return templates.TemplateResponse(request, "settings.html", {})

    # ---- settings ------------------------------------------------------------
    @app.get("/api/settings")
    def get_settings(sid: str = Depends(auth.require_session)):
        return mask_settings(db.get_settings())

    @app.put("/api/settings")
    def put_settings(body: dict, sid: str = Depends(auth.require_session)):
        current = db.get_settings()
        if not isinstance(body, dict):
            body = {}
        groups = {k: (dict(g) if isinstance(g := body.get(k), dict) else {}) for k in ("time", "rules", "solve", "provider", "engine", "calendar")}
        groups["solve"] = _clean_solve_settings(groups["solve"], current.get("solve", {}))
        groups["calendar"] = cal_mod.clean(groups["calendar"], current.get("calendar", {}))
        for group, field in (("provider", "api_key"), ("engine", "key")):
            v = groups[group].get(field, "")
            if not isinstance(v, str):
                groups[group].pop(field, None)
            elif v.startswith("***"):
                groups[group][field] = current.get(group, {}).get(field, "")
        # A provider key belongs to one provider: switching kind without a fresh key stores no key.
        new_kind = groups["provider"].get("kind")
        if isinstance(new_kind, str) and new_kind != current.get("provider", {}).get("kind"):
            submitted = body.get("provider", {}).get("api_key", "") if isinstance(body.get("provider"), dict) else ""
            if not isinstance(submitted, str) or not submitted or submitted.startswith("***"):
                groups["provider"]["api_key"] = ""
        merged = {k: {**current.get(k, {}), **groups[k]} for k in groups}
        db.set_settings(merged)
        return mask_settings(merged)

    # ---- timetable data ------------------------------------------------------
    @app.get("/api/solid")
    def get_solid(sid: str = Depends(auth.require_session)):
        live, draft = db.get_org("live"), db.get_org("draft")
        cur = db.current_timetable()
        return {"organisation": live, "draft": summarise(draft) if draft else None, "check": db.get_value("last_check"),
                "labels": db.get_settings()["time"]["labels"],
                "timetable": {"id": cur, "name": next((t["name"] for t in db.timetables() if t["id"] == cur), cur)}}

    @app.post("/api/upload")
    async def upload(sid: str = Depends(auth.require_session), files: list[UploadFile] = File(...),
                     mode: str = Form("keep"), classes_as_planes: str = Form("")):
        # Extract everything first: an UnsupportedFile in any of them must 400 before anything is stored.
        pending = []
        asc_pages: list[list[dict]] = []
        asc_names: list[str] = []
        for f in files:
            data = await f.read()
            if len(data) > MAX_UPLOAD:
                raise HTTPException(400, f"{f.filename} is over 20 MB")
            name = f.filename or "upload"
            if name.lower().endswith(".pdf") and asc_import.is_asc_pdf(data):
                # a timetable-software export: read the grid geometry, no model needed
                asc_pages.extend(asc_import.read_pdf_words(data))
                asc_names.append(name)
                pending.append((name, data, None))
                continue
            try:
                ex = extract(name, data)
            except UnsupportedFile as e:
                raise HTTPException(400, str(e))
            pending.append((name, data, ex))
        if asc_pages:
            if mode not in ("keep", "rebuild"):
                raise HTTPException(400, "mode must be keep or rebuild")
            try:
                org, notes = asc_import.build_organisation(asc_pages, mode=mode, classes_as_planes=bool(classes_as_planes))
            except asc_import.AscFormatError as e:
                raise HTTPException(400, str(e))
            try:
                org = apply_patch(org, {})            # validates like every other draft
            except IntakeError as e:
                raise HTTPException(400, f"The timetable export could not be turned into a draft: {e}")
            for name, data, ex in pending:
                for old_path in db.remove_upload_by_name(sid, name):
                    _unlink_quietly(old_path)
                path = config.data_dir / "uploads" / f"{auth.new_session_id()}.pdf"
                path.write_bytes(data)
                db.add_upload(sid, name, str(path), "asc" if ex is None else ex.kind, "" if ex is None else ex.text)
            others = [name for name, _, ex in pending if ex is not None]
            if others:
                notes.append({"section": "documents", "source": ", ".join(others),
                              "note": "Ignored alongside the timetable export; the export already holds the timetable. Upload other documents on their own to add to the draft by chat."})
            db.set_org("draft", org)
            s = summarise(org)
            return {"draft": s, "notes": notes, "warnings": [], "source": "asc", "files": asc_names}
        warnings = []
        for name, data, ex in pending:
            for old_path in db.remove_upload_by_name(sid, name):     # re-dropping a file replaces it
                _unlink_quietly(old_path)
            path = config.data_dir / "uploads" / f"{auth.new_session_id()}.{ex.kind}"
            path.write_bytes(data)
            db.add_upload(sid, name, str(path), ex.kind, ex.text)
            if ex.warning:
                warnings.append(f"{name}: {ex.warning}")
        texts = [(u["name"], u["text"]) for u in db.uploads(sid)]
        try:
            org, notes = extract_organisation(provider(), db.get_settings(), texts)
        except (IntakeError, ProviderError) as e:
            raise HTTPException(400, str(e))
        db.set_org("draft", org)
        return {"draft": summarise(org), "notes": notes, "warnings": warnings}

    @app.post("/api/uploads/clear")
    def clear_uploads(sid: str = Depends(auth.require_session)):
        for path in db.clear_uploads(sid):
            _unlink_quietly(path)
        return {"ok": True}

    @app.post("/api/draft/new")
    def new_draft(sid: str = Depends(auth.require_session)):
        """An empty draft to fill from criteria, by chat or by hand."""
        org = empty_organisation(db.get_settings())
        db.set_org("draft", org)
        return summarise(org)

    # ---- timetable instances ---------------------------------------------------
    def _timetables_payload() -> dict:
        return {"current": db.current_timetable(), "items": db.timetables()}

    @app.get("/api/timetables")
    def list_timetables(sid: str = Depends(auth.require_session)):
        return _timetables_payload()

    @app.post("/api/timetables")
    def create_timetable(body: dict, sid: str = Depends(auth.require_session)):
        name = str(body.get("name") or "").strip() if isinstance(body, dict) else ""
        if not name:
            raise HTTPException(400, "give the timetable a name")
        source = body.get("clone_from") if isinstance(body, dict) else None
        cloned = None
        if source:
            known = {t["id"] for t in db.timetables()}
            if source not in known:
                raise HTTPException(404, f"no timetable {source}")
            db.select_timetable(source)
            src_settings, src_org = db.get_settings(), (db.get_org("live") or db.get_org("draft"))
            cloned = source
        tid = db.create_timetable(name)
        if cloned:
            db.set_settings(src_settings)           # time and rules travel with the clone; provider/engine are global anyway
            if src_org:
                db.set_org("draft", clone_for_rebuild(src_org))
        return {"id": tid, "name": name, "cloned_from": cloned, **_timetables_payload()}

    @app.post("/api/timetables/{tid}/select")
    def select_timetable(tid: str, sid: str = Depends(auth.require_session)):
        try:
            db.select_timetable(tid)
        except KeyError:
            raise HTTPException(404, f"no timetable {tid}")
        return _timetables_payload()

    @app.patch("/api/timetables/{tid}")
    def rename_timetable(tid: str, body: dict, sid: str = Depends(auth.require_session)):
        name = str(body.get("name") or "").strip() if isinstance(body, dict) else ""
        if not name:
            raise HTTPException(400, "give the timetable a name")
        if tid not in {t["id"] for t in db.timetables()}:
            raise HTTPException(404, f"no timetable {tid}")
        db.rename_timetable(tid, name)
        return _timetables_payload()

    @app.delete("/api/timetables/{tid}")
    def delete_timetable(tid: str, sid: str = Depends(auth.require_session)):
        try:
            paths = db.delete_timetable(tid)
        except KeyError:
            raise HTTPException(404, f"no timetable {tid}")
        except ValueError as e:
            raise HTTPException(400, str(e))
        for path in paths:
            _unlink_quietly(path)
        return _timetables_payload()

    @app.get("/api/draft")
    def get_draft(sid: str = Depends(auth.require_session)):
        d = db.get_org("draft")
        if d is None:
            raise HTTPException(404, "no draft")
        return d

    @app.post("/api/draft/patch")
    def patch_draft(body: dict, sid: str = Depends(auth.require_session)):
        d = db.get_org("draft")
        if d is None:
            raise HTTPException(404, "no draft")
        try:
            new = apply_patch(d, body.get("patch") or {})
        except IntakeError as e:
            raise HTTPException(400, str(e))
        db.set_org("draft", new)
        return summarise(new)

    @app.post("/api/build")
    def build_draft_route(sid: str = Depends(auth.require_session)):
        d = draft_for_engine(db)
        if d is None:
            raise HTTPException(404, "no draft to build")
        try:
            result = engine().build(d)
        except EngineError as e:
            raise HTTPException(502, e.detail)
        result["organisation"] = bookings.strip(result["organisation"])
        return {**result, "ok": promote_build(db, result)}

    @app.post("/api/query")
    def query(body: dict, sid: str = Depends(auth.require_session)):
        live = live_for_engine(db)
        if live is None:
            raise HTTPException(404, "no live timetable")
        try:
            return engine().query(live, body.get("kind", ""), body.get("args") or {})
        except EngineError as e:
            raise HTTPException(502 if e.status in (0, 500) else e.status, e.detail)

    @app.get("/api/loads")
    def loads(sid: str = Depends(auth.require_session)):
        live = live_for_engine(db)
        if live is None:
            raise HTTPException(404, "no live timetable")
        try:
            return engine().loads(live)
        except EngineError as e:
            raise HTTPException(502 if e.status in (0, 500) else e.status, e.detail)

    # ---- solve jobs ------------------------------------------------------------
    # One solve at a time per timetable. The record under "solve" holds the engine job id, the preset,
    # the live timetable's score before the solve and, once the job has finished, its summary and
    # whether it was promoted, so a finished solve is served from the db and never asks the engine again.
    def _engine_error(e: EngineError) -> HTTPException:
        return HTTPException(502 if e.status in (0, 500) else e.status, e.detail)

    def _solve_weights(settings: dict, preset: str) -> dict:
        return dict(PRESETS[preset]) if preset in PRESETS else dict(settings["solve"]["weights"])

    def _solve_summary(job: dict, record: dict) -> dict:
        """What the browser gets: the job's progress plus the record; the whole organisation stays server-side."""
        result = job.get("result")
        if result is not None:
            result = {k: v for k, v in result.items() if k != "organisation"}
        # `elapsed` is the solver's own clock (it starts after the model is built); `running_for` is wall time
        # since the job was queued, measured here so the browser needs no clock of its own.
        return {"job": record["job"], "preset": record["preset"], "time_limit": record["time_limit"], "started": record["started"],
                "running_for": round(time.time() - record["started"], 1),
                "before": record.get("before"), "status": job["status"], "elapsed": job.get("elapsed", 0),
                "best_objective": job.get("best_objective"), "bound": job.get("bound"), "result": result,
                "error": job.get("error"), "promoted": record.get("promoted", False)}

    @app.post("/api/solve", status_code=202)
    def start_solve(body: dict | None = None, sid: str = Depends(auth.require_session)):
        d = draft_for_engine(db)
        if d is None:
            raise HTTPException(404, "no draft to solve")
        body = body if isinstance(body, dict) else {}
        settings = db.get_settings()
        preset = body.get("preset", settings["solve"]["preset"])
        if preset not in PRESETS and preset != "custom":
            raise HTTPException(400, f"preset must be one of {', '.join(PRESETS)} or custom")
        try:
            time_limit = int(body.get("time_limit", settings["solve"]["time_limit"]))
        except (TypeError, ValueError):
            raise HTTPException(400, "time_limit must be a whole number of seconds")
        if not MIN_TIME_LIMIT <= time_limit <= MAX_TIME_LIMIT:
            raise HTTPException(400, f"time_limit must be between {MIN_TIME_LIMIT} and {MAX_TIME_LIMIT} seconds")
        eng = engine()
        with solve_lock():                                      # two clicks at once start one job, not two
            record = db.get_value("solve")
            if record and "final" not in record:
                try:
                    if eng.job(record["job"])["status"] in ("queued", "running"):
                        raise HTTPException(409, "a solve is already running for this timetable; wait or stop it")
                except EngineError:
                    pass                                        # the engine forgot it: start afresh
            live, weights = live_for_engine(db), _solve_weights(settings, preset)
            before = None
            try:
                if live is not None:
                    before = eng.score(live, None, weights)
                job = eng.solve(d, previous=live, time_limit=time_limit, weights=weights,
                                hint="previous" if preset == "close" else "greedy")
            except EngineError as e:
                raise _engine_error(e)
            db.set_value("solve", {"job": job["job"], "preset": preset, "time_limit": time_limit, "weights": weights,
                                   "started": time.time(), "before": before, "promoted": False})
        return {"job": job["job"], "preset": preset, "time_limit": time_limit}

    @app.get("/api/solve")
    def solve_status(sid: str = Depends(auth.require_session)):
        record = db.get_value("solve")
        if record is None:
            raise HTTPException(404, "no solve job for this timetable")
        if "final" in record:
            return _solve_summary(record["final"], record)
        try:
            job = engine().job(record["job"])
        except EngineError as e:
            if e.status == 404:
                job = {"status": "failed", "error": "the engine no longer has this job"}
            else:
                raise _engine_error(e)
        if job["status"] in ("done", "failed", "cancelled"):
            with solve_lock():                                  # concurrent polls promote once
                record = db.get_value("solve")
                if record is None or record["job"] != job["id"]:
                    raise HTTPException(409, "the solve changed underneath this request; read again")
                if "final" in record:
                    return _solve_summary(record["final"], record)
                # A cancelled job keeps the best solution found so far; it is promoted like a finished one.
                if job.get("result") is not None and db.get_org("draft") is not None:
                    job["result"]["organisation"] = bookings.strip(job["result"]["organisation"])
                    record["promoted"] = promote_build(db, job["result"])
                record["final"] = {k: v for k, v in job.items() if k != "trace"}
                if record["final"].get("result") is not None:
                    record["final"]["result"] = {k: v for k, v in record["final"]["result"].items() if k != "organisation"}
                db.set_value("solve", record)
        return _solve_summary(job, record)

    @app.post("/api/solve/cancel")
    def cancel_solve(sid: str = Depends(auth.require_session)):
        record = db.get_value("solve")
        if record is None or "final" in record:
            raise HTTPException(409, "no solve is running for this timetable")
        try:
            return engine().cancel_job(record["job"])
        except EngineError as e:
            raise _engine_error(e)

    @app.get("/api/score")
    def score_live(sid: str = Depends(auth.require_session)):
        """The live timetable's soft-rule score under the current weights (no previous: stability is trivially full)."""
        live = live_for_engine(db)
        if live is None:
            raise HTTPException(404, "no live timetable")
        settings = db.get_settings()
        try:
            return engine().score(live, None, _solve_weights(settings, settings["solve"]["preset"]))
        except EngineError as e:
            raise _engine_error(e)

    # ---- bookings --------------------------------------------------------------
    @app.get("/api/bookings")
    def list_bookings(sid: str = Depends(auth.require_session)):
        s = db.get_settings()
        items = bookings.list_all(db)
        return {"items": items, "describe": {b["id"]: cal_mod.describe(s["calendar"], s["time"], b["date"]) for b in items}}

    @app.delete("/api/bookings/{bid}")
    def delete_booking(bid: str, sid: str = Depends(auth.require_session)):
        def snapshot_before_removal(booking: dict) -> None:
            # runs before the removal is persisted, so the change log's "before" snapshot still
            # includes the booking (undo must be able to restore it).
            changes.record(db, "booking_removed", f"Removed booking: {booking['title']} on {booking['date']}")

        if bookings.remove(db, bid, before_persist=snapshot_before_removal) is None:
            raise HTTPException(404, "no such booking")
        return {"ok": True}

    # ---- proposals ---------------------------------------------------------
    # The browser can only apply what the assistant has already put in the pending list, by id.
    @app.post("/api/proposals/apply")
    def apply_proposal(body: dict, sid: str = Depends(auth.require_session)):
        pid = str((body or {}).get("id", ""))
        if not any(p["id"] == pid for p in proposals.pending(db, sid)):
            raise HTTPException(404, "nothing pending with that id")
        try:
            return proposals.apply(db, engine(), pid, sid)
        except EngineError as e:
            raise _engine_error(e)

    @app.post("/api/proposals/dismiss")
    def dismiss_proposal(sid: str = Depends(auth.require_session)):
        proposals.clear_pending(db, sid)
        return {"ok": True}

    @app.get("/api/changes")
    def list_changes(sid: str = Depends(auth.require_session)):
        return {"items": changes.list_all(db)}

    @app.post("/api/undo")
    def undo_change(sid: str = Depends(auth.require_session)):
        undone = changes.undo(db)
        return {"ok": undone is not None, "undone": undone}

    # ---- chat ----------------------------------------------------------------
    @app.post("/api/chat")
    def chat(body: dict, sid: str = Depends(auth.require_session)):
        now = time.monotonic()
        times = app.state.chat_times[sid]
        while times and now - times[0] > 60:
            times.popleft()
        if not times:
            app.state.chat_times.pop(sid, None)
        if len(app.state.chat_times.get(sid, ())) >= 30:
            raise HTTPException(429, "Slow down: 30 messages per minute.")
        app.state.chat_times[sid].append(now)
        if len(app.state.chat_times) > 1000:
            stale = [k for k, dq in app.state.chat_times.items() if not dq or now - dq[-1] > 60]
            for k in stale:
                del app.state.chat_times[k]
        text = (body.get("text") or "").strip()
        if not text:
            raise HTTPException(400, "empty message")
        try:
            res = run_chat(db, sid, provider(), engine(), text)
        except ProviderError as e:
            raise HTTPException(400, str(e))
        return {"text": res.text, "events": res.events}

    @app.get("/api/messages")
    def messages(sid: str = Depends(auth.require_session)):
        return db.messages(sid)

    @app.post("/api/messages/clear")
    def clear(sid: str = Depends(auth.require_session)):
        db.clear_messages(sid)
        return {"ok": True}

    @app.get("/api/usage")
    def usage(sid: str = Depends(auth.require_session)):
        try:
            return engine().usage()
        except EngineError as e:
            return {"error": e.detail}

    return app


def _bootstrap() -> FastAPI:
    cfg = Config.from_env()
    return create_app(cfg, Db(cfg.data_dir / "app.db"))


# Set PLANE_APP_BOOT=0 (the test conftest does) to import this module without touching the filesystem.
app = _bootstrap() if os.environ.get("PLANE_APP_BOOT", "1") == "1" else None
