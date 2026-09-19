"""Deterministic staff-deployment workbook importer (spec docs/superpowers/specs/2026-09-19-curriculum-plan-design.md §4).

Reads a department-per-sheet workbook (one row per teaching requirement), a `Groups` sheet
(divisions and bands) and a `Control`/`Load` sheet (staff), into a curriculum plan document.
No LLM: everything here is deterministic and reports issues instead of raising on bad data.
"""
from __future__ import annotations

import copy
import csv
import hashlib
import io
import re
from datetime import datetime, timezone

from .. import names as names_mod
from . import model as M
from .issues import Issue

_SKIP_SHEETS = {"master", "upload", "overall", "level of prep setup", "cca", "committees"}
_SPECIAL_SHEETS = {"groups", "control", "load"}

_NEEDED_HEADERS = {"subject", "single", "double", "grouping", "class 1"}

_LESSON_COLS = (("single", "1"), ("double", "2"), ("triple", "3"), ("quadruple", "4"))

_PERIODS_SUFFIX = re.compile(r"\s*\((\d+)\s*Periods?\)\s*$", re.IGNORECASE)

_STAFF_NAME_KEYS = ("staff", "staff name")
_STAFF_SIGNAL_KEYS = ("dept", "teaching load factor", "teaching load", "short")


# ---------------------------------------------------------------------------
# small helpers
# ---------------------------------------------------------------------------

def _cell(row, col):
    if col is None or col >= len(row):
        return None
    return row[col]


def _text(row, col) -> str:
    v = _cell(row, col)
    return str(v).strip() if v is not None else ""


def _header_map(row) -> dict[str, int]:
    out: dict[str, int] = {}
    for i, c in enumerate(row or ()):
        if c is not None and str(c).strip():
            out[str(c).strip().lower()] = i
    return out


def _row_hash(row) -> str:
    h = hashlib.sha1()
    h.update("\x1f".join("" if v is None else str(v) for v in row).encode("utf-8"))
    return h.hexdigest()


def _family(code: str) -> str:
    """The letters following the leading digit(s) of a group code, minus any trailing digits
    and its trailing variant character: "1ELP" -> "EL", "1EMP" -> "EM", "1HCR2" -> "HC"."""
    rest = re.sub(r"^\d+", "", code)
    rest = re.sub(r"\d+$", "", rest)
    return rest[:-1] if len(rest) > 1 else rest


def _resolve_staff(name: str, org: dict | None, staff: list[dict]) -> str | None:
    """Match `name` to an existing person (`names.find`), else to an already-known staff
    entry by name, else create a new staff entry with a slug id and no availability of its own
    (`avail: None`), which generation reads as the whole cycle: only the Control sheet says when
    someone works, and a made-up window would be rejected by every grid it does not fit."""
    name = (name or "").strip()
    if not name:
        return None
    if org is not None:
        matches = names_mod.find(org, name, kinds=("person",))
        if matches:
            pid = matches[0]["id"]
            if not any(s["id"] == pid for s in staff):
                staff.append({"id": pid, "name": matches[0]["name"], "short": "", "dept": "",
                              "load_factor": 1.0, "avail": None, "max_periods_day": None, "source_hash": None})
            return pid
    low = name.lower()
    new_id = M.slug(name)
    for s in staff:
        if s["name"].strip().lower() == low or s["id"] == new_id:
            return s["id"]
    staff.append({"id": new_id, "name": name, "short": "", "dept": "", "load_factor": 1.0,
                  "avail": None, "max_periods_day": None, "source_hash": None})
    return new_id


# ---------------------------------------------------------------------------
# is_deployment_workbook
# ---------------------------------------------------------------------------

def is_deployment_workbook(data: bytes) -> bool:
    try:
        wb = openpyxl_load(data)
    except Exception:
        return False
    try:
        for ws in wb.worksheets:
            row = next(ws.iter_rows(min_row=1, max_row=1, values_only=True), None)
            if row is None:
                continue
            cells = {str(c).strip().lower() for c in row if c is not None}
            if _NEEDED_HEADERS <= cells:
                return True
        return False
    finally:
        wb.close()


