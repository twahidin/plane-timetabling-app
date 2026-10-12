"""Every file a reading (spec docs/superpowers/specs/2026-10-12-assistant-files-design.md §2.1, §2.4).

`read_files` routes each file: an aSc export (all of a drop's, together) → one timetable reading; a
planning workbook or a class-sizes CSV → an exact plan reading (merged by the importer, nothing
stored; a drop's sizes CSVs go into the reading of its first planning workbook, applied on top); any other .xlsx/.csv → the smart reader; .docx and other PDFs → the document reader; anything
else → a failed card that says why. A file the model could not read now (or read as nothing) is kept
on disk for Try again. Nothing here writes the plan or the draft."""
from __future__ import annotations

import csv
import io
import logging
import secrets
import time
from pathlib import Path

from ... import asc_import
from ...config import MAX_UPLOAD
from ...extract import UnsupportedFile, extract
from ...intake import IntakeError, apply_patch as apply_org_patch
from ...llm import ProviderError
from .. import importer as IMP
from .. import model as M
from . import document, routes as RR, sections as S, writer
from . import recipes as R
from .render import WorkbookError

FILES_MAX = 10
TYPES = (".pdf", ".docx", ".xlsx", ".csv")
SWEEP_GRACE = 600            # seconds a kept file is left alone by the sweep: a drop being read may not have stored it yet
OPTIONS = {"mode": "keep", "classes_as_planes": False}
WAITING = ("waiting", "failed", "retry")      # what clear drops; applied and skipped readings stay
_log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# kept files
# ---------------------------------------------------------------------------

def files_dir(db) -> Path:
    """Where readings keep their files: beside the chat's uploads, under the app's data directory."""
    d = Path(db.path).parent / "uploads" / "readings"
    d.mkdir(parents=True, exist_ok=True)
    return d


def keep_file(db, name: str, data: bytes) -> str:
    ext = Path(name).suffix.lower()
    path = files_dir(db) / f"{secrets.token_hex(12)}{ext if ext in TYPES else '.bin'}"
    path.write_bytes(data)
    return str(path)


def _paths(reading: dict) -> list[str]:
    return [p for p in [reading.get("path"), *(reading.get("paths") or []), *(reading.get("sizes_paths") or [])]
            if isinstance(p, str) and p]


def _unlink(path: str) -> None:
    try:
        Path(path).unlink(missing_ok=True)
    except OSError:
        pass


def _named(db) -> set[str]:
    """Every file a reading of any timetable names."""
    out: set[str] = set()
    for t in db.timetables():
        for e in RR._stored(db, t["id"]):
            out.update(_paths(e["reading"]))
    return out


def sweep(db) -> None:
    """Unlink kept files no reading names (a deleted timetable's, a replaced or trimmed reading's)."""
    named = {str(Path(p)) for p in _named(db)}
    cutoff = time.time() - SWEEP_GRACE
    for f in files_dir(db).iterdir():
        try:
            if f.is_file() and str(f) not in named and f.stat().st_mtime < cutoff:
                f.unlink()
        except OSError:
            pass


# ---------------------------------------------------------------------------
# storing a reading
# ---------------------------------------------------------------------------

def _entry(db, name: str, reading: dict, tid: str | None) -> dict:
    return RR.store(db, name, reading, tid)


def _failed(db, name: str, message: str, tid: str | None, path: str | None = None, status: str = "failed") -> dict:
    entry = _entry(db, name, {"kind": "plan", "via": "ai", "status": status, "sections": {}, "doubts": [],
                              "summary": name, "error": message, "path": path}, tid)
    return {"name": name, "status": status, "id": entry["id"], "message": message}


def _current(db, tid: str | None) -> dict:
    return M.normalise(db.get_value("plan", tid) or M.empty_plan())


