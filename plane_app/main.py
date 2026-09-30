"""The app: pages, API, and the wiring between db, engine and model provider."""
from __future__ import annotations

import asyncio
import os
import threading
import time
from collections import defaultdict, deque
from pathlib import Path

from fastapi import Depends, FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from . import auth
from .assets import make_asset_url
from . import users
from . import bookings
from . import calendar as cal_mod
from . import changes
from .bookings import draft_for_engine, live_for_engine     # re-exported: every route/tool that sends an organisation to the engine uses these
from .chat import run_chat
from .config import MAX_UPLOAD, Config
from .db import MAX_TIME_LIMIT, MIN_TIME_LIMIT, PRESETS, THREAD, Db, mask_settings
from .engine_client import EngineClient, EngineError
from .extract import UnsupportedFile, extract
from . import asc_import
from .grid_api import make_router as grid_router
from .learning.routes import make_router as learning_router
from .intake import IntakeError, apply_patch, clone_for_rebuild, empty_organisation, extract_organisation, summarise
from .llm import ProviderError, make_provider
from . import periods
from . import proposals
from .plan import importer as plan_importer
from .plan import model as plan_model
from .plan.model import vocabulary_of
from .periods_api import make_router as periods_router
from .plan.routes import import_workbook as import_plan_workbook, make_router as plan_router
from .print.routes import make_router as print_router
from .learning import log as decisions
from .promote import promote_build
from .relief_api import make_router as relief_router
from .wizard.routes import make_router as wizard_router

HERE = Path(__file__).parent
LOGIN_MAX_FAILURES, LOGIN_WINDOW = 10, 15 * 60      # failed logins per client ip before a 429, seconds
USER_MAX_FAILURES = 5          # failed logins per department username (from any ip), and failed current-password
                               # checks per account on the password change, within LOGIN_WINDOW before a 429
HASH_CONCURRENCY = 3           # at most this many login scrypt checks at once (each ~16 MB, tens of ms)
HASH_MAX_WAITING = 20          # logins queued for a hash slot beyond this are answered 429 at once
COUNTER_MAX_KEYS = 5000        # each failure-counter dict keeps at most this many keys (see _make_room)
TOO_MANY = "Too many attempts, try again later."
CHAT_LOCK_TIMEOUT = 60    # seconds a /api/chat request waits for another device's turn on this timetable


class HashGate:
    """Per app: runs password hash checks in the threadpool, a few at a time, so a burst of logins
    can neither block the event loop nor take all the memory; and counts who is waiting. The
    semaphore is made at first use on the running loop (never at import), and remade if the loop
    changes, so it is never used from a loop it is not bound to. A slot is given back when the hash
    really ends, not when the request does: a cancelled request's scrypt keeps its slot."""

    def __init__(self):
        self.waiting = 0
        self._loop = self._slots = None
        self._running: set[asyncio.Task] = set()     # keeps a cancelled request's hash task alive

    def full(self) -> bool:
        return self.waiting >= HASH_MAX_WAITING

    async def run(self, fn, *args):
        loop = asyncio.get_running_loop()
        if self._loop is not loop:
            self._loop, self._slots = loop, asyncio.Semaphore(HASH_CONCURRENCY)
        slots = self._slots
        self.waiting += 1
        try:
            await slots.acquire()
        finally:
            self.waiting -= 1
        # No await between the acquire and the task: once the slot is taken, the hash task exists and
        # its done-callback is the only thing that gives the slot back.
        task = asyncio.ensure_future(run_in_threadpool(fn, *args))
        self._running.add(task)

        def done(t: asyncio.Task) -> None:
            self._running.discard(t)
            slots.release()
            if not t.cancelled():
                t.exception()                          # retrieved: no "never retrieved" warning after a cancel
        task.add_done_callback(done)
        return await asyncio.shield(task)


def _recent(times: deque | None, now: float) -> int:
    """How many of `times` fall within the window ending now (older ones are dropped)."""
    if times is None:
        return 0
    while times and now - times[0] > LOGIN_WINDOW:
        times.popleft()
    return len(times)


