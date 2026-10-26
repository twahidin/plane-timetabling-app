"""The five exact readers (spec docs/superpowers/specs/2026-10-20-csv-pack-design.md §2, and its "Calls
made by the designer" §2, §3, §5, §6, §8 and §9): staff, venues, requirements, combinations and the
level students list. Each file is read row by row, never by the AI, and recognised by its headers,
not its name.

`table` turns a .csv or a single-sheet .xlsx into a header and the file's own numbered rows; `detect`
names its kind; `missing` names the required columns still to add; each reader returns its items and
its problems. A problem is {"file", "row", "text", "dropped"}: `row` is the file's own row number (None
for a note about the whole file). A structural problem (a missing key, a cell that does not read, a
duplicate, a combination breaking Calls §8, an unknown level, more cells than the header) leaves its row
out of the items, `dropped` True (Calls §3, ruling R11). A cross-file mismatch keeps the row and is
listed, `dropped` False: a teacher's unknown subject or level, a student's unmatched choice (only that
choice is left out) or merit problem, a requirement whose department must be said (R13). Texts carry
the file name, row numbers, codes, subject words and header words, and never a student's name.

`context` is {"levels", "combinations", "plan"} (and optionally "staff", a staff file's items in the
same drop): the facts already stored plus the other files of the drop, for the cross-file checks.

Pure: no database, no web, no logging of cell values."""
from __future__ import annotations

import csv
import io
import math
import re

from ... import blocks as BK, levels as LV, students as ST
from .. import model as M
from .. import specialisation as SP
from ..pattern import parse_pattern
from .tabular import cell, decoded, delimiters, head

KINDS = ("staff", "venues", "requirements", "combinations", "level_students")
FILES = {"staff": "staff.csv", "venues": "venues.csv", "requirements": "requirements.csv",
         "combinations": "combinations.csv", "level_students": "students.csv"}

# The blank copies a school downloads (start wizard): each file's headers and ONE example row. An example
# row's first cell starts with "e.g." (R22): `table` skips any row of any file whose first cell does, wherever
# it stands, without a problem, so a blank file reads as nothing and the example can stay beside real rows.
EXAMPLE_MARK = "e.g."
HEADERS = {
    "staff": ["Name", "Short", "Department", "Load", "Subject 1", "Levels 1", "Subject 2", "Levels 2",
              "Subject 3", "Levels 3"],
    "venues": ["Room", "Kind", "Capacity"],
    "requirements": ["Teacher", "Level", "Subject", "Classes", "Groups", "Periods a week", "Lesson pattern",
                     "Runs with", "Taught as", "Room kind", "Department"],
    "combinations": ["Level", "Code", "Name", "Subject 1", "Subject 2", "Subject 3", "Places"],
    "level_students": ["Student ID", "Name", "Level", "Merit", "Choice 1", "Choice 2", "Choice 3"],
}
EXAMPLES = {
    "staff": ["e.g. Teacher Name", "TN", "Science", "24", "Physics", "Sec 3, Sec 4", "Science", "Sec 1, Sec 2", "", ""],
    "venues": ["e.g. Lab 1", "Lab", "32"],
    "requirements": ["e.g. Teacher Name", "Sec 3", "Physics", "2", "", "4", "2x2p", "", "Class", "Lab", "Science"],
    "combinations": ["e.g. Sec 3", "", "Pure Sciences", "Physics", "Chemistry", "Biology", "30"],
    "level_students": ["e.g. S0001", "Student Name", "Sec 3", "1", "Physics, Chemistry, Biology", "", ""],
}

# What each file is for and what each column holds, for the README that goes in the download. Written with
# the template's own words as {person}, {group}, {requirement} and {venue}; the headers themselves never change.
PURPOSES = {
    "staff": "Who teaches: each {person}, their teaching load and the {requirement}s they can take.",
    "venues": "Where {group}s meet: each {venue}, its kind and its capacity.",
    "requirements": "What each level needs: every {requirement}, how many periods a week and how it is taught.",
    "combinations": "The subject combinations a level offers, for levels that split into options.",
    "level_students": "Each student of a level, their merit rank and the combinations they would choose.",
}
DESCRIPTIONS = {
    "staff": {
        "Name": "The {person}'s full name, as it should print",
        "Short": "A short code for the {person}, different for each (leave empty to have one made)",
        "Department": "The department the {person} belongs to",
        "Load": "The periods a week the {person} teaches (leave empty for the usual load)",
        "Subject 1": "A {requirement} the {person} can take",
        "Levels 1": "The levels they take it at, separated by commas (leave empty for every level)",
        "Subject 2": "A second {requirement}, if there is one (leave empty when there is no more)",
        "Levels 2": "The levels for the second {requirement} (leave empty for every level)",
        "Subject 3": "A third {requirement}, if there is one (leave empty when there is no more)",
        "Levels 3": "The levels for the third {requirement} (leave empty for every level)",
    },
    "venues": {
        "Room": "The {venue}'s name",
        "Kind": "What sort of {venue} it is, such as the kind a {requirement} asks for",
        "Capacity": "How many people it holds",
    },
    "requirements": {
        "Teacher": "The {person} who teaches it (leave empty to have one picked; one row for each {person})",
        "Level": "The level the {requirement} is for",
        "Subject": "The {requirement}'s name",
        "Classes": "How many {group}s that {person} takes for it (leave empty for the rest)",
        "Groups": "For a {requirement} that runs with others: how many groups that {person} takes (leave empty to work it out)",
        "Periods a week": "How many periods a week each {group} has it",
        "Lesson pattern": "How the periods fall into lessons, such as 2x2p for two lessons of two periods (leave empty for the usual)",
        "Runs with": "Another {requirement}, or a name for a set of them, that runs at the same time (leave empty when it runs alone)",
        "Taught as": "Class (each {group} on its own), Combined N (N {group}s together) or Banded (split across options)",
        "Room kind": "The kind of {venue} it needs (leave empty for any {venue})",
        "Department": "The department that teaches it",
    },
    "combinations": {
        "Level": "The level the combination belongs to",
        "Code": "A short code for the combination, different within a level (leave empty to have one made)",
        "Name": "What the combination is called",
        "Subject 1": "A {requirement} in the combination",
        "Subject 2": "A second {requirement} in the combination",
        "Subject 3": "A third {requirement} in the combination (leave empty when there is no more)",
        "Places": "How many students the combination can take",
    },
    "level_students": {
        "Student ID": "The student's ID, different for each student",
        "Name": "The student's name",
        "Level": "The student's level",
        "Merit": "The student's merit rank, 1 for the first place",
        "Choice 1": "The student's first choice of combination, as its subjects separated by commas",
        "Choice 2": "Their second choice (leave empty when there is none)",
        "Choice 3": "Their third choice (leave empty when there is none)",
    },
}