def openpyxl_load(data: bytes):
    import openpyxl
    return openpyxl.load_workbook(io.BytesIO(data), read_only=True, data_only=True)


# ---------------------------------------------------------------------------
# staff (Control / Load sheet)
# ---------------------------------------------------------------------------

def _read_staff_sheet(ws, org: dict | None) -> list[dict]:
    staff: list[dict] = []
    rows = list(ws.iter_rows(values_only=True))
    header = None
    header_idx = None
    for i, row in enumerate(rows):
        hm = _header_map(row)
        if any(k in hm for k in _STAFF_NAME_KEYS) and any(k in hm for k in _STAFF_SIGNAL_KEYS):
            header, header_idx = hm, i
            break
    if header is None:
        return staff

    name_col = header.get("staff", header.get("staff name"))
    dept_col = header.get("dept")
    load_col = header.get("teaching load factor", header.get("teaching load"))
    short_col = header.get("short")

    for row in rows[header_idx + 1:]:
        name = _text(row, name_col)
        if not name:
            continue
        pid = None
        if org is not None:
            matches = names_mod.find(org, name, kinds=("person",))
            if matches:
                pid = matches[0]["id"]
        if pid is None:
            pid = M.slug(name)
        load_raw = _cell(row, load_col)
        staff.append({
            "id": pid,
            "name": name,
            "short": _text(row, short_col),
            "dept": _text(row, dept_col),
            "load_factor": float(load_raw) if load_raw not in (None, "") else 1.0,
            "avail": None,
            "max_periods_day": None,
            "source_hash": _row_hash(row),
        })
    return staff


# ---------------------------------------------------------------------------
# department sheets (requirements)
# ---------------------------------------------------------------------------

def _read_department_sheet(ws, org, staff, issues: list[Issue]) -> list[dict]:
    rows = list(ws.iter_rows(values_only=True))
    if not rows:
        return []
    header = _header_map(rows[0])
    if "subject" not in header:
        issues.append(Issue("warn", ws.title, f"{ws.title}: no 'Subject' column found, sheet skipped"))
        return []

    dept = ws.title
    subject_col = header["subject"]
    level_col = header.get("subject level", header.get("level"))
    total_col = header.get("total")
    grouping_col = header.get("grouping")
    size_col = header.get("total students")
    class_cols = [header.get(f"class {n}") for n in range(1, 7)]
    teacher_cols = sorted(
        (int(k.split(" ", 1)[1]), v) for k, v in header.items()
        if k.startswith("teacher ") and k.split(" ", 1)[1].strip().isdigit()
    )

    requirements: list[dict] = []
    for row in rows[1:]:
        subject_raw = _text(row, subject_col)
        if not subject_raw:
            continue

        m = _PERIODS_SUFFIX.search(subject_raw)
        if m:
            subject = subject_raw[:m.start()].strip()
            suffix_periods = int(m.group(1))
        else:
            subject = subject_raw
            suffix_periods = None

        total_val = _cell(row, total_col)
        if total_val not in (None, ""):
            periods = int(total_val)
        elif suffix_periods is not None:
            periods = suffix_periods
        else:
            periods = None

        lessons = {}
        for name_key, lkey in _LESSON_COLS:
            v = _cell(row, header.get(name_key))
            lessons[lkey] = int(float(v)) if v not in (None, "") else 0

        classes = [_text(row, c) for c in class_cols if c is not None and _text(row, c)]
        level = _text(row, level_col)
        grouping_raw = _text(row, grouping_col)
        is_entire_class = grouping_raw.strip().lower() == "entire class"

        teachers: list[str] = []
        for _, col in teacher_cols:
            tname = _text(row, col)
            if not tname:
                continue
            tid = _resolve_staff(tname, org, staff)
            if tid and tid not in teachers:
                teachers.append(tid)

        size_val = _cell(row, size_col)
        size = int(size_val) if size_val not in (None, "") else None

        row_hash = _row_hash(row)

        def make_req(req_classes: list[str], grouping: str) -> dict:
            return {
                "id": M.requirement_id(dept, level, grouping, req_classes),
                "dept": dept,
                "level": level,
                "subject": subject,
                "periods": periods,
                "lessons": dict(lessons),
                "grouping": grouping,
                "classes": req_classes,
                "teachers": list(teachers),
                "size": size,
                "venue": {"kind": None, "room": None},
                "sync_with": None,
                "source_hash": row_hash,
            }

        if not classes:
            continue

        if is_entire_class:
            row_reqs = [make_req([cls], "class") for cls in classes]
        else:
            row_reqs = [make_req(classes, grouping_raw)]
        requirements.extend(row_reqs)

        if periods is not None:
            expected = sum(int(k) * v for k, v in lessons.items())
            if periods != expected:
                for req in row_reqs:
                    issues.append(Issue("block", req["id"],
                                         f"{req['id']}: periods {periods} does not match lessons total {expected}"))

    return requirements


