"""Every file a reading (spec docs/superpowers/specs/2026-10-12-assistant-files-design.md §2.1, §2.4).

`read_files` routes each file: an aSc export (all of a drop's, together) → one timetable reading; a
planning workbook or a class-sizes CSV → an exact plan reading (merged by the importer, nothing
stored; a drop's sizes CSVs go into the reading of its first planning workbook, applied on top); a
student list → a students reading, read exactly by students.read_rows (one that cannot be read
exactly is a failed card: a student list never reaches the AI); any other .xlsx/.csv → the smart
reader; .docx and other PDFs → the document reader; anything else → a failed card that says why. A file the model could not read now (or read as nothing) is kept
on disk for Try again. Nothing here writes the plan or the draft."""
from __future__ import annotations

import csv
import io
import logging
import re
import secrets
import time
from pathlib import Path

from ... import asc_import, students as ST
from ...config import MAX_UPLOAD
from ...extract import UnsupportedFile, extract
from ...intake import IntakeError, apply_patch as apply_org_patch
from ...llm import ProviderError
from ... import week as W
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
    try:
        header = {c.strip().lower() for c in next(csv.reader(io.StringIO(text)), [])}
    except csv.Error:                       # NULs or a stray newline: not a sizes CSV
        return None
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


NOT_KEPT = "the file wasn't kept because its Students sheet couldn't be removed"


def _kept_workbook(name: str, data: bytes) -> bytes | None:
    return without_students_sheet(data) if name.lower().endswith(".xlsx") else data


def _open_for_copy(data: bytes):
    import openpyxl
    return openpyxl.load_workbook(io.BytesIO(data), data_only=True)    # values: a saved formula would lose its cached result


def _names_students_sheet(data: bytes) -> bool:
    """Whether the workbook's own list of sheets (xl/workbook.xml) names a Students sheet: asked without
    openpyxl, for a workbook it could not open."""
    import html
    import zipfile
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as z:
            book = z.read("xl/workbook.xml").decode("utf-8", "replace")
    except Exception:
        return False
    return any(html.unescape(n).strip().casefold() == STUDENTS_SHEET
               for n in re.findall(r"<(?:\w+:)?sheet\b[^>]*?\bname=\"([^\"]*)\"", book))


def without_students_sheet(data: bytes) -> bytes | None:
    """`data` (a planning workbook) without any sheet named Students: the copy that is kept for Apply.
    The students are read once, at the drop, and live only in the timetable's `students` value (and a
    reading until it is applied or skipped); the plan reading never needs them, so the roster is not
    left on disk in a file nothing removes. A workbook with no such sheet is kept as it came. Fails
    closed: None (keep no copy) when the workbook has a Students sheet that could not be taken out."""
    try:
        wb = _open_for_copy(data)
    except Exception:
        return None if _names_students_sheet(data) else data
    try:
        extra = [ws for ws in wb.worksheets if str(ws.title).strip().casefold() == STUDENTS_SHEET]
        if not extra:
            return data
        for ws in extra:
            wb.remove(ws)
        buf = io.BytesIO()
        wb.save(buf)
        return buf.getvalue()
    except Exception:
        return None
    finally:
        wb.close()


def _exact(db, name: str, data: bytes, tid: str | None, summary: str = "Planning workbook",
           sizes: list[tuple[str, bytes]] = (), alongside: list | None = None) -> dict:
    """A planning workbook (or sizes CSV) as an exact plan reading. When `alongside` is a list and the
    workbook's Students sheet holds students, their reading (read exactly, against the plan this workbook
    brings) is appended to it: the one file then makes two readings, a plan and a student list."""
    merged, doubts = exact_merged(db, name, data, _current(db, tid), tid, sizes)
    kept = _kept_workbook(name, data)
    if kept is None:                        # never a copy still holding the students: no copy at all
        _log.warning("%s: the Students sheet could not be removed, the file was not kept", name)
        doubts = [*doubts, NOT_KEPT]
    body = {"merged": merged, "path": keep_file(db, name, kept) if kept is not None else None,
            "summary": summary, "doubts": doubts}
    if sizes:                               # kept beside the workbook: merged again with it at apply
        body["sizes_paths"] = [keep_file(db, n, d) for n, d in sizes]
    out = _plan_reading(db, name, body, "exact", tid)
    if out["status"] != "read":
        for p in _paths(body):
            _unlink(p)
    if alongside is not None and name.lower().endswith(".xlsx"):
        students = _sheet_students_reading(db, name, data, tid, merged)
        if students is not None:
            alongside.append(students)
    return out


