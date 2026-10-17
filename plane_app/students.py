"""Students and their choices, the pure core (spec docs/superpowers/specs/2026-10-16-students-design.md §2).

A student list is read against the curriculum plan: each student's class and the option groups they
take (a division plus an option code, as the board names them). From those come the choice patterns
the solver keeps apart, the real group sizes, the problems an admin should check, and the slots where
one student's lessons overlap.

Pure: no database, no I/O. Problem texts carry IDs, row numbers, class and subject codes only, never
a student's name.
"""
from __future__ import annotations

import hashlib
import json
import re
import threading

from .plan.generate import class_group_id, option_division, option_group_id, option_id


# the one example row of the start wizard's Students sheet: a sheet holding nothing else is read as empty
EXAMPLE_ID = "S0001"
EXAMPLE_NAME = "Student 001"


def norm(s) -> str:
    """Casefold with all whitespace removed: "3sci a" and "3SCIA" match, as do "Physics" and "physics"."""
    return "".join(str(s or "").split()).casefold()


def _id_key(student_id: str) -> tuple:
    """Student-ID order with numbers compared as numbers (S2 before S10); the ID itself breaks a tie
    (S01 and S1), so the order never depends on the input's."""
    parts = [(0, int(p), "") if p.isdigit() else (1, 0, p) for p in re.split(r"(\d+)", student_id) if p]
    return (parts, student_id)


def _natural(text) -> tuple:
    """`_id_key` for a class or a level, case and spaces aside: "9A" before "10A"."""
    return _id_key(norm(text))


# ---------------------------------------------------------------------------
# the plan's option groups
# ---------------------------------------------------------------------------

def option_groups(plan: dict) -> dict[str, dict]:
    """Each option group, by the id generate gives it: {id, division, grouping, subject, classes,
    requirements, band}. A group taught in parts (a split) lists every part's requirement; its band
    is the first part's, as in generate."""
    band_of_req = {option: b["id"] for b in plan["bands"] for option in b["options"]}
    out: dict[str, dict] = {}
    for r in plan["requirements"]:
        if r["grouping"] == "class":
            continue
        gid = option_group_id(plan, r)
        group = out.get(gid)
        if group is None:
            out[gid] = {"id": gid, "division": option_division(plan, r), "grouping": r["grouping"],
                        "subject": r["subject"], "classes": list(r["classes"]),
                        "requirements": [r["id"]], "band": band_of_req.get(r["id"])}
        elif r["id"] not in group["requirements"]:
            group["requirements"].append(r["id"])
    return out


def group_id(take: dict) -> str:
    """The option group id a take ({division, grouping}) names."""
    return option_id(take["grouping"], take["division"])


def _index(plan: dict, groups: dict[str, dict]) -> dict[str, dict]:
    """Per class code: its option groups by normalised code and by normalised subject (each list in
    grouping order), and the normalised subjects it takes as a whole class."""
    index = {c["code"]: {"codes": {}, "subjects": {}, "whole": set()} for c in plan["classes"]}
    for g in sorted(groups.values(), key=lambda g: (g["grouping"], g["id"])):
        for code in g["classes"]:
            if code in index:
                index[code]["codes"].setdefault(norm(g["grouping"]), []).append(g["id"])
                index[code]["subjects"].setdefault(norm(g["subject"]), []).append(g["id"])
    for r in plan["requirements"]:
        if r["grouping"] == "class":
            for code in r["classes"]:
                if code in index:
                    index[code]["whole"].add(norm(r["subject"]))
    return index


def _take(student: dict, group: dict, by_app: bool = False) -> None:
    take = {"division": group["division"], "grouping": group["grouping"]}
    if take not in student["takes"]:
        student["takes"].append(take)
        if by_app:
            student["placed_by_app"].append(group["grouping"])


def _same_band(student: dict, groups: dict[str, dict]) -> list[dict]:
    first_in: dict[str, str] = {}
    out = []
    for t in student["takes"]:
        g = groups.get(group_id(t))
        if not g or not g["band"]:
            continue
        first = first_in.setdefault(g["band"], t["grouping"])
        if first != t["grouping"]:
            out.append({"kind": "same_band", "student": student["id"],
                        "text": f"{student['id']}: {first} and {t['grouping']} are in the same band and run together"})
    return out


