"""Readings waiting for Apply, and saved readers, over HTTP (spec docs/superpowers/specs/2026-10-08-smart-
workbook-reader-design.md §4-§6; docs/superpowers/specs/2026-10-12-assistant-files-design.md §2.4).

- `store(db, file, reading, tid)`: a reading kept in per-timetable kv `readings` (`[{id, file, made_at,
  reading}]`; a new reading of the same file name replaces the old). `inbox.read_files` makes them.
- Routes (admin-only): GET /api/readings (each as `inbox.view`), POST /api/readings/{id}/retry,
  POST /api/readings/clear, POST /api/readings/{id}/apply (by section), POST /api/readings/{id}/choices
  (a waiting card's ticks and options), POST /api/readings/{id}/undo,
  POST /api/readings/{id}/skip, DELETE /api/readings/{id}, GET /api/readers, DELETE /api/readers/{signature}.

Nothing changes the plan until Apply (`applying.apply`), which merges the reading into the plan as it is
now, under the plan lock, the way PATCH /api/plan writes it (the rev moves; the board history clears)."""
from __future__ import annotations

import csv
import io
import logging
import secrets
import threading
import time

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse
from starlette.concurrency import run_in_threadpool

from ... import auth
from . import readers

KEY = "readings"
READINGS_MAX = 20           # readings kept per timetable: past it the oldest finished one goes first
OPEN = ("waiting", "retry")  # a card still to be answered: never trimmed, even past READINGS_MAX
BUSY = "The reader is busy or could not start just now; try again in a minute."
PLAN_UNREADABLE = "The plan does not read as it stands; fix it in the Tables view first."

_lock = threading.Lock()
_log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# the stored readings
# ---------------------------------------------------------------------------

def _stored(db, tid: str | None = None) -> list[dict]:
    value = db.get_value(KEY, tid)
    return [e for e in value if isinstance(e, dict) and isinstance(e.get("reading"), dict)] if isinstance(value, list) else []


def store(db, file: str, reading: dict, tid: str | None = None) -> dict:
    """Keep `reading` of `file`; one of the same file name goes. Past READINGS_MAX the oldest applied,
    skipped or failed readings go; a waiting card (or one to try again) is never dropped unseen."""
    entry = {"id": secrets.token_hex(8), "file": file, "made_at": time.time(), "reading": reading}
    with _lock:
        kept = [e for e in _stored(db, tid) if e.get("file") != file]
        kept.append(entry)
        over = len(kept) - READINGS_MAX
        if over > 0:
            # never the reading just made: a card that failed now is shown before it is trimmed
            done = [e for e in kept[:-1] if (e["reading"].get("status") or "waiting") not in OPEN][:over]
            kept = [e for e in kept if all(e is not d for d in done)]
        db.set_value(KEY, kept, tid)
    return entry


def find(db, rid: str) -> dict | None:
    return next((e for e in _stored(db) if e.get("id") == rid), None)


def remove(db, rid: str) -> bool:
    with _lock:
        stored = _stored(db)
        kept = [e for e in stored if e.get("id") != rid]
        if len(kept) == len(stored):
            return False
        db.set_value(KEY, kept)
    return True


# ---------------------------------------------------------------------------
# a CSV as a workbook
# ---------------------------------------------------------------------------

CSV_SHEET = "CSV"        # a CSV's one sheet, whatever the file is called: its layout signature and a
                         # saved reader's recipes then fit next term's file under another name


def csv_as_workbook(name: str, data: bytes) -> bytes:
    """A CSV as a one-sheet workbook for the reader, its sheet always CSV_SHEET; text cells only.
    `name`, the file's name, no longer names anything: the parameter stays for the callers."""
    import openpyxl
    try:
        text = data.decode("utf-8-sig")
    except UnicodeDecodeError:
        text = data.decode("cp1252", errors="replace")
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = CSV_SHEET
    for row in csv.reader(io.StringIO(text)):
        ws.append([c if c != "" else None for c in row])
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def upload_response(results: list[dict]):
    """An upload's answer: {files, events}; a 400 naming each file when every file failed."""
    if results and all(r["status"] == "failed" for r in results):
        detail = results[0]["message"] if len(results) == 1 else "\n".join(f"{r['name']}: {r['message']}" for r in results)
        return JSONResponse({"detail": detail, "files": results}, status_code=400)
    return {"files": results, "events": [{"kind": "readings"}]}