def _make_room(counters: dict, key: str, now: float, limit: int) -> bool:
    """Whether `key` can be counted: it is already there, or there is room, or room can be made.
    Room is made from keys whose window has passed, then from the stalest keys below `limit`; a key
    at or over its limit is never dropped (a flood of new names or addresses must not unlock it).
    False when every key is locked: the caller refuses the attempt instead."""
    if key in counters or len(counters) < COUNTER_MAX_KEYS:
        return True
    for k in [k for k, times in counters.items() if _recent(times, now) == 0]:
        del counters[k]
    for k in list(counters):                           # stalest first (_reserve moves a key to the end)
        if len(counters) < COUNTER_MAX_KEYS:
            break
        if len(counters[k]) < limit:
            del counters[k]
    return len(counters) < COUNTER_MAX_KEYS


def _reserve(counters: dict, key: str, now: float) -> None:
    """Count an attempt before it is checked, so concurrent attempts cannot all pass the limit.
    The key moves to the end (the freshest). Call `_make_room` first."""
    times = counters.pop(key, None) or deque()
    times.append(now)
    counters[key] = times


def _release(counters: dict, key: str, now: float) -> None:
    """Take back a reservation (the attempt did not fail)."""
    times = counters.get(key)
    if times is not None:
        try:
            times.remove(now)
        except ValueError:
            pass
        if not times:
            counters.pop(key, None)


def _unlink_quietly(path: str) -> None:
    try:
        Path(path).unlink(missing_ok=True)
    except OSError:
        pass


def _default_engine_factory(config: Config):
    def factory(settings: dict) -> EngineClient:
        return EngineClient(settings["engine"]["url"] or config.engine_url, settings["engine"]["key"] or config.engine_key)
    return factory