def is_example(cells) -> bool:
    """Whether a row is an example row: its first cell, trimmed, starts with "e.g." in any case."""
    return bool(cells) and cell(cells[0]).lower().startswith(EXAMPLE_MARK)


class Rows(list):
    """A file's numbered body rows; `examples` counts the example rows `table` skipped."""
    examples = 0


# ---------------------------------------------------------------------------
# headers
# ---------------------------------------------------------------------------

def _singular(word: str) -> str:
    if len(word) <= 2 or not word.endswith("s") or word.endswith("ss"):
        return word
    if word.endswith("ies") and len(word) > 4:
        return word[:-3] + "y"              # capacities
    if word.endswith("sses"):
        return word[:-2]                    # classes
    return word[:-1]


def _key(c) -> str:
    """A header cell as it is compared: case, punctuation and plural aside ("Periods a week" is
    "period a week", "Dept." is "dept")."""
    return " ".join(_singular(w) for w in head(c).split())


def _keys(*names: str) -> frozenset:
    return frozenset(_key(n) for n in names)


class _Exact(frozenset):
    """Header words matched as written (`head`: case and punctuation aside), never plural-folded: for the
    columns whose singular a school already uses for something else ("Group" holds group names)."""


def _exact(*names: str) -> _Exact:
    return _Exact(head(n) for n in names)


# Each kind's columns: canonical name -> the header words that name it (compared by `_key`).
_ALIASES = {
    "staff": {
        "name": _keys("Name", "Teacher", "Staff", "Teacher name", "Staff name", "Full name"),
        "short": _keys("Short", "Short name", "Initials"),
        "department": _keys("Department", "Dept"),
        "load": _keys("Load", "Load periods"),
        "load factor": _keys("Load factor", "Teaching load factor"),
        "subjects": _keys("Subjects", "Specialisation", "Specialization"),
    },
    "venues": {
        "room": _keys("Room", "Venue", "Room name", "Venue name"),
        "kind": _keys("Kind", "Room type", "Room kind", "Type", "Venue type", "Venue kind"),
        "capacity": _keys("Capacity", "Seats", "Size"),
    },
    "requirements": {
        "level": _keys("Level", "Year", "Year group"),
        "subject": _keys("Subject"),
        "periods": _keys("Periods", "Periods a week", "Periods per week", "Periods a cycle", "Periods per cycle"),
        "teacher": _keys("Teacher", "Staff"),
        "classes": _exact("Classes", "Number of classes"),
        # as written: "Class", "Group", "Block" and "Band" are columns schools already have, holding names
        "groups": _exact("Groups"),
        "pattern": _exact("Lesson pattern", "Pattern", "Blocks"),
        "runs with": _exact("Runs with"),
        "taught as": _keys("Taught as", "How taught"),
        "combine": _keys("Combine", "Classes combined", "Combined classes"),
        "room kind": _keys("Room kind", "Room type", "Venue kind", "Venue type"),
        "department": _keys("Department", "Dept"),
    },
    "combinations": {
        "level": _keys("Level", "Year", "Year group"),
        "code": _keys("Code", "Combination code"),
        "name": _keys("Name", "Combination name", "Combination"),
        "places": _keys("Places", "Seats"),
    },
    "level_students": {
        "student id": _keys("Student ID", "Admission no", "Admission number", "ID", "Reg no"),
        "name": _keys("Name", "Student name", "Full name"),
        "level": _keys("Level", "Year", "Year group"),
        "merit": _keys("Merit", "Merit rank"),
    },
}
# numbered columns, matched on `_key`: Subject 1, Subject 2... and Choice 1, Choice 2...
_NUMBERED = {"combinations": re.compile(r"subject ?(\d+)"), "staff": re.compile(r"subject ?(\d+)"),
             "level_students": re.compile(r"(?:choice|preference|combination) ?(\d+)")}
_STAFF_LEVELS = re.compile(r"levels? ?(\d+)")
_FIRST = {"combinations": "subject 1", "staff": "subject 1", "level_students": "choice 1"}

# What recognises a kind (spec §2), and what it must also have to be read; each entry is a set of
# alternatives, one of which is enough. R25: staff.csv is Name + Department + (Subjects or Load), so a
# staff list without Subjects is read exactly; requirements.csv is recognised by Taught as too, so one
# without Periods is refused naming that column, never given to the AI.
_DETECT = {
    "level_students": [("student id",), ("level",), ("choice 1",)],
    "combinations": [("level",), ("subject 1",)],
    "requirements": [("level",), ("subject",), ("periods", "taught as")],
    "staff": [("name",), ("department",), ("subjects", "load", "subject 1")],
    "venues": [("room",), ("kind", "capacity")],
}
_REQUIRED = {**_DETECT, "combinations": [("level",), ("subject 1",), ("places",)],
             "requirements": [("level",), ("subject",), ("periods",)]}