def _plan_reading(db, name: str, body: dict, via: str, tid: str | None, data: bytes | None = None) -> dict:
    """A plan reading stored to wait for Apply. One read by the AI (`data`, its file) that brings nothing
    is kept to try again: an empty reply is the model's, not the file's."""
    current = _current(db, tid)
    try:
        secs = S.sections_of(current, S.merged(current, {**body, "file": name}))
    except M.PlanError as e:
        return _failed(db, name, f"What was read from {name} does not fit the plan: {e}", tid)
    if not secs and via == "ai" and data is not None:
        return _retry(db, name, data, f"Nothing usable came back when the AI read {name}: try again, "
                                      "or check that it adds something to the plan.", tid)
    if not secs:
        why = next(iter(body.get("doubts") or []), "")
        return _failed(db, name, f"Nothing in {name} adds to the plan{': ' + why if why else '.'}", tid)
    stored = {k: v for k, v in body.items() if k != "merged"}
    entry = _entry(db, name, {**stored, "kind": "plan", "via": via, "status": "waiting", "sections": secs}, tid)
    summary = body.get("summary") or name
    return {"name": name, "status": "read", "id": entry["id"],
            "message": f"{summary}: " + ", ".join(f"{s['count']} {section}" for section, s in secs.items())}


def _timetable_reading(db, name: str, body: dict, ticked: bool, tid: str | None) -> dict:
    org = body.get("organisation") or body.pop("_org")
    secs = {"timetable": {"count": len(org.get("events") or []), "teachers": len(org.get("persons") or []),
                          "samples": [], "ticked": ticked}}
    reading = {k: v for k, v in body.items() if k != "_org"}
    entry = _entry(db, name, {**reading, "kind": "timetable", "via": "ai" if body.get("organisation") else "exact",
                              "status": "waiting", "sections": secs, "options": dict(OPTIONS)}, tid)
    t = secs["timetable"]
    return {"name": name, "status": "read", "id": entry["id"],
            "message": f"{body.get('summary') or name}: {t['count']} lessons, {t['teachers']} teachers"}


# ---------------------------------------------------------------------------
# the readers
# ---------------------------------------------------------------------------

def sizes_text(name: str, data: bytes) -> str | None:
    """A class-sizes CSV's text (a Class or Teaching Group column with Enrolled, or a Teaching Group column
    with Capacity, and a size in some row); None for any other file. A Class column with Capacity alone
    is a room's capacity (a home-room list), not a class size."""
    if not name.lower().endswith(".csv"):
        return None
    try:
        text = data.decode("utf-8-sig")
    except UnicodeDecodeError:
        text = data.decode("cp1252", errors="replace")
    header = {c.strip().lower() for c in next(csv.reader(io.StringIO(text)), [])}
    if not ("enrolled" in header and header & {"class", "teaching group"}
            or {"capacity", "teaching group"} <= header):
        return None
    return text if IMP.read_sizes_csv(text) else None


def is_planning(name: str, data: bytes) -> bool:
    """A deployment or duties workbook: read exactly by the importer."""
    return name.lower().endswith(".xlsx") and (IMP.is_deployment_workbook(data) or IMP.is_generic_workbook(data))


def _with_sizes(plan: dict, texts: list[str]) -> tuple[dict, list[str]]:
    """`plan` with the sizes of `texts` (sizes CSVs) on top, and a doubt naming sizes that fit nothing."""
    sizes: dict[str, int] = {}
    for text in texts:
        sizes.update(IMP.read_sizes_csv(text))
    if not sizes:
        return plan, []
    known = {c["code"] for c in plan["classes"]} | {r.get("grouping") for r in plan["requirements"]}
    unknown = [k for k in sizes if k not in known]
    doubts = [f"{len(unknown)} size{'' if len(unknown) == 1 else 's'} name no class or group in the plan: "
              + ", ".join(unknown[:8])] if unknown else []
    return IMP.apply_sizes(plan, sizes), doubts


def exact_merged(db, name: str, data: bytes, current: dict, tid: str | None,
                 sizes: list[tuple[str, bytes]] = ()) -> tuple[dict, list[str]]:
    """A planning workbook (or a sizes CSV) merged into `current` by the importer: (plan, its issue texts).
    `sizes`, the (name, bytes) of sizes CSVs given with a workbook, are applied on top of its merge."""
    text = sizes_text(name, data)
    if text is not None:
        return _with_sizes(current, [text])
    live = db.get_org("live", tid)
    if IMP.is_generic_workbook(data):
        merged, issues = IMP.read_generic_workbook(data, live, current, name, db.get_settings(tid))
    else:
        merged, issues = IMP.read_workbook(data, live, current, name)
    texts = [t for t in (sizes_text(n, d) for n, d in sizes) if t is not None]
    merged, size_doubts = _with_sizes(merged, texts)
    return merged, list(dict.fromkeys([*(i.text for i in issues), *size_doubts]))[:document.DOUBTS_MAX]