# ---------------------------------------------------------------------------
# reading a list
# ---------------------------------------------------------------------------

def _merit(cell) -> int | None:
    """A merit cell as a whole number from 1, else None (blank, 0, negative, a fraction or text)."""
    if isinstance(cell, bool):
        return None
    if isinstance(cell, float) and cell.is_integer():
        cell = int(cell)
    text = str(cell if cell is not None else "").strip()
    return int(text) if text.isascii() and text.isdigit() and int(text) >= 1 else None


def read_rows(rows: list[dict], plan: dict) -> tuple[list[dict], list[dict]]:
    """Read rows {id, name, class, subjects: [str], columns?: [str], row?: int} (row numbers 1-based by
    position, or each row's own `row`: the file's row number) into (students, problems). `columns` names each subject cell's column as the file's header has it
    (default "subject 1", "subject 2", ...).

    A problem's `text` never echoes a cell the plan does not know (a name typed in the wrong column
    would otherwise travel on to the AI): it names the row and the column, and the raw cell goes in
    `detail`, for the admin page only.

    A row may also carry `merit` (the merit-rank cell: 1 chooses first) and `choices` (the ranked
    combination cells, in order), only when the file has those columns; the student then carries
    `merit` (a whole number from 1, or None for a blank or invalid cell) and `choices` (the stripped,
    non-blank cells). Problems for merit name the row and the student's ID, never a name or a cell:
    `merit_invalid` (the cell goes in `detail`), `merit_missing` (a blank cell where other students
    have a rank) and `merit_duplicate`. A choice is not a subject: it is never read as a take, and
    whether it names a combination is checked later against the combinations.

    A subject cell matches, case- and space-insensitively, an option code of the student's class
    first, then a subject. A subject taught in several sets is deferred: once every row is read,
    the students who named only it are spread round-robin across its sets (in grouping order) in
    student-ID order and marked `placed_by_app`. A whole-class subject of the class is ignored (every
    student takes it). A repeated ID keeps the first row; a student whose class is unknown is kept,
    without takes."""
    groups = option_groups(plan)
    index = _index(plan, groups)
    class_by_norm: dict[str, str] = {}
    for c in plan["classes"]:
        class_by_norm.setdefault(norm(c["code"]), c["code"])

    students: list[dict] = []
    problems: list[dict] = []
    seen: set[str] = set()
    deferred: dict[tuple[str, ...], list[dict]] = {}
    ranked: list[tuple[int, str, object, str]] = []   # (row, id, raw merit cell, column) of each kept student, when the file has a Merit column
    for n, row in enumerate(rows, 1):
        n = row.get("row") or n             # the file's own row number, when the reader knows it
        sid = str(row.get("id") or "").strip()
        if not sid:
            problems.append({"kind": "missing_id", "row": n, "text": f"row {n}: no student ID"})
            continue
        if sid in seen:
            problems.append({"kind": "duplicate_id", "row": n, "text": f"row {n}: ID {sid} appears twice"})
            continue
        seen.add(sid)
        raw_class = str(row.get("class") or "").strip()
        code = raw_class if raw_class in index else class_by_norm.get(norm(raw_class))
        student = {"id": sid, "name": str(row.get("name") or "").strip(), "class": code or raw_class,
                   "takes": [], "placed_by_app": []}
        if "merit" in row:
            student["merit"] = _merit(row["merit"])
            ranked.append((n, sid, row["merit"], str(row.get("merit_column") or "").strip() or "merit"))
        if "choices" in row:
            student["choices"] = [c for c in (str(x if x is not None else "").strip()
                                              for x in row["choices"] or []) if c]
        students.append(student)
        if code is None:
            problems.append({"kind": "unknown_class", "row": n, "text": f"row {n}: the class isn't known",
                             "detail": raw_class})
            continue
        ix = index[code]
        columns = row.get("columns") or []
        for j, cell in enumerate(row.get("subjects") or []):
            key = norm(cell)
            if not key:
                continue
            if key in ix["codes"]:
                _take(student, groups[ix["codes"][key][0]])
                continue
            gids = ix["subjects"].get(key)
            if gids is None:
                if key not in ix["whole"]:
                    column = str(columns[j]).strip() if j < len(columns) and str(columns[j] or "").strip() \
                        else f"subject {j + 1}"
                    problems.append({"kind": "not_offered", "row": n,
                                     "text": f"row {n}: {column} isn't offered to {code}",
                                     "detail": str(cell).strip()})
            elif len(gids) == 1:
                _take(student, groups[gids[0]])
            else:
                deferred.setdefault(tuple(gids), []).append(student)

    for gids, waiting in deferred.items():
        i = 0
        for student in sorted(waiting, key=lambda s: _id_key(s["id"])):
            if any(group_id(t) in gids for t in student["takes"]):
                continue                    # already in one of these sets (by code, or named twice)
            _take(student, groups[gids[i % len(gids)]], by_app=True)
            i += 1

    for student in students:
        problems += _same_band(student, groups)
    problems += _merit_problems(students, ranked)
    return students, problems