def _sheet_students_reading(db, name: str, data: bytes, tid: str | None, plan: dict) -> dict | None:
    """The reading of a planning workbook's Students sheet (None when it holds no student), kept under
    its own file name: one of the same name replaces the plan reading's. The students are never in the
    plan reading's file or in anything the AI is shown."""
    try:
        if sheet_students(name, data) is None:
            return None
    except Exception as e:
        _log.warning("checking %s for a Students sheet failed (%s)", name, type(e).__name__)
        return None
    return _students_branch(db, f"{name} (Students sheet)", data, tid, plan, table=lambda n, d: sheet_students(name, d))


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
    if document_students(ex):               # before the provider is made: a student list never reaches the AI
        return _students_failed(db, name, "a Word or PDF student list can't be read exactly; give it as Excel or CSV", tid)
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


def asc_read(datas: list[bytes], mode: str = "keep", classes_as_planes: bool = False) -> tuple[dict, list[dict], dict | None]:
    """The draft an aSc export's PDFs make, validated like every other draft, and the week the export's grid
    proposes (None when it makes no week the app can keep). The week never stays on the draft."""
    pages: list = []
    for d in datas:
        pages.extend(asc_import.read_pdf_words(d))
    org, notes = asc_import.build_organisation(pages, mode=mode, classes_as_planes=classes_as_planes)
    week = org.pop("_week", None)
    return apply_org_patch(org, {}), notes, week


def asc_org(datas: list[bytes], mode: str = "keep", classes_as_planes: bool = False) -> tuple[dict, list[dict]]:
    """The draft an aSc export's PDFs make, validated like every other draft."""
    org, notes, _week = asc_read(datas, mode, classes_as_planes)
    return org, notes


def _same_week(a: dict, b: dict) -> bool:
    return (a["n_days"], a["spd"], a["open_per_day"]) == (b["n_days"], b["spd"], b["open_per_day"])


def _asc(db, asc: list[tuple[int, str, bytes]], tid: str | None, results: dict, timetables: list) -> None:
    try:
        org, notes, proposed = asc_read([d for _, _, d in asc], **OPTIONS)
    except asc_import.AscFormatError as e:
        for i, name, _ in asc:
            results[i] = _failed(db, name, str(e), tid)
        return
    except IntakeError as e:
        for i, name, _ in asc:
            results[i] = _failed(db, name, f"The timetable export could not be turned into a draft: {e}", tid)
        return
    doubts = [str(n.get("note")) for n in notes if isinstance(n, dict) and n.get("note")][:document.DOUBTS_MAX]
    body = {"summary": "Timetable export", "doubts": doubts, "_org": org,
            "paths": [keep_file(db, name, d) for _, name, d in asc]}
    if proposed is not None:                    # kept whole: whether it differs is asked afresh each time the card is shown
        body["week"] = proposed
    timetables.append(([i for i, _, _ in asc], body))


# ---------------------------------------------------------------------------
# student lists (spec docs/superpowers/specs/2026-10-16-students-design.md §2 Readers, §4): read
# exactly by students.read_rows, never by the AI. Every text the reading carries (summary, doubts,
# error, the upload's message) is made of counts, row numbers, IDs, class and option codes and the
# file's header words: never a name, never a raw cell (a problem's `detail` stays off the reading).
# ---------------------------------------------------------------------------