def _exact(db, name: str, data: bytes, tid: str | None, summary: str = "Planning workbook",
           sizes: list[tuple[str, bytes]] = ()) -> dict:
    merged, doubts = exact_merged(db, name, data, _current(db, tid), tid, sizes)
    body = {"merged": merged, "path": keep_file(db, name, data), "summary": summary, "doubts": doubts}
    if sizes:                               # kept beside the workbook: merged again with it at apply
        body["sizes_paths"] = [keep_file(db, n, d) for n, d in sizes]
    out = _plan_reading(db, name, body, "exact", tid)
    if out["status"] != "read":
        for p in _paths(body):
            _unlink(p)
    return out


def _retry(db, name: str, data: bytes, message: str, tid: str | None) -> dict:
    return _failed(db, name, message, tid, path=keep_file(db, name, data), status="retry")


def _no_model(name: str) -> str:
    return f"The AI could not read {name} just now: check the model in Settings or try again."


def _smart(db, provider_factory, name: str, data: bytes, tid: str | None) -> dict:
    book = RR.csv_as_workbook(name, data) if name.lower().endswith(".csv") else data
    try:
        reading = writer.read(provider_factory, book, db.get_value("plan", tid), db=db)
    except R.ReadingError:                  # before RecipeError, its base: the run, not the recipes
        return _retry(db, name, data, RR.BUSY, tid)
    except writer.NoProvider as e:          # our own words: shown as they are
        return _retry(db, name, data, str(e), tid)
    except ProviderError as e:              # the provider's own words may be raw: logged, not shown
        _log.warning("reading %s: the provider failed: %s", name, e)
        return _retry(db, name, data, _no_model(name), tid)
    except (R.RecipeError, WorkbookError) as e:
        return _failed(db, name, f"{name} could not be read: {e}", tid)
    body = {**reading, "summary": reading.get("summary") or name}
    return _plan_reading(db, name, body, "saved" if reading.get("reader") else "ai", tid, data)


def _document(db, provider_factory, i: int, name: str, data: bytes, tid: str | None, timetables: list) -> dict | None:
    """A Word or PDF document: a plan reading stored now, or a timetable body put on `timetables` (stored
    once the drop's plan material is known; None returned then)."""
    try:
        ex = extract(name, data)
    except UnsupportedFile as e:
        return _failed(db, name, str(e), tid)
    if ex.kind == "pdf" and ex.warning:     # a scan: no text to read
        return _failed(db, name, ex.warning, tid)
    try:
        body = document.read_document(provider_factory(), name, ex.text, db.get_value("plan", tid), db.get_settings(tid))
    except ProviderError as e:
        _log.warning("reading %s: the provider failed: %s", name, e)
        return _retry(db, name, data, str(e) if isinstance(e, writer.NoProvider) else _no_model(name), tid)
    except document.DocumentError as e:
        return _failed(db, name, str(e), tid)
    if body.get("kind") == "timetable":
        timetables.append(([i], {"summary": body.get("summary") or name, "doubts": body.get("doubts") or [],
                                  "organisation": body["organisation"], "paths": [keep_file(db, name, data)]}))
        return None
    return _plan_reading(db, name, body, "ai", tid, data)


def asc_org(datas: list[bytes], mode: str = "keep", classes_as_planes: bool = False) -> tuple[dict, list[dict]]:
    """The draft an aSc export's PDFs make, validated like every other draft."""
    pages: list = []
    for d in datas:
        pages.extend(asc_import.read_pdf_words(d))
    org, notes = asc_import.build_organisation(pages, mode=mode, classes_as_planes=classes_as_planes)
    return apply_org_patch(org, {}), notes


def _asc(db, asc: list[tuple[int, str, bytes]], tid: str | None, results: dict, timetables: list) -> None:
    try:
        org, notes = asc_org([d for _, _, d in asc], **OPTIONS)
    except asc_import.AscFormatError as e:
        for i, name, _ in asc:
            results[i] = _failed(db, name, str(e), tid)
        return
    except IntakeError as e:
        for i, name, _ in asc:
            results[i] = _failed(db, name, f"The timetable export could not be turned into a draft: {e}", tid)
        return
    doubts = [str(n.get("note")) for n in notes if isinstance(n, dict) and n.get("note")][:document.DOUBTS_MAX]
    timetables.append(([i for i, _, _ in asc], {
        "summary": "Timetable export", "doubts": doubts, "_org": org,
        "paths": [keep_file(db, name, d) for _, name, d in asc]}))