_SHOWN = {"name": "Name", "department": "Department", "subjects": "Subjects", "room": "Room",
          "kind": "Kind", "capacity": "Capacity", "level": "Level", "subject": "Subject",
          "periods": "Periods a week", "subject 1": "Subject 1", "places": "Places", "student id": "Student ID",
          "choice 1": "Choice 1", "short": "Short", "load": "Load", "load factor": "Load factor",
          "taught as": "Taught as", "combine": "Combine", "teacher": "Teacher", "classes": "Classes",
          "groups": "Groups", "pattern": "Lesson pattern", "runs with": "Runs with", "room kind": "Room kind",
          "code": "Code", "merit": "Merit"}
_CLASS = _keys("Class", "Form class", "Form", "Class code")      # a file with one is B1's list (Calls §4)
_SERIAL = _keys("S/N", "SN", "S No", "Serial", "Serial no", "Serial number", "No")


def _columns(kind: str, header: list) -> tuple[dict, dict, list]:
    """({canonical: index}, {canonical: the header as written}, [(index, its number)] of the numbered
    columns in number order). The first header naming a column wins."""
    keys = [_key(h) for h in header]
    cols: dict = {}
    heads = [head(h) for h in header]
    for canon, names in _ALIASES[kind].items():
        found = heads if isinstance(names, _Exact) else keys
        at = next((i for i, k in enumerate(found) if k in names), None)
        if at is not None:
            cols[canon] = at
    numbered: list = []
    if kind in _NUMBERED:
        ranked = sorted((int(m.group(1)), i) for i, k in enumerate(keys) if (m := _NUMBERED[kind].fullmatch(k)))
        numbered = [(i, n) for n, i in ranked if n >= 1]
        if any(n == 1 for _, n in numbered):
            cols[_FIRST[kind]] = next(i for i, n in numbered if n == 1)
    labels = {canon: cell(header[i]) or _SHOWN.get(canon, canon) for canon, i in cols.items()}
    return cols, labels, numbered


def staff_pairs(header: list) -> list[tuple[int, int | None, int]]:
    """(Subject N column, its Levels N column or None, N) in number order, from a staff header; matched
    on `_key`, and "Level N" is accepted for "Levels N". The first header with a number wins."""
    keys = [_key(h) for h in header]
    subjects: dict[int, int] = {}
    levels: dict[int, int] = {}
    for i, k in enumerate(keys):
        for found, pattern in ((subjects, _NUMBERED["staff"]), (levels, _STAFF_LEVELS)):
            m = pattern.fullmatch(k)
            if m and int(m.group(1)) >= 1:
                found.setdefault(int(m.group(1)), i)
    return [(subjects[n], levels.get(n), n) for n in sorted(subjects)]


def _lacking(kind: str, header: list, table: dict) -> list[str]:
    cols = _columns(kind, header)[0]
    return [" or ".join(_SHOWN[a] for a in alts) for alts in table[kind] if not any(a in cols for a in alts)]


def detect(header) -> str | None:
    """The kind of pack file a header row names (spec §2), or None. A file with a Class column is B1's
    student list, never the level students list; with a student ID column too it is no pack file at all.
    One that reads as staff.csv but has Level and Subject columns is requirements.csv lacking Periods,
    unless its Subject is not a singular one (a staff list's own Subjects and Levels columns fold to it)."""
    header = list(header or [])
    keys = {_key(h) for h in header}
    heads = {head(h) for h in header}
    if keys & _CLASS and keys & _ALIASES["level_students"]["student id"]:
        return None                         # a student list with a class: B1's, whatever else it has
    for kind in _DETECT:
        if kind == "level_students" and keys & _CLASS:
            continue
        if not _lacking(kind, header, _DETECT):
            if (kind == "staff" and "subject" in heads and not heads & _STAFF_SUBJECTS
                    and not _lacking("requirements", header, _LEVEL_SUBJECT)):
                return "requirements"       # a deployment file lacking Periods: its card names the column
            return kind
    return None


# A header with these is requirements.csv, never staff.csv (a Teacher, Department and Subject column read
# as a staff list's too).
_LEVEL_SUBJECT = {"requirements": [("level",), ("subject",)]}
# ...but only with a Subject column written in the singular and none of the staff list's own, as written.
_STAFF_SUBJECTS = frozenset(head(n) for n in ("Subjects", "Specialisation", "Specialization"))


_STUDENT_ID = _keys("Student ID", "Admission no", "Admission number", "Reg no")


_CLASS_AS_WRITTEN = frozenset(head(n) for n in ("Class", "Form class", "Form", "Class code"))


def names_a_class(header) -> bool:
    """Whether a header has a Class column. None of the five files has one: a file that does is B1's
    student list, a home-room list or a per-class table, and keeps the route it had before them. The
    plural is not one: a "Classes" column lists several classes on a row, beside a requirement's teachers."""
    return bool({head(h) for h in header or []} & _CLASS_AS_WRITTEN)


def names_students(header) -> bool:
    """Whether a header with no Class column still names students: a student ID column, or a bare ID
    beside a Level. Such a file that `detect` does not take is the level students list missing a column,
    refused with what to add, never given to the AI."""
    keys = {_key(h) for h in header or []}
    if keys & _CLASS:
        return False
    ids = _ALIASES["level_students"]["student id"]
    return bool(keys & _STUDENT_ID) or bool(keys & ids and keys & _ALIASES["level_students"]["level"])


def missing(kind: str, header) -> list[str]:
    """The required columns of `kind` that `header` still lacks, as a person would name them."""
    return _lacking(kind, list(header or []), _REQUIRED)


# ---------------------------------------------------------------------------
# table
# ---------------------------------------------------------------------------

def _shape(rows) -> tuple[list[str], list[tuple[int, list]]] | None:
    """Numbered raw rows as (header, body): the header is the first row naming a kind (else the first
    non-blank row), trailing blank columns and an S/N column are dropped, and blank rows are skipped.
    A row with non-blank cells past the header keeps them after its header-width cells, so a reader
    can refuse it (`_overflows`) instead of losing them; blank cells past the header are dropped."""
    rows = [(n, [cell(v) for v in r]) for n, r in rows]
    rows = [(n, r) for n, r in rows if any(r)]
    if not rows:
        return None
    at = next((i for i, (_, r) in enumerate(rows) if detect(r) is not None), 0)
    header = rows[at][1]
    width = len(header)
    while width and not header[width - 1]:
        width -= 1
    keep = [i for i in range(width) if _key(header[i]) not in _SERIAL]
    body = Rows()
    for n, r in rows[at + 1:]:
        cells = [r[i] if i < len(r) else "" for i in keep]
        extra = r[width:]
        while extra and not extra[-1]:
            extra = extra[:-1]
        if any(cells) or extra:
            if is_example(cells):
                body.examples += 1
            else:
                body.append((n, cells + extra))
    return [header[i] for i in keep], body