# ---------------------------------------------------------------------------
# routes
# ---------------------------------------------------------------------------

def make_router(db) -> APIRouter:
    r = APIRouter()

    @r.get("/api/readings")
    def list_readings(sid: str = Depends(auth.require_session)):
        from . import inbox                 # here: inbox imports this module
        return {"readings": [inbox.view(e) for e in _stored(db)]}

    @r.post("/api/readings/clear")
    def clear_readings(sid: str = Depends(auth.require_session)):
        from . import inbox
        inbox.clear(db)
        return {"ok": True}

    @r.post("/api/readings/{rid}/retry")
    async def retry_reading(rid: str, request: Request, sid: str = Depends(auth.require_session)):
        from . import inbox

        def factory():                      # made only when the file needs the model
            return request.app.state.provider_factory(db.get_settings())
        # in a worker thread: a reading may wait on the model for a while
        return await run_in_threadpool(inbox.retry, db, factory, rid)

    def _body(body) -> tuple[list[str] | None, dict | None]:
        """A request's {"sections": [names] | null, "options": {...} | null}; whatever else is ignored."""
        body = body if isinstance(body, dict) else {}
        sections = body.get("sections")
        sections = [x for x in sections if isinstance(x, str)] if isinstance(sections, list) else None
        options = body.get("options")
        return sections, options if isinstance(options, dict) else None

    def _run(call, rid: str, tid: str, **kw) -> dict:
        from . import applying              # here: applying imports this module
        try:
            out = call(db, rid, tid=tid, **kw)
            if "reading" not in out:
                out = {**out, "reading": applying.reading_view(db, rid, tid), "status": applying.plan_status(db, tid)}
            return out
        except applying.ApplyError as e:
            raise HTTPException(e.code, e.message)

    @r.post("/api/readings/{rid}/apply")
    async def apply_reading(rid: str, request: Request, sid: str = Depends(auth.require_session)):
        from . import applying
        try:
            sections, options = _body(await request.json())
        except ValueError:
            sections, options = None, None
        tid = db.working_timetable()        # pinned: the selection may change while this waits for the lock
        # in a worker thread: it waits on the plan lock, which must not stall the event loop
        return await run_in_threadpool(lambda: _run(applying.apply, rid, tid, ticked=sections, options=options))

    @r.post("/api/readings/{rid}/choices")
    async def reading_choices(rid: str, request: Request, sid: str = Depends(auth.require_session)):
        """A waiting card's ticks ({"sections": {name: bool}}) and timetable options, kept on the reading."""
        from . import applying
        try:
            body = await request.json()
        except ValueError:
            body = None
        body = body if isinstance(body, dict) else {}
        sections = body.get("sections") if isinstance(body.get("sections"), dict) else {}
        options = body.get("options") if isinstance(body.get("options"), dict) else None
        tid = db.working_timetable()
        return await run_in_threadpool(lambda: _run(applying.choose, rid, tid, sections=sections, options=options))

    @r.post("/api/readings/{rid}/undo")
    async def undo_reading(rid: str, sid: str = Depends(auth.require_session)):
        from . import applying
        tid = db.working_timetable()
        return await run_in_threadpool(lambda: _run(applying.undo, rid, tid))

    @r.post("/api/readings/{rid}/skip")
    async def skip_reading(rid: str, sid: str = Depends(auth.require_session)):
        from . import applying
        tid = db.working_timetable()
        return await run_in_threadpool(lambda: _run(applying.skip, rid, tid))

    @r.delete("/api/readings/{rid}")
    def discard_reading(rid: str, sid: str = Depends(auth.require_session)):
        if not remove(db, rid):
            raise HTTPException(404, "no such reading")
        return {"ok": True}

    @r.get("/api/readers")
    def list_readers(sid: str = Depends(auth.require_session)):
        return {"readers": [{"signature": e.get("signature"), "name": e.get("name") or "Saved reader",
                             "uses": e.get("uses") or 0, "made_at": e.get("made_at")} for e in readers.list(db)]}

    @r.delete("/api/readers/{signature}")
    def delete_reader(signature: str, sid: str = Depends(auth.require_session)):
        try:
            readers.delete(db, signature)
        except KeyError:
            raise HTTPException(404, "no such saved reader")
        return {"ok": True}

    return r
