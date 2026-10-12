"""Readings waiting for Apply, and saved readers, over HTTP (spec docs/superpowers/specs/2026-10-08-smart-
workbook-reader-design.md §4-§6).

- `read_upload(db, provider, name, data, tid)`: one uploaded workbook (.xlsx, or a .csv made into one)
  read by writer.read and kept as a reading in per-timetable kv `readings` (`[{id, file, made_at,
  reading}]`; a new reading of the same file name replaces the old). Blocking: the upload runs it in
  the threadpool. Returns the file's result for the upload's `files` list.
- Routes (admin-only): GET /api/readings, POST /api/readings/{id}/apply, DELETE /api/readings/{id},
  GET /api/readers, DELETE /api/readers/{signature}.

Nothing changes the plan until Apply, which re-runs the reading's patch against the plan as it is now,
under the plan lock, the way PATCH /api/plan writes it (the rev moves; the board history clears)."""
from __future__ import annotations

import csv
import io
import logging
import secrets
import threading
import time

from fastapi import APIRouter, Depends, HTTPException
from starlette.concurrency import run_in_threadpool

from ... import auth, journey
from ...llm import ProviderError
from .. import model as M
from . import readers, writer
from . import recipes as R
from .render import WorkbookError

KEY = "readings"
READINGS_MAX = 20           # readings kept per timetable; the oldest goes first
BUSY = "The reader is busy or could not start just now; try again in a minute."
PLAN_UNREADABLE = "The plan does not read as it stands; fix it in the Tables view first."
WORDS = {"venues": ("room", "rooms"), "homes": ("home room", "home rooms"), "classes": ("class", "classes"),
         "staff": ("staff member", "staff"), "requirements": ("requirement", "requirements")}

_lock = threading.Lock()
_log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# the stored readings
# ---------------------------------------------------------------------------

def _stored(db, tid: str | None = None) -> list[dict]:
    value = db.get_value(KEY, tid)
    return [e for e in value if isinstance(e, dict) and isinstance(e.get("reading"), dict)] if isinstance(value, list) else []


def store(db, file: str, reading: dict, tid: str | None = None) -> dict:
    """Keep `reading` of `file`; one of the same file name goes. The newest READINGS_MAX are kept."""
    entry = {"id": secrets.token_hex(8), "file": file, "made_at": time.time(), "reading": reading}
    with _lock:
        kept = [e for e in _stored(db, tid) if e.get("file") != file]
        kept.append(entry)
        db.set_value(KEY, kept[-READINGS_MAX:], tid)
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


def public(db, rid: str) -> dict | None:
    """The stored reading `rid` as the page sees it, or None."""
    entry = find(db, rid)
    return _public(entry) if entry is not None else None


def _public(entry: dict) -> dict:
    """What the page sees: the reading without its patch, recipes and signature (kept for Apply)."""
    r = entry["reading"]
    return {"id": entry["id"], "file": entry.get("file") or "", "made_at": entry.get("made_at"),
            "summary": r.get("summary") or "", "counts": r.get("counts") or {}, "samples": r.get("samples") or {},
            "doubts": r.get("doubts") or [], "reader": r.get("reader")}


# ---------------------------------------------------------------------------
# reading an upload
# ---------------------------------------------------------------------------

def counts_line(counts: dict) -> str:
    parts = []
    for target, n in counts.items():
        one, many = WORDS.get(target, (target, target))
        parts.append(f"{n} {one if n == 1 else many}")
    return ", ".join(parts)


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