def _csv(data: bytes):
    text = decoded(data)
    first = None
    for delimiter in delimiters(text):
        try:
            shaped = _shape(enumerate(csv.reader(io.StringIO(text), delimiter=delimiter), 1))
        except csv.Error:
            continue
        if shaped is not None and detect(shaped[0]) is not None:
            return shaped
        first = first or shaped
    return first


def _xlsx(data: bytes):
    import openpyxl
    try:
        wb = openpyxl.load_workbook(io.BytesIO(data), read_only=True, data_only=True)
    except Exception:           # noqa: BLE001 - a workbook that does not open is not a pack file
        return None
    try:
        found = None
        for ws in wb.worksheets:
            ws.reset_dimensions()           # a stale <dimension ref="A1"/> would hide every other cell
            rows = [list(r) for r in ws.iter_rows(values_only=True)]
            if not any(cell(v) for r in rows for v in r):
                continue
            if found is not None:
                return None                 # a second non-empty sheet: a planning workbook, not a pack file
            found = rows
    except Exception:           # noqa: BLE001 - a sheet that does not read is not a pack file
        return None
    finally:
        wb.close()
    return None if found is None else _shape(enumerate(found, 1))


def table(name: str, data: bytes) -> tuple[list[str], list[tuple[int, list]]] | None:
    """A .csv (UTF-8 with or without a BOM, UTF-16, cp1252; `,` `;` tab or `|`) or a single-sheet .xlsx
    as (header, [(the file's row number, cells)]), each cell text. None for another file type, a
    workbook that does not open or has more than one non-empty sheet, or a file with no cell."""
    lower = str(name or "").lower()
    if lower.endswith(".csv"):
        return _csv(data)
    if lower.endswith(".xlsx"):
        return _xlsx(data)
    return None


# ---------------------------------------------------------------------------
# shared
# ---------------------------------------------------------------------------

class _Out:
    """A reader's problems, by file and row."""

    def __init__(self, file: str):
        self.file, self.problems = file, []

    def add(self, row, text: str, dropped: bool = True) -> None:
        """A problem at `row`; `dropped`: the row is left out of the items (a structural problem)."""
        where = f"{self.file} row {row}" if row is not None else self.file
        self.problems.append({"file": self.file, "row": row, "text": f"{where}: {text}", "dropped": dropped})

    def dropped(self) -> set:
        """The rows left out of the items."""
        return {p["row"] for p in self.problems if p["dropped"] and p["row"] is not None}

    def overflows(self, n: int, row: list, header: list) -> bool:
        """True, with a structural problem, when `row` has cells past the header (an unquoted list)."""
        if any(cell(v) for v in row[len(header):]):
            self.add(n, "more cells than the header — put a list in quotes")
            return True
        return False

    def sorted(self) -> list[dict]:
        return sorted(self.problems, key=lambda p: (p["row"] is not None, p["row"] or 0))


def _getter(cols: dict, row: list):
    def get(canon: str) -> str:
        i = cols.get(canon)
        return cell(row[i]) if i is not None and i < len(row) else ""
    return get


def _cell(row: list, i: int | None) -> str:
    return cell(row[i]) if i is not None and i < len(row) else ""


def _number(text: str) -> float | None:
    """A typed number; "0,5" is 0.5 (a decimal comma, as a `;` file from Excel writes it)."""
    text = text.strip()
    if text.count(",") == 1 and "." not in text:
        text = text.replace(",", ".")
    try:
        value = float(text)
    except ValueError:
        return None
    return value if math.isfinite(value) else None


def _whole(text: str) -> int | None:
    value = _number(text)
    return int(value) if value is not None and value.is_integer() else None


def _plan(context) -> dict:
    return (context or {}).get("plan") or {}


def _known_subjects(context) -> set[str]:
    """Every subject the requirements know, as `students.norm` keys: the levels store's and the plan's."""
    levels = (context or {}).get("levels") or {}
    return ({ST.norm(s.get("subject")) for lv in levels.values() for s in lv.get("subjects") or []}
            | SP.plan_subjects(_plan(context))) - {""}


def _known_levels(context) -> set[str]:
    """Every level known, as `level_key`s: the levels store's, the plan's classes' and requirements'."""
    plan = _plan(context)
    return (set((context or {}).get("levels") or {})
            | {SP.level_key(ST.level_of(plan, c.get("code"))) for c in plan.get("classes") or []}
            | {SP.level_key(r.get("level")) for r in plan.get("requirements") or []}) - {""}


def _and(words) -> str:
    words = list(words)
    return words[0] if len(words) == 1 else f"{', '.join(words[:-1])} and {words[-1]}"


# ---------------------------------------------------------------------------
# staff.csv
# ---------------------------------------------------------------------------