# ---------------------------------------------------------------------------
# a drop of files
# ---------------------------------------------------------------------------

def _one(db, provider_factory, i: int, name: str, data: bytes, tid: str | None, timetables: list,
         sizes: list[tuple[str, bytes]] = ()) -> dict | None:
    lower = name.lower()
    if is_planning(name, data):
        return _exact(db, name, data, tid, sizes=sizes)
    if sizes_text(name, data) is not None:  # class sizes: read exactly, as the plan upload once took them
        return _exact(db, name, data, tid, "Class sizes")
    if lower.endswith((".xlsx", ".csv")):
        return _smart(db, provider_factory, name, data, tid)
    if lower.endswith((".docx", ".pdf")):
        return _document(db, provider_factory, i, name, data, tid, timetables)
    if lower.endswith((".doc", ".xls")):
        try:
            extract(name, data)
        except UnsupportedFile as e:
            return _failed(db, name, str(e), tid)
    return _failed(db, name, f"{name} is not a file the Assistant reads: give it PDF, Word (.docx), Excel (.xlsx) or CSV.", tid)


def _folding(items: list[tuple[str, bytes]]) -> tuple[int | None, list[int]]:
    """(the position of the drop's first planning workbook, the positions of its sizes CSVs), or (None, [])
    when the drop holds no planning workbook: the sizes then go on their own cards."""
    def ok(i: int, name: str, data: bytes, test) -> bool:
        if i >= FILES_MAX or len(data) > MAX_UPLOAD:
            return False
        try:
            return bool(test(name or "upload", data))
        except Exception:                   # not readable here: the file's own card says why
            return False
    book = next((i for i, (n, d) in enumerate(items) if ok(i, n, d, is_planning)), None)
    if book is None:
        return None, []
    return book, [i for i, (n, d) in enumerate(items) if ok(i, n, d, lambda n, d: sizes_text(n, d) is not None)]


def read_files(db, provider_factory, items: list[tuple[str, bytes]], tid: str | None = None) -> list[dict]:
    """Blocking. Each file of `items` ((name, bytes), in order) as a stored reading of timetable `tid`
    (the working one when None); one result per file, {name, status: read | failed | retry, id, message}.
    Never raises: whatever goes wrong is that file's failed card, so one file never stops the others.
    A file past FILES_MAX gets a card only: no stored reading (it would push the drop's own readings out).
    A drop's sizes CSVs go into the reading of its first planning workbook (their sizes applied on top of
    its merge, as the plan upload once took them); each says so, with that reading's id."""
    tid = tid or db.working_timetable()     # pinned: the selection may change while the model reads
    try:
        sweep(db)
    except Exception:
        _log.exception("sweeping kept files failed")
    book, folded = _folding(items)
    results: dict[int, dict] = {}
    asc: list[tuple[int, str, bytes]] = []
    timetables: list = []                   # ([positions], body) of timetable readings, stored last
    for i, (name, data) in enumerate(items):
        name = name or "upload"
        try:
            if i >= FILES_MAX:
                results[i] = {"name": name, "status": "failed",
                              "message": f"Up to {FILES_MAX} files at a time: give {name} again on its own."}
            elif len(data) > MAX_UPLOAD:
                results[i] = _failed(db, name, f"{name} is over 20 MB", tid)
            elif i in folded:
                continue                    # read with the workbook, after it
            elif name.lower().endswith(".pdf") and asc_import.is_asc_pdf(data):
                asc.append((i, name, data))
            else:
                sizes = [(items[j][0] or "upload", items[j][1]) for j in folded] if i == book else []
                out = _one(db, provider_factory, i, name, data, tid, timetables, sizes)
                if out is not None:
                    results[i] = out
        except M.PlanError as e:            # the stored plan itself does not read
            results[i] = _failed(db, name, f"{RR.PLAN_UNREADABLE} ({e})", tid)
        except Exception:
            _log.exception("reading %s failed", name)
            results[i] = _failed(db, name, f"Could not read {name}; try again.", tid)
    for j in folded:
        name, data = items[j][0] or "upload", items[j][1]
        wb = results.get(book) or {}
        if wb.get("status") == "read":
            results[j] = {"name": name, "status": "read", "id": wb["id"],
                          "message": f"The sizes in {name} are in the {wb['name']} card."}
            continue
        try:                                # the workbook did not read: the sizes on a card of their own
            results[j] = _one(db, provider_factory, j, name, data, tid, timetables)
        except M.PlanError as e:
            results[j] = _failed(db, name, f"{RR.PLAN_UNREADABLE} ({e})", tid)
        except Exception:
            _log.exception("reading %s failed", name)
            results[j] = _failed(db, name, f"Could not read {name}; try again.", tid)
    if asc:
        try:
            _asc(db, asc, tid, results, timetables)
        except Exception:
            _log.exception("reading the timetable export failed")
            for i, name, _ in asc:
                results[i] = _failed(db, name, "Could not read the timetable export; try again.", tid)
    plan_material = any(r["status"] == "read" for r in results.values())
    for positions, body in timetables:
        names = [items[i][0] or "upload" for i in positions]
        try:
            out = _timetable_reading(db, ", ".join(names), body, not plan_material, tid)
        except Exception:
            _log.exception("storing the timetable reading failed")
            out = None
        for i, name in zip(positions, names):
            results[i] = ({**out, "name": name} if out is not None
                          else _failed(db, name, f"Could not read {name}; try again.", tid))
    return [results[i] for i in sorted(results)]