def create_app(config: Config, db: Db, engine_factory=None, provider_factory=None) -> FastAPI:
    app = FastAPI(title="plane-app", docs_url=None, redoc_url=None, openapi_url=None)
    app.state.config, app.state.db = config, db
    app.state.engine_factory = engine_factory or _default_engine_factory(config)
    app.state.provider_factory = provider_factory or make_provider
    app.state.chat_times: dict[str, deque] = defaultdict(deque)
    # One lock per timetable's chat thread: two devices posting to /api/chat for the same timetable
    # at once must not interleave — a tool_use with its tool_result split across two concurrent
    # run_chat calls would otherwise land with a user message wedged between them (see history_for_provider).
    app.state.chat_locks: dict[str, threading.Lock] = defaultdict(threading.Lock)
    # Failure counters: key -> attempt times within LOGIN_WINDOW. An attempt is reserved (counted)
    # before its password is checked and taken back if it succeeds.
    # (client ip, "admin" | "named"): department logins (typos included) never lock the admin's
    # login out from behind the same address (a school's NAT), nor the other way round
    app.state.login_failures: dict[tuple[str, str], deque] = {}
    app.state.user_login_failures: dict[str, deque] = {}     # well-formed department username
    app.state.password_failures: dict[str, deque] = {}       # account id -> wrong current passwords
    app.state.hash_gate = HashGate()
    password_failures_guard = threading.Lock()                          # held only briefly, never across an await
    app.state.solve_locks: dict[str, threading.Lock] = {}                # timetable id -> lock around start and promotion
    solve_locks_guard = threading.Lock()

    def solve_lock() -> threading.Lock:
        with solve_locks_guard:
            return app.state.solve_locks.setdefault(db.current_timetable(), threading.Lock())
    templates = Jinja2Templates(directory=str(HERE / "templates"))
    templates.env.globals["asset"] = make_asset_url(HERE / "static")   # versioned /static URLs (assets.py)
    app.include_router(print_router(db, templates))
    app.include_router(plan_router(db))
    app.include_router(grid_router(db))
    app.include_router(learning_router(db))
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

    app.include_router(wizard_router(db, templates, engine))

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
        """The rightmost X-Forwarded-For entry: the hop our proxy appended, which the client cannot
        choose (anything left of it is whatever the client sent). Without the header, the peer."""
        forwarded = request.headers.get("x-forwarded-for", "").split(",")[-1].strip()
        return forwarded or (request.client.host if request.client else "unknown")

    def _set_session(request: Request, resp, sid: str, user: str = auth.ADMIN, v: int = 0) -> None:
        forwarded_proto = request.headers.get("x-forwarded-proto", "").split(",")[0].strip()
        secure = request.url.scheme == "https" or forwarded_proto == "https"
        resp.set_cookie(auth.COOKIE, auth.make_session_cookie(config.secret_key, sid, user, v),
                        max_age=auth.MAX_AGE, httponly=True, samesite="lax", secure=secure)

    def _check_account(uname: str | None, password: str) -> tuple[dict | None, bool]:
        """(the account, whether the password is its own and it is active). Blocking (the users
        store, scrypt): the login runs it through the HashGate. An unknown or malformed name costs
        what a known one does."""
        account = users.find_by_username(db, uname) if uname else None
        if account is None:
            users.burn_time(password)
            return None, False
        return account, users.verify_password(password, account["password_hash"]) and bool(account.get("active"))

    @app.post("/login")
    async def login(request: Request, username: str = Form(""), password: str = Form("")):
        now = time.monotonic()
        uname = username.strip().lower()
        named = uname not in ("", auth.ADMIN)          # a department account (the admin has only the ip limit)
        well_formed = named and bool(users.USERNAME_RE.match(uname))     # only these get a username counter
        ip = (_client_ip(request), "named" if named else "admin")        # the ip counter, per kind of login

        def refuse():
            return templates.TemplateResponse(request, "login.html", {"error": TOO_MANY, "username": username}, status_code=429)
        if _recent(app.state.login_failures.get(ip), now) >= LOGIN_MAX_FAILURES:
            return refuse()
        if well_formed and _recent(app.state.user_login_failures.get(uname), now) >= USER_MAX_FAILURES:
            return refuse()
        gate: HashGate = app.state.hash_gate
        if named and gate.full():
            return refuse()
        if not _make_room(app.state.login_failures, ip, now, LOGIN_MAX_FAILURES):
            return refuse()                            # every address counted is locked: add none
        if well_formed and not _make_room(app.state.user_login_failures, uname, now, USER_MAX_FAILURES):
            return refuse()
        # reserve the attempt before any await: concurrent attempts see it and cannot all pass the limits
        _reserve(app.state.login_failures, ip, now)
        if well_formed:
            _reserve(app.state.user_login_failures, uname, now)
        account = None
        if not named:                                  # the admin: today's single password
            ok = auth.constant_time_equals(password, config.admin_password)
        else:                                          # the account read and the hash, off the event loop
            account, ok = await gate.run(_check_account, uname if well_formed else None, password)
        if not ok:                                     # the reservations stand as the failure
            await asyncio.sleep(1)
            error = "Wrong password." if not uname else "Wrong username or password."
            return templates.TemplateResponse(request, "login.html", {"error": error, "username": username}, status_code=200)
        resp = RedirectResponse("/", status_code=303)
        if account is None:
            app.state.login_failures.pop(ip, None)     # only the admin's own login clears the admin ip count
            _set_session(request, resp, auth.new_session_id())
        else:
            _release(app.state.login_failures, ip, now)
            app.state.user_login_failures.pop(uname, None)
            await run_in_threadpool(users.touch_login, db, account["id"])
            _set_session(request, resp, auth.new_session_id(), account["id"], int(account.get("session_version", 0)))
        return resp

    @app.post("/logout")
    def logout():
        resp = RedirectResponse("/login", status_code=303)
        resp.delete_cookie(auth.COOKIE)
        return resp

    # ---- who is logged in; department accounts -------------------------------
    def _timetable_ref() -> dict:
        cur = db.working_timetable()                    # an HOD's: the deployment timetable
        return {"id": cur, "name": next((t["name"] for t in db.timetables() if t["id"] == cur), cur)}

    @app.get("/api/me")
    def me(who: auth.Principal = Depends(auth.require_user)):
        return {"role": who.role, "name": who.name, "dept": who.dept, "must_change": who.must_change,
                "timetable": _timetable_ref()}

    @app.post("/api/me/password")
    async def change_my_password(request: Request, body: dict, who: auth.Principal = Depends(auth.require_user)):
        """Async: the scrypt work (the current password checked, the new one hashed) goes through
        the login's HashGate, a few at a time and off the event loop."""
        if who.is_admin:
            raise HTTPException(400, "the admin password is set in ADMIN_PASSWORD, not here")
        old, new = body.get("old"), body.get("new")
        if not isinstance(old, str) or not isinstance(new, str):
            raise HTTPException(400, "send the current password as old and the new one as new")
        now = time.monotonic()
        gate: HashGate = app.state.hash_gate
        if gate.full():
            raise HTTPException(429, TOO_MANY)
        with password_failures_guard:          # reserve the attempt before checking: a burst cannot all pass
            if _recent(app.state.password_failures.get(who.user_id), now) >= USER_MAX_FAILURES:
                raise HTTPException(429, TOO_MANY)
            if not _make_room(app.state.password_failures, who.user_id, now, USER_MAX_FAILURES):
                raise HTTPException(429, TOO_MANY)
            _reserve(app.state.password_failures, who.user_id, now)
        try:
            user = await gate.run(users.set_password, db, who.user_id, old, new)
        except users.WrongPassword as e:       # the reservation stands as the failure
            raise HTTPException(e.status, e.message)
        except users.UserError as e:
            with password_failures_guard:
                _release(app.state.password_failures, who.user_id, now)
            raise HTTPException(e.status, e.message)
        with password_failures_guard:
            app.state.password_failures.pop(who.user_id, None)
        resp = JSONResponse({"ok": True, "must_change": False})
        _set_session(request, resp, who.sid, user["id"], user["session_version"])     # this device stays logged in
        return resp

    def _user_error(e: users.UserError) -> HTTPException:
        return HTTPException(e.status, e.message)

    @app.get("/api/users")
    def list_users(sid: str = Depends(auth.require_session)):
        return {"users": users.list_users(db)}

    @app.post("/api/users")
    def create_user(body: dict, sid: str = Depends(auth.require_session)):
        try:
            user, temp = users.create_user(db, body.get("username"), body.get("name"), body.get("dept"))
        except users.UserError as e:
            raise _user_error(e)
        return {"user": user, "temp_password": temp}      # shown once: only its hash is kept

    @app.patch("/api/users/{uid}")
    def update_user(uid: str, body: dict, sid: str = Depends(auth.require_session)):
        unknown = sorted(set(body) - set(users.EDITABLE))
        if unknown:
            raise HTTPException(400, f"cannot change {', '.join(unknown)}")
        try:
            return {"user": users.update_user(db, uid, **body)}
        except users.UserError as e:
            raise _user_error(e)

    @app.post("/api/users/{uid}/reset")
    def reset_user_password(uid: str, sid: str = Depends(auth.require_session)):
        try:
            return {"temp_password": users.reset_password(db, uid)}
        except users.UserError as e:
            raise _user_error(e)

    @app.delete("/api/users/{uid}")
    def delete_user(uid: str, sid: str = Depends(auth.require_session)):
        try:
            users.delete_user(db, uid)
        except users.UserError as e:
            raise _user_error(e)
        return {"ok": True}

    # ---- pages ---------------------------------------------------------------
    @app.get("/", response_class=HTMLResponse)
    def index(request: Request, who: auth.Principal = Depends(auth.require_user)):
        if not who.is_admin:            # a head of department: their department's page
            return templates.TemplateResponse(request, "department.html", {
                "department": True, "name": who.name, "dept": who.dept or "",
                "timetable": _timetable_ref()["name"], "must_change": bool(who.must_change)})
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
        return mask_settings(db.store_settings(body))

    # ---- the deployment timetable (the one department accounts work on) --------
    def _deployment_payload() -> dict:
        """The deployment timetable as it is in effect (`stored`: fixed, else it follows the
        selection), and each department's sign-off in its plan, whichever timetable the admin has
        open: {dept: {status, by, at, accounts: active accounts of the department}}."""
        tid = users.deployment_timetable(db)
        plan = db.get_value("plan", tid=tid) or {}
        accounts: dict[str, int] = {}
        for u in users.list_users(db):
            if u.get("active") and u.get("dept"):
                accounts[u["dept"]] = accounts.get(u["dept"], 0) + 1
        depts = ({str(r.get("dept")) for r in plan.get("requirements") or [] if isinstance(r, dict) and r.get("dept")}
                 | {str(s.get("dept")) for s in plan.get("staff") or [] if isinstance(s, dict) and s.get("dept")}
                 | set(accounts))
        departments = {d: {**plan_model.department_status(plan, d), "accounts": accounts.get(d, 0)} for d in sorted(depts)}
        return {"timetable": tid, "name": next((t["name"] for t in db.timetables() if t["id"] == tid), tid),
                "stored": db.deployment_stored(), "rev": int(plan.get("rev") or 0), "departments": departments}

    @app.get("/api/deployment")
    def get_deployment(sid: str = Depends(auth.require_session)):
        return _deployment_payload()

    @app.put("/api/deployment")
    def put_deployment(body: dict, sid: str = Depends(auth.require_session)):
        tid = body.get("timetable")
        if not isinstance(tid, str) or not tid:
            raise HTTPException(400, "send the timetable id as timetable")
        if tid not in {t["id"] for t in db.timetables()}:
            raise HTTPException(404, f"no timetable {tid}")
        if db.get_value("period_base", tid=tid):
            raise HTTPException(400, "a period timetable cannot be the deployment timetable: choose its normal timetable")
        try:
            db.set_deployment_timetable(tid)
        except KeyError:                               # deleted in between
            raise HTTPException(404, f"no timetable {tid}")
        return _deployment_payload()

    # ---- timetable data ------------------------------------------------------
    @app.get("/api/solid")
    def get_solid(who: auth.Principal = Depends(auth.require_user)):
        live, draft = db.get_org("live"), (db.get_org("draft") if who.is_admin else None)     # an HOD sees no draft
        cur = db.working_timetable()
        return {"organisation": live, "draft": summarise(draft) if draft else None, "check": db.get_value("last_check"),
                "labels": db.get_settings()["time"]["labels"],
                "timetable": {"id": cur, "name": next((t["name"] for t in db.timetables() if t["id"] == cur), cur)},
                "vocabulary": vocabulary_of(db)}

    @app.post("/api/upload")
    async def upload(sid: str = Depends(auth.require_session), files: list[UploadFile] = File(...),
                     mode: str = Form("keep"), classes_as_planes: str = Form("")):
        # Extract everything first: an UnsupportedFile in any of them must 400 before anything is stored.
        pending = []
        asc_pages: list[list[dict]] = []
        asc_names: list[str] = []
        plan_notes: list[dict] = []
        plan_events: list[dict] = []
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
            if name.lower().endswith(".xlsx") and (plan_importer.is_deployment_workbook(data)
                                                    or plan_importer.is_generic_workbook(data)):
                # a staff-deployment or generic duties workbook: import into the curriculum plan, not the draft
                # in a worker thread: it waits on the plan lock, which must not stall the event loop
                _, _, note = await run_in_threadpool(import_plan_workbook, db, name, data)
                plan_notes.append({"section": "plan", "source": name, "note": note})
                plan_events.append({"kind": "plan_updated"})
                continue
            try:
                ex = extract(name, data)
            except UnsupportedFile as e:
                raise HTTPException(400, str(e))
            pending.append((name, data, ex))
        if not pending and not asc_pages:
            # nothing left to turn into a draft (only deployment workbook(s) were uploaded)
            return {"notes": plan_notes, "warnings": [], "events": plan_events}
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
                for old_path in db.remove_upload_by_name(THREAD, name):
                    _unlink_quietly(old_path)
                path = config.data_dir / "uploads" / f"{auth.new_session_id()}.pdf"
                path.write_bytes(data)
                db.add_upload(THREAD, name, str(path), "asc" if ex is None else ex.kind, "" if ex is None else ex.text)
            others = [name for name, _, ex in pending if ex is not None]
            if others:
                notes.append({"section": "documents", "source": ", ".join(others),
                              "note": "Ignored alongside the timetable export; the export already holds the timetable. Upload other documents on their own to add to the draft by chat."})
            db.set_org("draft", org)
            s = summarise(org)
            return {"draft": s, "notes": notes + plan_notes, "warnings": [], "source": "asc", "files": asc_names,
                    "events": plan_events}
        warnings = []
        for name, data, ex in pending:
            for old_path in db.remove_upload_by_name(THREAD, name):     # re-dropping a file replaces it
                _unlink_quietly(old_path)
            path = config.data_dir / "uploads" / f"{auth.new_session_id()}.{ex.kind}"
            path.write_bytes(data)
            db.add_upload(THREAD, name, str(path), ex.kind, ex.text)
            if ex.warning:
                warnings.append(f"{name}: {ex.warning}")
        texts = [(u["name"], u["text"]) for u in db.uploads(THREAD)]
        try:
            org, notes = extract_organisation(provider(), db.get_settings(), texts)
        except (IntakeError, ProviderError) as e:
            raise HTTPException(400, str(e))
        db.set_org("draft", org)
        return {"draft": summarise(org), "notes": notes + plan_notes, "warnings": warnings, "events": plan_events}

    @app.post("/api/uploads/clear")
    def clear_uploads(sid: str = Depends(auth.require_session)):
        for path in db.clear_uploads(THREAD):
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
        base, rec = periods.record_of(db, tid)
        if rec is not None:                           # a period instance: its record and orgs follow the name
            periods.update(db, rec["id"], base=base, name=name)
        else:
            db.rename_timetable(tid, name)
        return _timetables_payload()

    def _delete_instance(tid: str) -> None:
        """Delete a timetable, unlink its uploads and drop its locks. KeyError when unknown,
        ValueError when it is the last one or the deployment timetable while an active department
        account works on it. A period's record goes from its base first, so no period ever points at
        a gone timetable."""
        if tid == users.deployment_timetable(db) and users.any_active(db):
            raise ValueError("department accounts work on this timetable: choose another deployment timetable first")
        if len(db.timetables()) > 1:                  # the last one is refused below: touch nothing then
            periods.forget_instance(db, tid)
            periods.release_instances(db, tid)        # a deleted base's periods become ordinary timetables
        paths = db.delete_timetable(tid)
        db.forget_deployment_timetable(tid)          # the next account read fixes another (never the drifting selection)
        for path in paths:
            _unlink_quietly(path)
        with solve_locks_guard:                       # the deleted timetable's locks are never needed again
            app.state.solve_locks.pop(tid, None)
            app.state.chat_locks.pop(tid, None)       # chat locks are keyed by base: a period's tid has none, its base's stays

    @app.delete("/api/timetables/{tid}")
    def delete_timetable(tid: str, sid: str = Depends(auth.require_session)):
        try:
            _delete_instance(tid)
        except KeyError:
            raise HTTPException(404, f"no timetable {tid}")
        except ValueError as e:
            raise HTTPException(400, str(e))
        return _timetables_payload()

    app.include_router(periods_router(db, engine, _delete_instance))
    app.include_router(relief_router(db))

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
                    record["promoted"] = promote_build(db, job["result"], how="best", preset=record.get("preset"))
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
        s = db.get_settings(tid=periods.base_tid(db))      # dated: the base's term calendar
        items = bookings.list_all(db)
        return {"items": items, "describe": {b["id"]: cal_mod.describe(s["calendar"], s["time"], b["date"]) for b in items}}

    @app.delete("/api/bookings/{bid}")
    def delete_booking(bid: str, sid: str = Depends(auth.require_session)):
        def snapshot_before_removal(booking: dict) -> None:
            # runs before the removal is persisted; the entry carries the removed booking, which
            # undo puts back by id (never the whole list: the base and its periods share it).
            changes.record(db, "booking_removed", f"Removed booking: {booking['title']} on {booking['date']}", booking=booking)

        if bookings.remove(db, bid, before_persist=snapshot_before_removal) is None:
            raise HTTPException(404, "no such booking")
        return {"ok": True}

    # ---- proposals ---------------------------------------------------------
    # The browser can only apply what the assistant has already put in the pending list, by id.
    @app.post("/api/proposals/apply")
    def apply_proposal(body: dict, sid: str = Depends(auth.require_session)):
        pid = str((body or {}).get("id", ""))
        if not any(p["id"] == pid for p in proposals.pending(db, THREAD)):
            raise HTTPException(404, "nothing pending with that id")
        try:
            return proposals.apply(db, engine(), pid, THREAD)
        except EngineError as e:
            raise _engine_error(e)

    @app.post("/api/proposals/dismiss")
    def dismiss_proposal(sid: str = Depends(auth.require_session)):
        cards = proposals.pending(db, THREAD)
        proposals.clear_pending(db, THREAD)
        if cards:
            decisions.safe_record(db, "dismissed", lambda: {"cards": [{"kind": c.get("kind"), "event": c.get("event")} for c in cards]})
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
        lock = app.state.chat_locks[periods.base_tid(db)]    # a base and its periods share one lock (and one bookings list)
        if not lock.acquire(timeout=CHAT_LOCK_TIMEOUT):
            raise HTTPException(409, "Another message on this timetable is still being answered; try again in a moment.")
        try:
            res = run_chat(db, THREAD, provider(), engine(), text)
        except ProviderError as e:
            raise HTTPException(400, str(e))
        finally:
            lock.release()
        return {"text": res.text, "events": res.events}

    @app.get("/api/messages")
    def messages(sid: str = Depends(auth.require_session)):
        return db.messages(THREAD)

    @app.post("/api/messages/clear")
    def clear(sid: str = Depends(auth.require_session)):
        db.clear_messages(THREAD)
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