def _merit_problems(students: list[dict], ranked: list[tuple[int, str, object, str]]) -> list[dict]:
    """The problems of a Merit column, by row: an invalid cell, a blank where others have a rank, a rank
    two students share. Row numbers and IDs only; an invalid cell goes in `detail`."""
    out: list[dict] = []
    merit = {s["id"]: s["merit"] for s in students if "merit" in s}
    anyone = any(m is not None for m in merit.values())
    first_with: dict[int, str] = {}
    for n, sid, cell, column in ranked:
        m = merit[sid]
        if m is None:
            if str(cell if cell is not None else "").strip():
                out.append({"kind": "merit_invalid", "row": n,
                            "text": f"row {n}: {column} isn't a whole number from 1", "detail": str(cell).strip()})
            elif anyone:
                out.append({"kind": "merit_missing", "row": n, "text": f"row {n}: {sid} has no merit rank"})
        elif m in first_with:
            out.append({"kind": "merit_duplicate", "row": n,
                        "text": f"row {n}: {sid} has the same merit rank as {first_with[m]}"})
        else:
            first_with[m] = sid
    return out


# ---------------------------------------------------------------------------
# what the solver and the checks get
# ---------------------------------------------------------------------------

def patterns(students: list[dict], plan: dict) -> dict[str, list[str]]:
    """`pattern-<hash>` -> the sorted option group ids, for each distinct set of two or more groups
    the plan still has that some student takes. Ids are stable for the same set; keys are sorted.

    A band's options start together, so a pattern holding two of one band could never be placed: only the
    first take of each band (in the student's takes order) goes in; the others are a `same_band` problem."""
    groups = option_groups(plan)
    out: dict[str, list[str]] = {}
    for s in students:
        kept: set[str] = set()
        bands: set[str] = set()
        for t in s["takes"]:
            g = groups.get(group_id(t))
            if g is None or (g["band"] and g["band"] in bands):
                continue
            if g["band"]:
                bands.add(g["band"])
            kept.add(g["id"])
        gids = sorted(kept)
        if len(gids) >= 2:
            out.setdefault("pattern-" + hashlib.sha1(",".join(gids).encode()).hexdigest()[:8], gids)
    return dict(sorted(out.items()))


def sizes(students: list[dict], plan: dict) -> dict[str, int]:
    """Option group id -> how many students take it, for the plan's groups that have any."""
    known = option_groups(plan).keys()
    out: dict[str, int] = {}
    for s in students:
        for gid in {group_id(t) for t in s["takes"]} & known:
            out[gid] = out.get(gid, 0) + 1
    return out


def _not_offered(student: dict, groups: dict[str, dict]) -> list[dict]:
    """A take whose group is still in the plan but no longer lists the student's class."""
    return [{"kind": "not_offered", "student": student["id"],
             "text": f"{student['id']}: {t['grouping']} isn't offered to {student['class']}"}
            for t in student["takes"]
            if group_id(t) in groups and student["class"] not in groups[group_id(t)]["classes"]]