def retry(db, provider_factory, rid: str, tid: str | None = None) -> dict:
    """Blocking. The kept file of reading `rid`, waiting to be tried again, read once more."""
    tid = tid or db.working_timetable()
    entry = next((e for e in RR._stored(db, tid) if e.get("id") == rid), None)
    reading = (entry or {}).get("reading") or {}
    path = reading.get("path")
    if entry is None or reading.get("status") != "retry" or not path:
        return {"status": "failed", "message": "Nothing to try again."}
    try:
        data = Path(path).read_bytes()
    except OSError:
        return {"name": entry.get("file") or "", "status": "failed", "message": "The file is gone: give it again."}
    out = read_files(db, provider_factory, [(entry.get("file") or "upload", data)], tid)[0]
    if path not in _named(db):
        _unlink(path)
    return out


# ---------------------------------------------------------------------------
# what the page sees, and clearing
# ---------------------------------------------------------------------------

_OLD_SECTION = {"venues": "rooms", "homes": "classes"}


def _old_sections(r: dict) -> dict:
    samples = r.get("samples") or {}
    out: dict[str, dict] = {}
    for target, n in (r.get("counts") or {}).items():
        name = _OLD_SECTION.get(target, target)
        s = out.setdefault(name, {"count": 0, "added": 0, "changed": 0, "samples": [], "ticked": True})
        s["count"] += n
        s["added"] += n
        s["samples"] += [str(x.get("name") or x.get("code") or x.get("subject") or "")
                         for x in samples.get(target, []) if isinstance(x, dict)]
    return out


def view(entry: dict) -> dict:
    r = entry.get("reading") or {}
    old = "kind" not in r                   # stored before readings had kinds: a smart-reader plan reading
    return {"id": entry.get("id"), "file": entry.get("file") or "", "made_at": entry.get("made_at"),
            "kind": r.get("kind") or "plan",
            "via": ("saved" if r.get("reader") else "ai") if old else r.get("via") or "ai",
            "status": r.get("status") or "waiting",
            "summary": r.get("summary") or "",
            "sections": _old_sections(r) if old else r.get("sections") or {},
            "doubts": r.get("doubts") or [], "error": r.get("error") or "",
            "options": r.get("options"), "undo": bool(r.get("undo")),
            "applied": r.get("applied")}      # {section: count} once applied: what the card says was applied


def clear(db, tid: str | None = None) -> None:
    """Drop the readings still waiting (or failed, or to try again) and their kept files."""
    tid = tid or db.working_timetable()
    gone: list[str] = []
    with RR._lock:
        kept = []
        for e in RR._stored(db, tid):
            if view(e)["status"] in WAITING:
                gone += _paths(e["reading"])
            else:
                kept.append(e)
        db.set_value(RR.KEY, kept, tid)
    for p in gone:
        _unlink(p)