STUDENTS_SHEET = "students"
NOT_AI = "Student lists are never sent to the AI reader."
RENAME = "Rename the columns to Student ID, Name, Class, Subject 1, Subject 2 and on, and give it again"
_ID_HEADS = ("student id", "admission no", "id", "reg no")
_NAME_HEADS = ("name", "student name", "full name")
_CLASS_HEADS = ("class", "form class", "form", "class code")
_SUBJECT_HEAD = re.compile(r"(?:subject|option|band) ?\d+")
_SUBJECT_LIST = "subjects"
_LIST_SPLIT = re.compile(r"[,;]")
_DELIMITERS = ",;\t|"
_WANTED = {"id": "a student ID column (Student ID, Admission No, ID or Reg No)",
           "name": "a name column (Name, Student Name or Full Name)",
           "class": "a class column (Class, Form Class, Form or Class Code)",
           "subjects": "subject columns (Subject 1, Option 1, Band 1 and on, or Subjects)"}
# the refusal net, wider than the reader: a row with one cell naming a class and another naming a
# student looks like a student list in other words, and is refused (never sent to the AI)
_LOOSE_CLASS = re.compile(r"\b(?:class(?:es)?|form|tutor ?group|home ?room|register)\b")
_LOOSE_WHO = re.compile(r"\b(?:name|surname|first ?name|id|admission|adm|reg|index|student|pupil)\b")
_NOT_WHO = re.compile(r"\b(?:teachers?|staff|subjects?|rooms?|venues?|department|dept|course|block|building)\b")
_PLAN_ROW = re.compile(r"\b(?:periods?|lessons?|load|hours)\b")
_PLAN_LINE = re.compile(r"\b(?:periods?|lessons?|load|hours|teachers?|staff|rooms?|venues?)\b")
LINE_WORDS = 12             # a PDF line longer than this is prose, not a header


def _head(cell) -> str:
    """A header cell as it is compared: lower case, punctuation as spaces ("Admission No." is "admission no")."""
    return " ".join(re.sub(r"[^0-9a-z]+", " ", str(cell if cell is not None else "").lower()).split())


def _student_columns(header: list) -> dict | None:
    """Where a header row's student-list columns are: {id, name, class: index | None, subjects: [(index,
    label, a comma list?)]}; None unless it names a class and an ID or a name (it then looks like a
    student list, read or refused, and never goes to the AI)."""
    heads = [_head(c) for c in header]

    def first(names) -> int | None:
        return next((i for i, h in enumerate(heads) if h in names), None)
    cols = {"id": first(_ID_HEADS), "name": first(_NAME_HEADS), "class": first(_CLASS_HEADS),
            "subjects": [(i, " ".join(str(header[i]).split()), h == _SUBJECT_LIST) for i, h in enumerate(heads)
                         if h == _SUBJECT_LIST or _SUBJECT_HEAD.fullmatch(h)]}
    if cols["class"] is None or (cols["id"] is None and cols["name"] is None):
        return None
    return cols


def looks_like_students(cells: list, words: bool = False) -> bool:
    """A header row that looks like a student list's in other words: one cell naming a class (class, form,
    tutor group, homeroom, register) and another naming a student (name, surname, first name, ID,
    admission, adm, reg, index, student, pupil), as whole words in any case. A who-cell about a teacher,
    a subject or a room does not count, nor does a row with periods, lessons, load or hours (a plan's
    table). `words`: the cells are a text line's words (a PDF), which must be short and name no teacher."""
    heads = [_head(c) for c in cells]
    heads = [h for h in heads if h]
    if not heads or (words and len(heads) > LINE_WORDS):
        return False
    if any((_PLAN_LINE if words else _PLAN_ROW).search(h) for h in heads):
        return False
    klass = {i for i, h in enumerate(heads) if _LOOSE_CLASS.search(h)}
    who = {i for i, h in enumerate(heads) if _LOOSE_WHO.search(h) and not _NOT_WHO.search(h)}
    return any(i != j for i in klass for j in who)


def _cell(v) -> str:
    if isinstance(v, float) and v.is_integer():
        v = int(v)                          # an ID or a code typed as a number: 1003, not 1003.0
    return "" if v is None else " ".join(str(v).split())