def read_upload(db, provider, name: str, data: bytes, tid: str | None = None) -> dict:
    """Blocking. {name, status: "reading" | "error", message} (+ `id` of the stored reading; + `code`,
    the HTTP status an error stands for, which the caller pops): `data` read into a stored reading of
    timetable `tid` (the working one when None). Never raises: whatever goes wrong is this file's
    error, so one file never fails the rest of an upload."""
    def error(message: str, code: int = 400) -> dict:
        return {"name": name, "status": "error", "message": message, "code": code}
    try:
        if name.lower().endswith(".csv"):
            data = csv_as_workbook(name, data)
        reading = writer.read(provider, data, db.get_value("plan", tid), db=db)
        if not reading.get("counts"):
            why = next(iter(reading.get("doubts") or []), "")
            return error(f"Nothing in {name} could be read{': ' + why if why else '.'}")
        entry = store(db, name, reading, tid)
    except R.ReadingError:                  # before RecipeError, its base: the run, not the recipes
        return error(BUSY)
    except writer.NoProvider as e:          # our own words: shown as they are
        return error(str(e))
    except ProviderError as e:              # the provider's own words may be raw: logged, not shown
        _log.warning("reading %s: the provider failed: %s", name, e)
        return error(f"The AI could not read {name} just now: check the model in Settings or try again.")
    except (R.RecipeError, WorkbookError) as e:
        return error(f"{name} could not be read: {e}")
    except M.PlanError:                     # the stored plan itself does not read
        return error(PLAN_UNREADABLE, 422)
    except Exception:
        _log.exception("reading %s failed", name)
        return error(f"Could not read {name}; try again or use the template.")
    head = (f"Read with your saved reader '{reading['reader']}'" if reading.get("reader")
            else f"Read as: {reading.get('summary') or 'a workbook'}")
    return {"name": name, "status": "reading", "id": entry["id"],
            "message": f"{head} ({counts_line(reading['counts'])}). Check it on the Requirements tab, then Apply."}


# ---------------------------------------------------------------------------
# routes
# ---------------------------------------------------------------------------

def make_router(db) -> APIRouter:
    r = APIRouter()

    @r.get("/api/readings")
    def list_readings(sid: str = Depends(auth.require_session)):
        return {"readings": [_public(e) for e in _stored(db)]}

    def _apply(rid: str) -> dict:
        with db.plan_lock():
            entry = find(db, rid)
            if entry is None:
                raise HTTPException(404, "no such reading: it was applied, discarded or replaced")
            reading = entry["reading"]
            patch = reading.get("patch") or {}
            if not patch:
                raise HTTPException(409, "nothing in this reading fits the plan: see its doubts")
            plan = db.get_value("plan") or M.empty_plan()
            try:
                new = M.apply_patch(plan, patch)            # the plan as it is now: it may have changed
            except M.PlanError as e:
                raise HTTPException(409, f"the plan has changed and this reading no longer fits it: {e}")
            db.set_value("plan", new)
            journey.stamp(db, "files")
            rev = int((db.get_value("plan") or {}).get("rev") or 0)
            remove(db, rid)
        out = {"rev": rev, "counts": reading.get("counts") or {}, "reader": None}
        recipes = reading.get("recipes") or []
        reader_sig = reading.get("reader_signature")
        if reader_sig:
            # read by a saved reader: count the use and remember this layout; never save it again (the
            # reading's recipes may be fewer than the reader's, and its made_at stays)
            kept = readers.used(db, reader_sig)
            if kept is not None:
                sig = reading.get("signature")
                if sig and sig != reader_sig:
                    readers.add_alias(db, reader_sig, sig)
                out["reader"] = kept.get("name") or "Saved reader"
        elif recipes and reading.get("signature"):
            name = reading.get("summary") or entry.get("file") or "Saved reader"
            try:
                saved = readers.save(db, reading["signature"], name, recipes)
                readers.used(db, saved["signature"])
                out["reader"] = saved["name"]
            except R.RecipeError as e:                      # the plan is applied; the reader is not kept
                out["note"] = f"The reader was not saved: {e}"
        return out

    @r.post("/api/readings/{rid}/apply")
    async def apply_reading(rid: str, sid: str = Depends(auth.require_session)):
        # in a worker thread: it waits on the plan lock, which must not stall the event loop
        return await run_in_threadpool(_apply, rid)

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