def read_staff(rows, header, context, file: str | None = None) -> tuple[list[dict], list[dict]]:
    """Teachers as {name, short, dept, allowance | load_factor, subjects}. Load is whole periods into
    `allowance`; Load factor (1, 0.5) goes to `load_factor` only when Load is blank (Calls §5). A blank
    Short is left empty (R24): the merge keeps a stored teacher's and gives a new one the initials.
    Subjects are the text form of specialisation; each is checked against the requirements known, or,
    when none are known yet, listed once as not yet checked (Calls §3). A subject or level the
    requirements don't have is listed and the teacher kept with it (R11): never "can't teach"."""
    out = _Out(file or FILES["staff"])
    cols, label, _ = _columns("staff", header)
    pairs = staff_pairs(header)
    subjects_known, levels_known = _known_subjects(context), _known_levels(context)
    items, kept, unchecked = [], {}, False
    for n, row in rows:
        if out.overflows(n, row, header):
            continue
        get = _getter(cols, row)
        name = get("name")
        if not name:
            out.add(n, f"no {label['name']}")
            continue
        item = {"name": name, "short": get("short"), "dept": get("department")}
        errors = []
        load, factor = get("load"), get("load factor")
        if load:
            whole = _whole(load)
            if whole is None or whole < 0:
                errors.append(f"{label['load']} {load} isn't a whole number of periods "
                              f"(a factor such as 0.5 goes in a Load factor column)")
            else:
                item["allowance"] = whole
        elif factor:
            value = _number(factor)
            if value is None or value <= 0:
                errors.append(f"{label['load factor']} {factor} isn't a number above 0")
            else:
                item["load_factor"] = value
        entries, unread = SP.parse_subjects(get("subjects"))
        paired, unpaired = SP.entries_from_pairs([
            (_cell(row, s), _cell(row, v), n) for s, v, n in pairs])
        entries = SP.merge_entries(entries + paired)
        errors += unread + unpaired
        item["subjects"] = entries
        if errors:
            for text in errors:
                out.add(n, text)
            continue
        if ST.norm(name) in kept:
            out.add(n, f"{name} appears twice (row {kept[ST.norm(name)]} is kept)")
            continue
        kept[ST.norm(name)] = n
        items.append(item)
        for entry in entries:
            shown = SP.format_subjects([entry])
            if not subjects_known:
                unchecked = True
            elif ST.norm(entry["subject"]) not in subjects_known:
                out.add(n, f"{shown} — no subject {entry['subject']} in the requirements", dropped=False)
            elif levels_known:
                for k in entry["levels"]:
                    if k not in levels_known:
                        out.add(n, f"{shown} — no level {SP._label(k)}", dropped=False)
    if unchecked:
        out.add(None, "subjects not yet checked — add requirements.csv", dropped=False)
    return items, out.sorted()


# ---------------------------------------------------------------------------
# venues.csv
# ---------------------------------------------------------------------------

def read_venues(rows, header, context, file: str | None = None) -> tuple[list[dict], list[dict]]:
    """Rooms as {name, kind, capacity}, read as `plan.model.normalise_venue` reads a venue."""
    out = _Out(file or FILES["venues"])
    cols, label, _ = _columns("venues", header)
    items, kept = [], {}
    for n, row in rows:
        if out.overflows(n, row, header):
            continue
        get = _getter(cols, row)
        name = get("room")
        if not name:
            out.add(n, f"no {label['room']}")
            continue
        try:
            venue = M.normalise_venue({"name": name, "kind": get("kind") or None, "capacity": get("capacity") or None})
        except M.PlanError as e:
            out.add(n, str(e))
            continue
        if venue["id"] in kept:
            out.add(n, f"{name} appears twice (row {kept[venue['id']]} is kept)")
            continue
        kept[venue["id"]] = n
        items.append({"name": venue["name"], "kind": venue["kind"], "capacity": venue["capacity"]})
    return items, out.sorted()


# ---------------------------------------------------------------------------
# requirements.csv
# ---------------------------------------------------------------------------

_CLASS_WORDS = ("", "class", "classes", "form class", "whole class")
_BANDED_WORDS = ("banded", "band", "bands")
_COMBINED = re.compile(r"combined\s*[x×]?\s*(\d+)")


def _taught_as(text: str, combine: str, label: str) -> tuple[str, object, str | None]:
    """(taught_as, combine, problem) for a Taught as cell: Class, Combined N (or Combined with a Combine
    column) or Banded, in any case."""
    t = " ".join(text.lower().split())
    if t in _CLASS_WORDS:
        return "class", 1, None
    if t in _BANDED_WORDS:
        return "banded", 1, None
    m = _COMBINED.fullmatch(t)
    if m:
        return "combined", int(m.group(1)), None
    if t == "combined":
        if combine:
            return "combined", combine, None
        return "class", 1, f"{text} needs its number of classes (Combined 2)"
    return "class", 1, f"{label} {text} isn't Class, Combined N or Banded"


def _staff_departments(context) -> dict[str, set]:
    """{norm(subject): the departments whose staff list it}, from the plan's staff and a staff file in
    the same drop."""
    found: dict[str, set] = {}
    people = list(_plan(context).get("staff") or []) + list((context or {}).get("staff") or [])
    for person in people:
        dept = str(person.get("dept") or "").strip()
        for entry in person.get("subjects") or [] if dept else []:
            found.setdefault(ST.norm(entry.get("subject")), set()).add(dept)
    return found


_NAME_SPLIT = re.compile(r"[;&/]")
_CODE_SPLIT = re.compile(r"[;,&/+\s]+")
_PLAIN_COUNT = re.compile(r"\d+(?:\.0+)?")
# a range of class codes, "3A-3D" or "3E1 – 3E4": a code with a letter and a digit on each side of a dash
_CODE = r"(?=[a-z0-9]*[a-z])(?=[a-z0-9]*\d)[a-z0-9]+"
_RANGE = re.compile(rf"{_CODE}\s*[-\u2013]\s*{_CODE}")


def _count(text: str, codes: bool) -> tuple[int | None, str | None]:
    """(count, None) for a Classes or Groups cell that reads. A whole number from 1 is itself; with `codes`
    (Classes) a list of class codes ("3A; 3B", "3A 3B", "3A+3B") is its number of different codes. Blank is
    (None, None). One that does not read is (None, "range") for a range of codes ("3A-3D"), else (None,
    "unread")."""
    if not text:
        return None, None
    if _PLAIN_COUNT.fullmatch(text):
        value = int(float(text))
        return (value, None) if value >= 1 else (None, "unread")
    if _number(text) is not None and not any(c.isalpha() for c in text):
        return None, "unread"               # a number that is no count: 0, 1.5, -2 ("1E2" is a class code)
    if codes and any(_RANGE.fullmatch(t.strip().lower()) for t in re.split(r"[;,&/+]", text)):
        return None, "range"
    tokens = {t.lower() for t in _CODE_SPLIT.split(text) if t} if codes else set()
    if tokens and all(any(c.isdigit() for c in t) for t in tokens):
        return len(tokens), None
    return None, "unread"