def _find(rows: list[list], named: bool) -> tuple | None:
    """In a sheet's or a CSV's rows (searched to the end: a school's export may open with a title block):
    ("table", header, [(file row number, row)] under it) at the first row naming the student-list
    columns; else ("loose",) when some row looks like a student list's header in other words, or the
    sheet is `named` Students; else None."""
    loose = named
    for i, row in enumerate(rows):
        cells = list(row)
        if _student_columns(cells) is not None:
            return "table", cells, [(n, list(r)) for n, r in enumerate(rows[i + 1:], i + 2)]
        loose = loose or looks_like_students(cells)
    return ("loose",) if loose else None


def _decoded(data: bytes) -> str:
    if data.startswith((b"\xff\xfe", b"\xfe\xff")):
        text = data.decode("utf-16", errors="replace")
    else:
        try:
            text = data.decode("utf-8-sig")
        except UnicodeDecodeError:
            text = data.decode("cp1252", errors="replace")
    return text.replace("\x00", "")         # UTF-16 without its mark reads as text between NULs


def _csv_table(data: bytes) -> tuple | None:
    """A CSV read with a comma, then the delimiter the sniffer sees, then each other common one, until
    one finds a student list; a CSV none of them can split is looked at line by line for the loose test."""
    text = _decoded(data)
    try:
        sniffed = csv.Sniffer().sniff(text[:4096], _DELIMITERS).delimiter
    except csv.Error:
        sniffed = ","
    for delimiter in dict.fromkeys([",", sniffed, *_DELIMITERS]):
        try:
            found = _find(list(csv.reader(io.StringIO(text), delimiter=delimiter)), False)
        except csv.Error:
            continue
        if found is not None:
            return found
    if any(looks_like_students(re.split(r"[,;\t|]", line)) for line in text.splitlines()):
        return ("loose",)
    return None


def students_table(name: str, data: bytes) -> tuple | None:
    """What a file holds as a student list: ("table", header, numbered rows) to read exactly, ("loose",)
    to refuse, or None for any other file. An .xlsx's sheet named Students comes first, then its first
    sheet with the student-list columns, then any sheet that looks like a student list in other words.
    A workbook openpyxl cannot open is not one (whatever reads it next says why)."""
    lower = name.lower()
    if lower.endswith(".csv"):
        return _csv_table(data)
    if not lower.endswith(".xlsx"):
        return None
    import openpyxl
    try:
        wb = openpyxl.load_workbook(io.BytesIO(data), read_only=True, data_only=True)
    except Exception:
        return None
    try:
        tables, loose = [], None
        for ws in wb.worksheets:
            named = str(ws.title).strip().casefold() == STUDENTS_SHEET
            ws.reset_dimensions()           # a stale <dimension ref="A1"/> would hide every other cell
            found = _find([list(r) for r in ws.iter_rows(values_only=True)], named)
            if found is not None and named:
                return found
            if found is not None and found[0] == "table":
                tables.append(found)
            elif found is not None:
                loose = loose or found
        return tables[0] if tables else loose
    finally:
        wb.close()


def sheet_students(name: str, data: bytes) -> tuple | None:
    """What a planning workbook's sheet named Students holds, to be read as `students_table` reads a
    file: ("table", header, numbered rows) to read exactly, ("loose",) to refuse; None when the workbook
    has no such sheet or it holds no student (no cell, no row under its header, or only the start
    wizard's example row). Only that sheet is looked at: any other sheet of a planning workbook is a
    department's, a venue list and so on."""
    import openpyxl
    try:
        wb = openpyxl.load_workbook(io.BytesIO(data), read_only=True, data_only=True)
    except Exception:
        return None
    try:
        ws = next((w for w in wb.worksheets if str(w.title).strip().casefold() == STUDENTS_SHEET), None)
        if ws is None:
            return None
        ws.reset_dimensions()
        rows = [list(r) for r in ws.iter_rows(values_only=True)]
    finally:
        wb.close()
    if not any(_cell(v) for r in rows for v in r):
        return None
    found = _find(rows, True)
    if found is None or found[0] != "table":
        return found
    _, header, body = found
    body = [(n, r) for n, r in body if any(_cell(v) for v in r)]
    cols = _student_columns(header)
    if not body:
        return None
    if len(body) == 1 and cols is not None and cols["id"] is not None and cols["name"] is not None:
        row = body[0][1]
        if (_cell(row[cols["id"]]) == ST.EXAMPLE_ID if cols["id"] < len(row) else False) and \
                (_cell(row[cols["name"]]) == ST.EXAMPLE_NAME if cols["name"] < len(row) else False):
            return None
    return "table", header, body