def problems(students: list[dict], plan: dict, org: dict | None = None) -> list[dict]:
    """What to check in a stored list against the plan as it is now: a repeated ID, a class the plan
    no longer has, a take whose group is gone (`missing_group`) or no longer offered to the student's
    class (`not_offered`), two takes in one band, and, given a
    placed organisation, a group with more students than the largest room its lessons use."""
    groups = option_groups(plan)
    codes = {c["code"] for c in plan["classes"]}
    out: list[dict] = []
    seen: set[str] = set()
    for s in students:
        sid = s["id"]
        if sid in seen:
            out.append({"kind": "duplicate_id", "student": sid, "text": f"ID {sid} appears twice"})
        seen.add(sid)
        if s["class"] not in codes:
            out.append({"kind": "unknown_class", "student": sid, "text": f"{sid}: the class isn't known",
                        "detail": s["class"]})
        for t in s["takes"]:
            if group_id(t) not in groups:
                out.append({"kind": "missing_group", "student": sid,
                            "text": f"{sid}: {t['grouping']} ({t['division']}) is no longer in the plan"})
        if s["class"] in codes:             # an unknown class is already one problem
            out += _not_offered(s, groups)
        out += _same_band(s, groups)
    if org is not None:
        out += _over_room(sizes(students, plan), groups, org)
    return out


def summary(students: list[dict], plan: dict, found: list[dict]) -> dict:
    """{count, patterns, to_check} for a stored list and its `problems`: to_check is the students with
    a problem plus the groups too big for their room. Counts only."""
    return {"count": len(students), "patterns": len(patterns(students, plan)),
            "to_check": len({p["student"] for p in found if "student" in p})
            + sum(1 for p in found if p["kind"] == "over_room")}


def _over_room(counts: dict[str, int], groups: dict[str, dict], org: dict) -> list[dict]:
    locs = {l["id"]: l for l in org.get("locations", []) if not l.get("rest")}
    rooms_of: dict[str, list[dict]] = {}
    for e in org.get("events", []):
        if e.get("t0") is None or e.get("loc") not in locs:
            continue
        for m in e["members"]:
            if m in counts:
                rooms_of.setdefault(m, []).append(locs[e["loc"]])
    out = []
    for gid, n in counts.items():
        if gid not in rooms_of:
            continue
        room = max(rooms_of[gid], key=lambda l: l.get("cap") or 0)
        cap = room.get("cap") or 0
        if n > cap:
            out.append({"kind": "over_room", "group": gid,
                        "text": f"{groups[gid]['grouping']}: {n} students, room {room.get('name') or room['id']} holds {cap}"})
    return out


def ordered(students: list[dict], plan: dict | None = None) -> list[dict]:
    """Students by level (given the plan), then class, then ID, numbers in each compared as numbers ("9A"
    before "10A", S2 before S10): the order of a printed pile."""
    level = (lambda c: _natural(level_of(plan, c))) if plan is not None else (lambda c: ())
    return sorted(students, key=lambda s: (level(s["class"]), _natural(s["class"]), _id_key(s["id"])))


def level_of(plan: dict, class_code: str) -> str:
    """A class's level: the plan's own, else the digits its code starts with ("3A" -> "3"), else "".
    For choosing a level's students to print."""
    c = next((c for c in plan.get("classes", []) if c["code"] == class_code), None)
    if c and str(c.get("level") or "").strip():
        return str(c["level"]).strip()
    m = re.match(r"\d+", str(class_code).strip())
    return m.group(0) if m else ""


# ---------------------------------------------------------------------------
# a student's lessons and clashes
# ---------------------------------------------------------------------------

def _own_groups(student: dict) -> set[str]:
    return {group_id(t) for t in student["takes"]}


def student_events(student: dict, org: dict) -> list[str]:
    """The ids of the events a student attends, in the organisation's order: their class's whole-class
    lessons and their own option groups' lessons, never another option of the same band."""
    own = _own_groups(student)
    mine = own | {class_group_id(student["class"])}
    org_groups = {g["id"]: g for g in org.get("groups", [])}
    bands = {org_groups[g].get("band") for g in own if g in org_groups} - {None}
    others = {gid for gid, g in org_groups.items()
              if g.get("option") and gid not in own and g.get("band") in bands}
    return [e["id"] for e in org.get("events", [])
            if mine.intersection(e["members"]) and not others.intersection(e["members"])]