def read_requirements(rows, header, context, file: str | None = None) -> tuple[dict, list[dict]]:
    """The levels store's shape (`levels.normalise_levels`), grouped by level under the level's first
    spelling: one subject row per level and subject, built from the first row. The department is the
    Department column, else the one department whose staff list the subject (Calls §6); when none or
    several do, the row is kept with dept "" and listed, "say which department" (R13).

    The deployment columns (Teacher, Classes, Groups, Lesson pattern, Runs with) are optional and stored
    only when given: each row with a teacher or a count adds `{teacher, classes, groups}` to its subject's
    `deploy`. A later row of a level and subject that says something else for Periods, Lesson pattern,
    Taught as, Runs with, Room kind or Department is a kept doubt; the first row is kept. A later row's
    blank cell states nothing, its Periods too."""
    out = _Out(file or FILES["requirements"])
    cols, label, _ = _columns("requirements", header)
    departments = _staff_departments(context)
    grouped: dict[str, dict] = {}
    kept: dict[tuple, tuple[int, dict, dict]] = {}      # key -> (first row, its entry, its cells as written)
    for n, row in rows:
        if out.overflows(n, row, header):
            continue
        get = _getter(cols, row)
        level, subject, periods = get("level"), get("subject"), get("periods")
        if not level:
            out.add(n, f"no {label['level']}")
            continue
        if not subject:
            out.add(n, f"no {label['subject']}")
            continue
        key = (SP.level_key(level), ST.norm(subject))
        repeat = key in kept
        pattern = get("pattern")
        if pattern and head(header[cols["pattern"]]) == "blocks" and (
                not {"x", "p"} & set(pattern.lower()) or parse_pattern(pattern, None)[1]):
            pattern = ""        # "Blocks" may hold block numbers or names (B1): not a pattern, so not read

        def stated(canon: str) -> str:
            """A compared cell as the row states it: a Blocks cell that is no pattern states nothing."""
            return pattern if canon == "pattern" else get(canon)

        errors = []
        if not periods and not repeat:          # a later row's blank Periods states nothing, as its other cells
            errors.append(f"no {label['periods']}")
        taught = get("taught as")
        taught_as, combine, problem = _taught_as(taught, get("combine"), label.get("taught as", "Taught as"))
        if problem:
            errors.append(problem)
        dept, unsaid = get("department"), None
        if not dept:
            found = departments.get(ST.norm(subject), set())
            if len(found) == 1:
                dept = next(iter(found))
            else:
                why = f"staff in {_and(sorted(found))} list it" if found else "no staff list it"
                unsaid = f"{subject} — say which department ({why})"
        # a blank Periods is already listed above (a later row's inherits the first's); "1" keeps it from
        # being listed twice
        entry = {"subject": subject, "periods": periods or "1", "taught_as": taught_as, "combine": combine,
                 "room_kind": get("room kind") or None, "dept": dept}
        doubts = []
        # a later row's pattern is read for its form only: its periods are listed beside the first row's
        lessons, problem = parse_pattern(pattern, None if repeat or not periods else _whole(periods))
        if problem:
            errors.append(problem)
        elif lessons:
            entry["lessons"] = lessons
        runs_with = get("runs with")
        banded = (kept[key][1]["taught_as"] if repeat else taught_as) == "banded"     # the kept subject decides
        if runs_with and banded:
            errors.append(f"{subject}: a row is Banded or runs with others, not both")
        elif runs_with:
            entry["runs_with"] = runs_with
        # a count that does not read is ignored and said; the row is kept
        classes, why = _count(get("classes"), codes=True)
        if why == "range":
            doubts.append(f"{subject} at {level}: {label['classes']} {get('classes')} is a range; name each class "
                          f"or give the number, so it is ignored")
        elif why:
            doubts.append(f"{subject} at {level}: {label['classes']} {get('classes')} isn't a whole number from 1 "
                          f"or a list of class codes, so it is ignored")
        groups, why = _count(get("groups"), codes=False)
        if why:
            doubts.append(f"{subject} at {level}: {label['groups']} {get('groups')} isn't a whole number from 1, "
                          f"so it is ignored")
        elif groups and banded:
            groups = None
            doubts.append(f"{subject} at {level}: {label['groups']} is ignored — option blocks decide the sets")
        names = [t.strip() for t in _NAME_SPLIT.split(get("teacher")) if t.strip()]
        if len(names) > 1:
            doubts.append(f"{subject} at {level}, row {n}: only {names[0]} is used — co-teaching comes later")
        teacher = names[0] if names else None
        if teacher or classes or groups:
            entry["deploy"] = [{"teacher": teacher, "classes": classes, "groups": groups}]
        try:        # the store's own validation, its codes mapped to this row (codes only, never a value)
            entry = LV.normalise_levels({level: {"subjects": [entry]}})[SP.level_key(level)]["subjects"][0]
        except ValueError as e:
            plain = taught.strip().lower() == "combined"
            shown = f"{taught} {get('combine')}" if plain else taught
            errors.append({"periods_below_1": f"{label['periods']} {periods} isn't a whole number from 1",
                           "combine_below_2": f"{shown} needs 2 or more classes (Combined 2)"}
                          .get(str(e), f"this row doesn't read ({e})"))
        if errors:
            for text in dict.fromkeys(errors):
                out.add(n, text)
        elif key in kept:
            first, first_entry, first_cells = kept[key]
            if "deploy" not in entry:
                out.add(n, f"{subject} at {level} appears twice (row {first} is kept)")
                continue
            for canon in _disagreements(entry, first_entry, stated):
                out.add(n, f"{subject} at {level}: row {n} says {label.get(canon, _SHOWN[canon])} {stated(canon)}, "
                           f"row {first} says {first_cells[canon] or 'nothing'}; row {first} is kept", dropped=False)
            first_entry.setdefault("deploy", []).extend(entry["deploy"])
            for text in doubts:
                out.add(n, text, dropped=False)
        else:
            cells = {canon: stated(canon) for canon in _COMPARED}
            kept[key] = (n, entry, cells)
            grouped.setdefault(key[0], {"name": level, "subjects": []})["subjects"].append(entry)
            for text in ([unsaid] if unsaid else []) + doubts:
                out.add(n, text, dropped=False)
    return LV.normalise_levels({g["name"]: g for g in grouped.values()}), out.sorted()