def document_students(ex) -> bool:
    """A Word or PDF text (as extract gives it) holding a student list's header: a Word table row
    ("| a | b |") or a short PDF line that looks like one."""
    for line in ex.text.splitlines():
        line = line.strip()
        if ex.kind == "docx" and line.startswith("|"):
            if looks_like_students([c.strip() for c in line.strip("|").split("|")]):
                return True
        elif ex.kind == "pdf" and line and not line.startswith("## "):
            if looks_like_students(line.split(), words=True):
                return True
    return False


def _student_rows(cols: dict, rows: list[tuple[int, list]]) -> list[dict]:
    """The rows students.read_rows takes ({row, id, name, class, subjects, columns}, `row` the file's own
    row number), blank rows left out."""
    def at(row, i):
        return _cell(row[i]) if i is not None and i < len(row) else ""
    out = []
    for n, row in rows:
        if not any(_cell(v) for v in row):
            continue
        subjects, columns = [], []
        for i, label, is_list in cols["subjects"]:
            cells = [x.strip() for x in _LIST_SPLIT.split(at(row, i))] if is_list else [at(row, i)]
            subjects += cells
            columns += [label] * len(cells)
        out.append({"row": n, "id": at(row, cols["id"]), "name": at(row, cols["name"]),
                    "class": at(row, cols["class"]), "subjects": subjects, "columns": columns})
    return out


def _students_failed(db, name: str, why: str, tid: str | None, message: str | None = None) -> dict:
    message = message or f"This looks like a student list but {why}. {NOT_AI}"
    entry = _entry(db, name, {"kind": "students", "via": "exact", "status": "failed", "sections": {},
                              "doubts": [], "summary": "Student list", "error": message}, tid)
    return {"name": name, "status": "failed", "id": entry["id"], "message": message}


def _to_check(problems: list[dict], students: list[dict], rows: list[dict]) -> int:
    """How many students (or rows that made none) a list's problems name."""
    made = {s["id"] for s in students}
    id_at = {r["row"]: r["id"] for r in rows}
    keys = set()
    for p in problems:
        if "student" in p:
            keys.add(p["student"])
        else:
            sid = id_at.get(p.get("row"), "")
            keys.add(sid if sid in made and p["kind"] != "duplicate_id" else ("row", p.get("row")))
    return len(keys)


def _students(db, name: str, found: tuple, tid: str | None, plan: dict | None = None) -> dict:
    if found[0] != "table":
        return _students_failed(db, name, f"its columns weren't recognised. {RENAME}", tid)
    _, header, body = found
    cols = _student_columns(header)
    missing = [_WANTED[k] for k in ("id", "name", "class", "subjects") if cols[k] in (None, [])]
    if missing:
        return _students_failed(db, name, "its columns weren't recognised: it needs "
                                + " and ".join(missing), tid)
    plan = plan if plan is not None else _current(db, tid)
    rows = _student_rows(cols, body)
    if not rows:
        return _students_failed(db, name, "it has no rows under its header", tid)
    read, problems = ST.read_rows(rows, plan)
    if not read:
        return _students_failed(db, name, f"none of its {len(rows)} rows has a student ID", tid)
    codes = {c["code"] for c in plan["classes"]}
    if not any(s["class"] in codes for s in read):
        return _students_failed(db, name, "none of its classes is in the plan" if codes
                                else "the plan has no classes yet: load the plan first", tid)
    stored = [s for s in db.get_value("students", tid) or [] if isinstance(s, dict)]
    before = {s.get("id"): s for s in stored}
    ids = {s["id"] for s in read}
    added = sum(1 for s in read if s["id"] not in before)
    changed = sum(1 for s in read if s["id"] in before and before[s["id"]] != s)
    removed = sum(1 for sid in before if sid not in ids)
    texts = list(dict.fromkeys(p["text"] for p in problems))          # `text` only: `detail` is a raw cell
    replaces = ([f"This list replaces the {len(before):,} student{'' if len(before) == 1 else 's'} stored; "
                 f"{removed:,} of them {'isn' if removed == 1 else 'aren'}'t in this file"] if before else [])
    doubts = replaces + texts[:document.DOUBTS_MAX]
    if len(texts) > document.DOUBTS_MAX:
        doubts.append(f"and {len(texts) - document.DOUBTS_MAX} more to check")
    n, k = len(read), _to_check(problems, read, rows)
    summary = f"{n:,} student{'' if n == 1 else 's'}, {k:,} to check"
    secs = {"students": {"count": n, "added": added, "changed": changed, "removed": removed, "samples": [],
                         "ticked": True}}
    entry = _entry(db, name, {"kind": "students", "via": "exact", "status": "waiting", "sections": secs,
                              "doubts": doubts, "summary": summary, "students": read}, tid)
    return {"name": name, "status": "read", "id": entry["id"], "message": f"Student list: {summary}"}