# ---------------------------------------------------------------------------
# Groups sheet (divisions and bands)
# ---------------------------------------------------------------------------

_GROUP_NUMBER = re.compile(r"^group\s*\d+$", re.IGNORECASE)
_PLAIN_NUMBER = re.compile(r"^\d+(\.0*)?$")


def _is_group_subheader(row, class_cols, group_cols) -> bool:
    """A second header row of the Groups sheet: it numbers the group columns ("Group 1", "Group 2")
    or the class columns ("1", "2", …), where a division row names classes ("1A1") and group
    codes ("1ELP")."""
    codes = [_text(row, c) for c in group_cols if _text(row, c)]
    if codes and all(_GROUP_NUMBER.match(t) for t in codes):
        return True
    classes = [_text(row, c) for c in class_cols if _text(row, c)]
    return bool(classes) and all(_PLAIN_NUMBER.match(t) for t in classes)


def _read_groups_sheet(ws, requirements: list[dict]) -> tuple[list[dict], list[dict], list[Issue]]:
    divisions: list[dict] = []
    bands: list[dict] = []
    issues: list[Issue] = []

    rows = list(ws.iter_rows(values_only=True))
    if not rows:
        return divisions, bands, issues

    header = rows[0] or ()
    class_start = groups_start = None
    for i, c in enumerate(header):
        if c is None:
            continue
        text = str(c).strip().lower()
        if text.startswith("class") and class_start is None:
            class_start = i
        elif text.startswith("groups") and groups_start is None:
            groups_start = i
    if class_start is None or groups_start is None:
        issues.append(Issue("warn", ws.title, f"{ws.title}: could not find the division/group header row"))
        return divisions, bands, issues

    class_cols = range(class_start, groups_start)
    max_len = max((len(r) for r in rows), default=groups_start)
    group_cols = range(groups_start, max_len)

    by_grouping: dict[str, list[str]] = {}
    for r in requirements:
        by_grouping.setdefault(r["grouping"], []).append(r["id"])

    # The divisions start after the header rows, however many there are: the school's sheet numbers
    # its class columns ("1", "2", …) and its group columns ("Group 1", "Group 2") on a second row,
    # but a sheet with one header row must not lose the division on its first data row.
    start = 1
    while start < len(rows) and _is_group_subheader(rows[start], class_cols, group_cols):
        start += 1

    for row in rows[start:]:
        classes = [_text(row, c) for c in class_cols if _text(row, c)]
        if not classes:
            continue
        codes = [_text(row, c) for c in group_cols if _text(row, c)]

        div_id = M.division_id(classes)
        divisions.append({"id": div_id, "classes": classes})

        families: dict[str, list[str]] = {}
        for code in codes:
            families.setdefault(_family(code), []).append(code)

        for fam, fam_codes in families.items():
            options: list[str] = []
            for code in fam_codes:
                matched = by_grouping.get(code, [])
                if not matched:
                    issues.append(Issue("warn", code, f"group code {code!r} matches no requirement"))
                options.extend(matched)
            if options:
                bands.append({"id": M.band_id(div_id, fam), "division": div_id, "options": options})

    return divisions, bands, issues