# The columns a later row of a level and subject must agree with the first row on, and the stored value
# each is compared by.
_COMPARED = ("periods", "pattern", "taught as", "runs with", "room kind", "department")


def _disagreements(entry: dict, first: dict, get) -> list[str]:
    """The compared columns the row states and the first row's subject says otherwise for, in column
    order. A blank cell states nothing."""
    ways = {"periods": lambda e: e["periods"], "pattern": lambda e: e.get("lessons"),
            "taught as": lambda e: (e["taught_as"], e["combine"]),
            "runs with": lambda e: (e.get("runs_with") or "").casefold(),
            "room kind": lambda e: (e.get("room_kind") or "").casefold(),
            "department": lambda e: (e.get("dept") or "").casefold()}
    return [canon for canon in _COMPARED if get(canon) and ways[canon](entry) != ways[canon](first)]


# ---------------------------------------------------------------------------
# combinations.csv
# ---------------------------------------------------------------------------

def _taught_words(subject: dict) -> str:
    if subject["taught_as"] == "combined":
        return f"Combined {subject['combine']}"
    return subject["taught_as"].capitalize()


def _stored_year_combinations(context, level) -> list[dict]:
    """The combinations stored for the year of `level` as written, the ones a combination without a Code keeps
    its code from (R23): those of a combinations file read before in the same drop
    (`context["year_combinations"]`, by year), else the setup option blocks keeps for that year
    (`blocks.find_setup` on `context["setups"]` with `context["names"]`), so "3E" and "3N" each keep their own.
    Without stored setups in the context, those of the level (`context["combinations"]`, by `level_key`)."""
    context = context or {}
    by_year = context.get("year_combinations") or {}
    if SP.stage_key(level) in by_year:
        found = by_year[SP.stage_key(level)]
    elif context.get("setups") is None:
        found = (context.get("combinations") or {}).get(SP.level_key(level))
    else:
        try:
            found = (BK.find_setup(context["setups"], BK.setup_key_for(level), context.get("names"))[1] or {}
                     ).get("combinations")
        except BK.SetupClash:                       # the card says so; Apply refuses it
            found = None
    return [c for c in found or [] if isinstance(c, dict) and c.get("code")]


def combination_levels(rows, header) -> list[str]:
    """The levels a combinations file writes, each as first written, in file order."""
    cols, _, _ = _columns("combinations", header)
    out: dict[str, str] = {}
    for _, row in rows:
        level = _getter(cols, row)("level")
        if level:
            out.setdefault(SP.stage_key(level), level)
    return list(out.values())


def read_combinations(rows, header, context, file: str | None = None) -> tuple[dict, list[dict]]:
    """{year: [{code, name, subjects, places}]} in file order. `year` is the level as written folded as option
    blocks folds a year key (`specialisation.stage_key`: "Sec 3", "S3", "3" -> "3"; "1st Year" -> "1styear";
    "3E" -> "3e", its own year), so "level:" + it (`blocks.setup_key_for`) is the key option blocks gives
    the classes formed for that level. The level's requirement rows (the levels store) are matched by its
    `level_key`. Code is optional (R23): without one a combination keeps the code of the combination stored
    for its year (`_stored_year_combinations`) with the same subject set (`students.norm` on each subject),
    so a file given again with a row inserted leaves every student's choice on the same subjects; a new set is C<n>, n its place among its year's rows or the next number no
    stored or written code has. A code written in the file is used as it is. Every subject must be a Banded
    requirement row at that level (Calls §8), written as the requirement writes it; two combinations of one
    subject set in a year are a problem; then B2's `check_combinations` rules hold for the year's list."""
    out = _Out(file or FILES["combinations"])
    cols, label, numbered = _columns("combinations", header)
    levels = (context or {}).get("levels") or {}
    in_file: dict[str, set] = {}                    # year -> codes written in the file
    for _, row in rows:
        get = _getter(cols, row)
        if get("level") and get("code"):
            in_file.setdefault(SP.stage_key(get("level")), set()).add(ST.norm(get("code")))
    stored: dict[str, dict] = {}                    # year -> {subject set: its stored code}
    used: dict[str, set] = {}                       # year -> codes taken: written in the file, or stored
    place: dict[str, int] = {}
    found: dict[str, list] = {}
    sets: dict[str, dict] = {}
    for n, row in rows:
        if out.overflows(n, row, header):
            continue
        get = _getter(cols, row)
        level = get("level")
        if not level:
            out.add(n, f"no {label['level']}")
            continue
        key, lkey = SP.stage_key(level), SP.level_key(level)
        if key not in used:
            mine = _stored_year_combinations(context, level)
            stored[key] = {}
            for c in mine:
                stored[key].setdefault(frozenset(ST.norm(x) for x in c.get("subjects") or []), str(c["code"]))
            used[key] = {ST.norm(c["code"]) for c in mine} | in_file.get(key, set())
        place[key] = place.get(key, 0) + 1
        code = get("code")
        rows_of_level = {ST.norm(s["subject"]): s for s in (levels.get(lkey) or {}).get("subjects") or []}
        errors, subjects = [], []
        for i, _ in numbered:
            written = cell(row[i]) if i < len(row) else ""
            if not written:
                continue
            req = rows_of_level.get(ST.norm(written))
            if req is None:
                errors.append(f"{written} has no requirement row at {level}")
            elif req["taught_as"] != "banded":
                errors.append(f"{written} at {level} is taught as {_taught_words(req)}; "
                              f"a combination subject must be Banded")
            else:
                subjects.append(req["subject"])
        places = get("places")
        if not places:
            errors.append(f"no {label.get('places', 'Places')}")
        elif _whole(places) is None or _whole(places) < 0:
            errors.append(f"{label['places']} {places} isn't a whole number from 0")
        if errors:
            for text in errors:
                out.add(n, text)
            continue
        same = frozenset(ST.norm(s) for s in subjects)
        if not code:
            code = stored[key].get(same)
            if code is None or ST.norm(code) in in_file.get(key, set()):
                k = place[key]
                while f"c{k}" in used[key]:
                    k += 1
                code = f"C{k}"
            used[key].add(ST.norm(code))
        if same in sets.setdefault(key, {}):
            first_row, first_code = sets[key][same]
            out.add(n, f"the same subjects as row {first_row} ({first_code}); "
                       f"a student's choice could not tell them apart")
            continue
        combo = {"code": code, "name": get("name"), "subjects": subjects, "places": _whole(places)}
        try:
            checked = BK.check_combinations(found.get(key, []) + [combo])
        except ValueError as e:
            out.add(n, str(e))
            continue
        found[key] = checked
        sets[key][same] = (n, checked[-1]["code"])
    return found, out.sorted()