def _students_branch(db, name: str, data: bytes, tid: str | None, plan: dict | None = None,
                     table=None) -> dict | None:
    """A student list read or refused (a stored reading's result); None for any other file. Whatever goes
    wrong here is a students card, logged by its kind only: an exception's words may hold a cell.
    `plan`, the plan to read against (the stored one by default); `table`, the function that finds the
    list in the file (`students_table` by default)."""
    try:
        found = (table or students_table)(name, data)
    except Exception as e:
        _log.warning("checking %s for a student list failed (%s)", name, type(e).__name__)
        return _students_failed(db, name, "", tid, message=f"{name} could not be checked for a student list, "
                                f"so it was not read; save it again as .xlsx or CSV and give it again. {NOT_AI}")
    if found is None:
        return None
    try:
        return _students(db, name, found, tid, plan)
    except M.PlanError:
        raise                               # the stored plan itself does not read: said as for any file
    except Exception as e:
        _log.warning("reading the student list %s failed (%s)", name, type(e).__name__)
        return _students_failed(db, name, "it could not be read exactly; check its columns and rows", tid)


# ---------------------------------------------------------------------------
# a drop of files
# ---------------------------------------------------------------------------

def _one(db, provider_factory, i: int, name: str, data: bytes, tid: str | None, timetables: list,
         sizes: list[tuple[str, bytes]] = (), alongside: list | None = None) -> dict | None:
    lower = name.lower()
    if is_planning(name, data):
        return _exact(db, name, data, tid, sizes=sizes, alongside=alongside)
    if sizes_text(name, data) is not None:  # class sizes: read exactly, as the plan upload once took them
        return _exact(db, name, data, tid, "Class sizes")
    if lower.endswith((".xlsx", ".csv")):   # a student list: read exactly or refused, never by the AI
        students = _students_branch(db, name, data, tid)
        if students is not None:
            return students
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
    also: list[dict] = []                   # a planning workbook's Students sheet: after every file's own result
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
                out = _one(db, provider_factory, i, name, data, tid, timetables, sizes, also)
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
            results[j] = _one(db, provider_factory, j, name, data, tid, timetables, alongside=also)
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
    return [*(results[i] for i in sorted(results)), *also]     # last: the results line up with the files


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


def view(entry: dict, settings: dict | None = None) -> dict:
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
            "applied": r.get("applied"),      # {section: count} once applied: what the card says was applied
            "week_adopted": bool((r.get("undo") or {}).get("week_adopted")),
            **_week_view(r, settings)}


def _week_view(r: dict, settings: dict | None) -> dict:
    """{"week": {"file": [lines], "mine": [lines], "time": the file's week}} when an export's week differs from
    the timetable's as it is now, as the card shows it; nothing otherwise (or without `settings`)."""
    if not (settings and isinstance(r.get("week"), dict)):
        return {}
    try:
        theirs = W.shape(W.derive(r["week"]))
    except (KeyError, TypeError, ValueError):
        return {}
    mine = W.shape(settings.get("time"), settings.get("rules"))
    if _same_week(mine, theirs):
        return {}
    return {"week": {"file": W.describe(theirs), "mine": W.describe(mine), "time": r["week"]}}


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