# ---------------------------------------------------------------------------
# CSV sizes
# ---------------------------------------------------------------------------

def read_sizes_csv(text: str) -> dict[str, int]:
    out: dict[str, int] = {}
    rows = list(csv.reader(io.StringIO(text)))
    if not rows:
        return out
    header = [c.strip().lower() for c in rows[0]]

    def idx(*names):
        for n in names:
            if n in header:
                return header.index(n)
        return None

    group_col = idx("teaching group")
    class_col = idx("class")
    enrolled_col = idx("enrolled")
    capacity_col = idx("capacity")

    def cell(row, i):
        return row[i].strip() if i is not None and i < len(row) and row[i] is not None else ""

    for row in rows[1:]:
        if not row or not any(c and c.strip() for c in row):
            continue
        value = cell(row, enrolled_col) or cell(row, capacity_col)
        if not value:
            continue
        try:
            size = int(float(value))
        except ValueError:
            continue
        # a CSV may carry both columns (one row naming a teaching group and the class it draws from):
        # take every key the row offers rather than only the first column that exists
        if group_col is not None:
            code = cell(row, group_col)
            key = code.split("-")[-1].strip() if code else ""
            if key:
                out[key] = size
        if class_col is not None:
            code = cell(row, class_col)
            if code:
                out[code] = size
    return out


def apply_sizes(plan: dict, sizes: dict[str, int]) -> dict:
    plan = copy.deepcopy(plan)
    for r in plan.get("requirements", []):
        key = r.get("grouping")
        if key and key != "class" and key in sizes:
            r["size"] = sizes[key]
    for c in plan.get("classes", []):
        code = c.get("code")
        if code in sizes:
            c["size"] = sizes[code]
    return M.normalise(plan)


# ---------------------------------------------------------------------------
# read_workbook
# ---------------------------------------------------------------------------

def read_workbook(data: bytes, org: dict | None, existing: dict | None, filename: str = "") -> tuple[dict, list[Issue]]:
    issues: list[Issue] = []
    try:
        wb = openpyxl_load(data)
    except Exception as e:
        issues.append(Issue("block", "workbook", f"could not read workbook: {e}"))
        base = existing if existing is not None else M.empty_plan()
        return M.normalise(base), issues

    try:
        control_ws = next((ws for ws in wb.worksheets if ws.title.strip().lower() == "control"), None)
        if control_ws is None:
            control_ws = next((ws for ws in wb.worksheets if ws.title.strip().lower() == "load"), None)
        staff = _read_staff_sheet(control_ws, org) if control_ws is not None else []

        requirements: list[dict] = []
        for ws in wb.worksheets:
            title_l = ws.title.strip().lower()
            if title_l in _SKIP_SHEETS or title_l.startswith("sheet") or title_l in _SPECIAL_SHEETS:
                continue
            requirements.extend(_read_department_sheet(ws, org, staff, issues))

        groups_ws = next((ws for ws in wb.worksheets if ws.title.strip().lower() == "groups"), None)
        if groups_ws is not None:
            divisions, bands, group_issues = _read_groups_sheet(groups_ws, requirements)
            issues.extend(group_issues)
        else:
            divisions, bands = [], []

        class_codes = sorted({c for r in requirements for c in r["classes"]})
        classes = [{"code": c, "level": "", "size": None} for c in class_codes]

        plan = {
            "version": M.PLAN_VERSION,
            "staff": staff,
            "classes": classes,
            "divisions": divisions,
            "requirements": requirements,
            "bands": bands,
            "rules": {"edge_subjects": [], "no_double_across_rest": True, "pinned": []},
            "source": {"file": filename, "imported": datetime.now(timezone.utc).isoformat()},
        }
        new_plan = M.normalise(plan)
    finally:
        wb.close()

    if existing is not None:
        return M.merge_import(existing, new_plan), issues
    return new_plan, issues