# ---------------------------------------------------------------------------
# students.csv (a level, no class yet)
# ---------------------------------------------------------------------------

_CHOICE_SPLIT = re.compile(r"[,;]")


def read_level_students(rows, header, context, file: str | None = None) -> tuple[list[dict], list[dict]]:
    """Students as {id, name, level, merit?, choices}: `level` is the school's own name for the level,
    `merit` is kept only when the file has the column (B1's R6), and each choice, written as the subjects
    of an offered combination in any order, is that combination's code. No `class`: classes are formed
    later. A subject the level runs with others (a Runs-with row of the levels in the context, stored or in
    the same drop: Chinese, Malay) named in a choice cell, as the whole cell or one of its comma items
    (`students.norm`), is the student's: it goes into `subjects` (as the requirement writes it, each once;
    the key only when there is one), is taken out of the cell before the rest is matched to a combination,
    and is never a doubt. Problems name the row, the student ID and the column, never a name; a choice's
    words are shown only when they are subjects the plan knows ("?" otherwise). A choice matching no
    combination is listed and left out, the student kept with the other choices in order; a merit problem
    keeps the student with merit None, as B1 does (R11)."""
    out = _Out(file or FILES["level_students"])
    cols, label, numbered = _columns("level_students", header)
    levels = (context or {}).get("levels") or {}
    combinations = (context or {}).get("combinations") or {}
    level_keys = _known_levels(context) | {SP.level_key(k) for k in combinations}
    subject_words = _known_subjects(context) | {ST.norm(s) for combos in combinations.values()
                                                for c in combos for s in c.get("subjects") or []}
    runs_with = {k: {ST.norm(r["subject"]): r["subject"] for r in (entry or {}).get("subjects") or []
                     if r.get("runs_with")} for k, entry in levels.items()}
    seen: set[str] = set()
    read: list[tuple[int, str, dict]] = []          # (row, level key, student) of each row read so far
    ranked: dict[str, list] = {}                     # level key -> merit cells, as students.read_rows keeps them
    for n, row in rows:
        if out.overflows(n, row, header):
            continue
        get = _getter(cols, row)
        sid = get("student id")
        if not sid:
            out.add(n, "no student ID")
            continue
        if sid in seen:
            out.add(n, f"ID {sid} appears twice")
            continue
        seen.add(sid)
        written = get("level")
        key = SP.level_key(written)
        if not written:
            out.add(n, f"no {label['level']}")
            continue
        if key not in level_keys:
            out.add(n, f"the {label['level']} isn't a known level")
            continue
        level = (levels.get(key) or {}).get("name") or written
        student = {"id": sid, "name": get("name"), "level": level}
        if "merit" in cols:
            raw = row[cols["merit"]] if cols["merit"] < len(row) else ""
            student["merit"] = ST._merit(raw)
            ranked.setdefault(key, []).append((n, sid, raw, label["merit"]))
        offered = {frozenset(ST.norm(s) for s in c.get("subjects") or []): c["code"]
                   for c in combinations.get(key) or []}
        choices, subjects = [], []
        running = runs_with.get(key) or {}          # norm(subject) -> the requirement's spelling
        for i, _ in numbered:
            text = cell(row[i]) if i < len(row) else ""
            if not text:
                continue
            whole = running.get(ST.norm(text))
            parts = [] if whole else [" ".join(p.split()) for p in _CHOICE_SPLIT.split(text) if p.strip()]
            mine = [whole] if whole else [running[ST.norm(p)] for p in parts if ST.norm(p) in running]
            subjects += [s for s in mine if s not in subjects]
            parts = [p for p in parts if ST.norm(p) not in running]
            if not parts:                           # only Runs-with subjects: the student's, no choice
                continue
            code = offered.get(frozenset(ST.norm(p) for p in parts))
            if code is not None:
                choices.append(code)
                continue
            shown = ", ".join(p if ST.norm(p) in subject_words else "?" for p in parts)
            out.add(n, f"{cell(header[i])} ({shown}) isn't an offered combination for {level}", dropped=False)
        student["choices"] = choices
        if subjects:
            student["subjects"] = subjects
        read.append((n, key, student))
    for key, cells in ranked.items():
        for p in ST._merit_problems([s for _, k, s in read if k == key], cells):
            out.problems.append({"file": out.file, "row": p["row"], "text": f"{out.file} {p['text']}",
                                 "dropped": False})
    bad = out.dropped()
    return [s for n, _, s in read if n not in bad], out.sorted()