def _student_clashes(student: dict, org: dict, events: dict[str, dict]) -> set[tuple[int, tuple[str, ...]]]:
    mine = _own_groups(student) | {class_group_id(student["class"])}
    placed = [events[i] for i in student_events(student, org) if events[i].get("t0") is not None]
    found = set()
    for i, a in enumerate(placed):
        a0, a1 = a["t0"], a["t0"] + (a.get("dur") or 1)
        ga = mine.intersection(a["members"])
        for b in placed[i + 1:]:
            b0, b1 = b["t0"], b["t0"] + (b.get("dur") or 1)
            if a0 < b1 and b0 < a1:
                gb = mine.intersection(b["members"])
                if ga != gb:                # one group's own lessons overlapping is not a student clash
                    found.add((max(a0, b0), tuple(sorted(ga | gb))))
    return found


def clashes(students: list[dict], org: dict, plan: dict) -> list[dict]:
    """Every slot where one student's placed lessons overlap: {t, label, groups, students}, one entry
    per (first overlapping slot, groups involved), with the student ids in ID order. `plan` is part of
    the interface the boundary calls with; the organisation alone decides what overlaps."""
    events = {e["id"]: e for e in org.get("events", [])}
    labels = org.get("time_labels") or []
    by_choice: dict[tuple, set] = {}
    found: dict[tuple[int, tuple[str, ...]], list[str]] = {}
    for s in students:
        key = (s["class"], frozenset(_own_groups(s)))
        if key not in by_choice:
            by_choice[key] = _student_clashes(s, org, events)
        for clash in by_choice[key]:
            found.setdefault(clash, []).append(s["id"])
    return [{"t": t, "label": labels[t] if t < len(labels) else str(t), "groups": list(groups),
             "students": sorted(ids, key=_id_key)}
            for (t, groups), ids in sorted(found.items())]


def clash_report(students: list[dict], org: dict, plan: dict, names: bool = False) -> dict:
    """The student clashes for display: {"clashes": [{label, groups: [display names], count}], "students": n}
    where n counts DISTINCT students (three groups overlapping at once give three entries, one student).
    The slot's time label comes from the organisation. A group shows as "Physics 3SCIA" (subject and option
    code), a whole class as its code. No student id or name unless `names`, which adds each clash's
    `students` as [{id, name}] (admin only)."""
    option = option_groups(plan)
    classes = {class_group_id(c["code"]): c["code"] for c in plan["classes"]}
    by_id = {s["id"]: s for s in students}

    def display(gid: str) -> str:
        g = option.get(gid)
        return f"{g['subject']} {g['grouping']}" if g else classes.get(gid, gid)

    out, seen = [], set()
    for c in clashes(students, org, plan):
        entry = {"label": c["label"], "groups": sorted(display(g) for g in c["groups"]), "count": len(c["students"])}
        if names:
            entry["students"] = [{"id": i, "name": by_id[i]["name"]} for i in c["students"]]
        seen.update(c["students"])
        out.append(entry)
    return {"clashes": out, "students": len(seen)}


_REPORTS: dict[str, dict] = {}
_REPORTS_MAX = 4
_REPORTS_LOCK = threading.Lock()        # lookups, inserts and evictions; the report itself is worked out outside it


def cached_clash_report(students: list[dict], org: dict, plan: dict) -> dict:
    """`clash_report` (counts only), kept for the last few (students, organisation, plan) triples so repeated
    reads of /api/solid reuse it. The key is a hash of the data itself, so any edit recomputes. Safe across
    threads: two requests missing at once each work it out, and the cache never breaks."""
    key = hashlib.sha1(json.dumps([students, org, plan], sort_keys=True, default=str).encode()).hexdigest()
    with _REPORTS_LOCK:
        found = _REPORTS.get(key)
    if found is None:
        found = clash_report(students, org, plan)
        with _REPORTS_LOCK:
            while len(_REPORTS) >= _REPORTS_MAX:
                _REPORTS.pop(next(iter(_REPORTS)))
            _REPORTS[key] = found
    return found
