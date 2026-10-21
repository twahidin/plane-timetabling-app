"""Option blocks from ranked combinations (B2). Pure functions, no database or web imports:
`checks` reports what stops or weakens a placement, `place` seats each student in a combination by
merit and ranked choice, `split` turns the engine's counts into each student's sets, `suggest_teachers`
proposes a teacher per set, and `apply_plan` / `student_takes` write the sets and bands into the plan and
the students' takes. Everything it says carries codes, ranks and IDs only; the page adds names, and an
error raised here carries counts and codes only."""
from __future__ import annotations

import copy
import hashlib
import json
import re
import threading

from . import levels as LV
from .plan import model as M
from .plan import specialisation as SP
from .plan.generate import option_division, option_group_id
from .students import _id_key, group_id as _group_id, level_of, norm


# ---------------------------------------------------------------------------
# a year group's combinations: the one validation, used by PUT /api/blocks/setup and by the workbook's
# Combinations sheet (a ValueError carries the words the route answers a 400 with)
# ---------------------------------------------------------------------------

MAX_BLOCKS, MAX_COMBINATIONS, MAX_SUBJECTS, MAX_CODE = 20, 200, 100, 40      # the engine's own caps
MAX_TEACHERS = 200                                    # the engine's cap on a subject's or department's teachers
DEFAULT_LIMITS = {"blocks": 4, "min_size": 10, "max_size": 30}
RESERVED = re.compile(r"c\d+", re.IGNORECASE)      # matched whole: the form of engine_request's ids
EXAMPLE_CODE = "C1"                                   # the start wizard's example row of the Combinations sheet:
EXAMPLE_NAME = "Example combination"                  # a sheet holding nothing else is read as empty
# blocks_setup read-modify-write (the routes' and the workbook reading's Apply); taken after the plan lock
SETUP_LOCK = threading.Lock()


def check_int(value, name: str, lo: int, hi: int | None = None) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < lo or (hi is not None and value > hi):
        top = f" to {hi}" if hi is not None else " or more"
        raise ValueError(f"{name} must be a whole number from {lo}{top}")
    return value


def check_text(value, name: str, required: bool = False) -> str:
    if value is None:
        value = ""
    if not isinstance(value, str):
        raise ValueError(f"{name} must be text")
    value = value.strip()
    if required and not value:
        raise ValueError(f"give {name}")
    if len(value) > 200:
        raise ValueError(f"{name} is too long")
    return value


def check_combinations(raw) -> list[dict]:
    """A year group's combinations, validated and tidied: [{code, name, subjects, places}]. ValueError
    (codes and counts only) when the list or any combination in it does not hold."""
    if not isinstance(raw, list) or len(raw) > MAX_COMBINATIONS:
        raise ValueError(f"combinations must be a list of at most {MAX_COMBINATIONS}")
    out, seen = [], set()
    for c in raw:
        if not isinstance(c, dict):
            raise ValueError("each combination needs a code, name, subjects and places")
        code = check_text(c.get("code"), "each combination a code", required=True)
        if len(code) > MAX_CODE:
            raise ValueError(f"the combination code {code[:MAX_CODE]}... is too long")
        if norm(code) in seen:
            raise ValueError(f"the combination code {code} is used twice")
        seen.add(norm(code))
        subjects = c.get("subjects")
        if not isinstance(subjects, list) or not all(isinstance(s, str) for s in subjects):
            raise ValueError(f"{code}: subjects must be a list of subject codes")
        subjects = list(dict.fromkeys(s.strip() for s in subjects if s.strip()))
        if len(subjects) < 2:
            raise ValueError(f"{code} needs 2 or more subjects")
        if any(len(s) > MAX_CODE for s in subjects):
            raise ValueError(f"{code}: a subject code is too long")
        reserved = next((s for s in subjects if RESERVED.fullmatch(s)), None)
        if reserved:                            # the engine's opaque combination ids look like this
            raise ValueError(f"{code}: the subject code {reserved} is reserved; choose another")
        out.append({"code": code, "name": check_text(c.get("name"), f"{code}'s name"), "subjects": subjects,
                    "places": check_int(c.get("places"), f"{code}'s places", 0)})
    if len({s for c in out for s in c["subjects"]}) > MAX_SUBJECTS:
        raise ValueError(f"the combinations name more than {MAX_SUBJECTS} subjects")
    return out


def _ordinal(n: int) -> str:
    suffix = "th" if 10 <= n % 100 <= 20 else {1: "st", 2: "nd", 3: "rd"}.get(n % 10, "th")
    return f"{n}{suffix}"


def _merit(student: dict):
    return student.get("merit")


def _choices(student: dict) -> list:
    return student.get("choices") or []


def checks(combinations: list[dict], students: list[dict], limits: dict) -> list[dict]:
    """What to look at before placing: [{"kind", "text", "blocking"}]. Only `over_places` is a warning."""
    found: list[dict] = []

    def add(kind: str, text: str, blocking: bool = True) -> None:
        found.append({"kind": kind, "text": text, "blocking": blocking})

    blocks = (limits or {}).get("blocks")
    if blocks is not None:
        for c in combinations:
            n = len(c.get("subjects") or [])
            if n > blocks:
                add("too_many_subjects", f"{c['code']} has {n} subjects but there are {blocks} blocks")

    known = {norm(c["code"]) for c in combinations}
    ordered = sorted(students, key=lambda s: _id_key(s["id"]))
    for s in ordered:
        reported: set[str] = set()
        for code in _choices(s):
            key = norm(code)
            if key and key not in known and key not in reported:
                reported.add(key)
                add("unknown_code", f"{s['id']}: choice {str(code).strip()} isn't a combination")

    for s in ordered:
        if _merit(s) is None:
            add("merit_missing", f"{s['id']} has no merit rank")
    by_merit: dict = {}
    for s in ordered:
        if _merit(s) is not None:
            by_merit.setdefault(_merit(s), []).append(s["id"])
    for merit in sorted(m for m, ids in by_merit.items() if len(ids) > 1):
        add("merit_duplicate", f"Merit {merit} is shared by {', '.join(by_merit[merit])}")

    places = sum(max(int(c.get("places") or 0), 0) for c in combinations)
    if len(students) > places:
        add("over_places", f"{len(students)} students, {places} places: "
                           f"{len(students) - places} will not be placed", blocking=False)
    return found


def place(students: list[dict], combinations: list[dict], overrides: dict[str, str] | None = None) -> dict:
    """Seat every student: {"placed": {sid: code}, "unplaced": [sid], "lines": {sid: str}}.

    Overrides (sid -> combination code) go first, always honoured and counted against the places, even
    beyond them. The rest go in merit order (no merit last; ties and no-merit by natural ID), each into
    the highest-ranked choice that still has room. Unknown or repeated choices are skipped, and "Nth
    choice" counts among the choices that remain."""
    by_key = {norm(c["code"]): c["code"] for c in combinations}
    room = {c["code"]: max(int(c.get("places") or 0), 0) for c in combinations}
    overrides = overrides or {}

    unique: dict[str, dict] = {}
    for s in students:
        unique.setdefault(s["id"], s)
    order = sorted(unique.values(), key=lambda s: (_merit(s) is None,
                                                   _merit(s) if _merit(s) is not None else 0,
                                                   _id_key(s["id"])))
    placed: dict[str, str] = {}
    lines: dict[str, str] = {}

    def lead(s: dict) -> list[str]:
        return [_ordinal(_merit(s))] if _merit(s) is not None else []

    for s in order:
        code = by_key.get(norm(overrides.get(s["id"])))
        if code is not None:
            room[code] -= 1
            placed[s["id"]] = code
            lines[s["id"]] = " · ".join(lead(s) + [f"overridden: {code}"])

    unplaced: list[str] = []
    for s in order:
        if s["id"] in placed:
            continue
        ranked: list[str] = []
        for raw in _choices(s):
            code = by_key.get(norm(raw))
            if code is not None and code not in ranked:
                ranked.append(code)
        parts = lead(s)
        for rank, code in enumerate(ranked, 1):
            if room[code] > 0:
                room[code] -= 1
                placed[s["id"]] = code
                parts.append(f"placed in {code} ({_ordinal(rank)} choice)")
                break
            parts.append(f"{_ordinal(rank)} choice {code} full")
        else:
            unplaced.append(s["id"])
            parts.append("no choice had room" if ranked else "no choices given")
        lines[s["id"]] = " · ".join(parts)
    return {"placed": placed, "unplaced": unplaced, "lines": lines}


# ---------------------------------------------------------------------------
# from the engine's counts to students
# ---------------------------------------------------------------------------

def _matching(rows: list, cols: list, w: dict) -> dict | None:
    """A perfect matching row -> col on the support of `w` (augmenting paths, rows and columns tried in
    their given order, so it is deterministic), or None when there is none."""
    owner: dict = {}

    def augment(r, seen: set) -> bool:
        for c in cols:
            if w[r][c] > 0 and c not in seen:
                seen.add(c)
                if c not in owner or augment(owner[c], seen):
                    owner[c] = r
                    return True
        return False

    for r in rows:
        if not augment(r, set()):
            return None
    return {r: c for c, r in owner.items()}


def _decompose(subjects: list[str], blocks: list[int], m: dict, n: int) -> list[dict[str, int]]:
    """Split a combination's subject × block counts (each subject's row sums to n, each block's column to
    at most n) into n maps subject -> block with distinct blocks. Dummy rows take up each column's slack, so
    the graph is n-regular and splits into n perfect matchings (König); each, less its dummies, is one
    student's blocks. A matching found k times over is taken k times at once."""
    slack = {b: n - sum(m[s][b] for s in subjects) for b in blocks}
    dummies = [("dummy", i) for i in range(len(blocks) - len(subjects))]
    w = {s: dict(m[s]) for s in subjects}
    for d in dummies:
        w[d], need = {b: 0 for b in blocks}, n
        for b in blocks:
            take = min(need, slack[b])
            w[d][b], slack[b], need = take, slack[b] - take, need - take
    rows = list(subjects) + dummies
    if not rows:
        return [{} for _ in range(n)]
    out: list[dict[str, int]] = []
    while len(out) < n:
        match = _matching(rows, blocks, w)
        if match is None:
            raise ValueError("the counts of a combination do not split into students")
        times = min(w[r][c] for r, c in match.items())
        for r, c in match.items():
            w[r][c] -= times
        out += [{s: match[s] for s in subjects}] * times
    return out


def split(placed: dict[str, str], combinations: list[dict], answer: dict) -> dict[str, list[dict]]:
    """Each placed student's sets: {sid: [{"subject", "block", "set"}]}, one entry per subject of their
    combination, in the combination's subject order.

    Per combination, the engine's counts per subject × block are split into one subject -> block map per
    student (`_decompose`), dealt to its students in natural ID order. Within each (subject, block) the
    students who got it fill that block's sets of the subject by the engine's per-set counts, again in
    natural ID order, so every set's size is the engine's. Raises ValueError (counts and codes only) when
    the answer does not fit the students, or when the result would break the invariant: each student takes
    each subject once, never two in one block, and every set holds exactly its size."""
    subjects_of = {c["code"]: list(dict.fromkeys(c.get("subjects") or [])) for c in combinations}
    block_of: dict[tuple[str, int], int] = {}
    size_of: dict[tuple[str, int], int] = {}
    for st in answer.get("sets") or []:
        key = (st["subject"], int(st["set"]))
        if key in block_of:
            raise ValueError(f"set {key[1]} of {key[0]} appears twice in the answer")
        block_of[key], size_of[key] = int(st["block"]), int(st["size"])

    # combination -> (subject, block) -> set -> count
    flows: dict[str, dict[tuple[str, int], dict[int, int]]] = {}
    for f in answer.get("flows") or []:
        count, key = int(f["count"]), (f["subject"], int(f["set"]))
        if count < 0:
            raise ValueError(f"a flow of {f['combination']} into {key[0]} has a negative count")
        if not count:
            continue
        if key not in block_of:
            raise ValueError(f"a flow of {f['combination']} names set {key[1]} of {key[0]}, which is not in the answer")
        if block_of[key] != int(f["block"]):
            raise ValueError(f"a flow of {f['combination']} puts set {key[1]} of {key[0]} in another block")
        cell = flows.setdefault(f["combination"], {}).setdefault((key[0], block_of[key]), {})
        cell[key[1]] = cell.get(key[1], 0) + count

    members: dict[str, list[str]] = {}
    for sid, code in placed.items():
        members.setdefault(code, []).append(sid)

    out: dict[str, list[dict]] = {}
    for code in sorted(set(members) | set(flows)):
        sids = sorted(members.get(code, []), key=_id_key)
        n, cells = len(sids), flows.get(code, {})
        if code not in subjects_of:
            raise ValueError(f"{n} students are placed in {code}, which is not a combination")
        subjects = subjects_of[code]
        stray = sorted({s for s, _ in cells} - set(subjects))
        if stray:
            raise ValueError(f"the flows send {code} into {', '.join(stray)}, which it does not take")
        if not n:
            if cells:
                raise ValueError(f"the flows place students in {code}, which has none")
            continue
        blocks = sorted({b for _, b in cells})
        m = {s: {b: sum(cells.get((s, b), {}).values()) for b in blocks} for s in subjects}
        for s in subjects:
            got = sum(m[s].values())
            if got != n:
                raise ValueError(f"{code} has {n} students but the flows give {s} to {got}")
        for b in blocks:
            got = sum(m[s][b] for s in subjects)
            if got > n:
                raise ValueError(f"{code} has {n} students but the flows put {got} of its places in block {b + 1}")

        blocks_of = dict(zip(sids, _decompose(subjects, blocks, m, n)))
        set_of: dict[tuple[str, str], int] = {}
        for (s, b), per_set in sorted(cells.items()):
            here = iter(sid for sid in sids if blocks_of[sid][s] == b)
            for k in sorted(per_set):
                for _ in range(per_set[k]):
                    set_of[next(here), s] = k
        for sid in sids:
            out[sid] = [{"subject": s, "block": blocks_of[sid][s], "set": set_of[sid, s]} for s in subjects]

    _check_split(out, placed, subjects_of, block_of, size_of)
    return out


def _check_split(out, placed, subjects_of, block_of, size_of) -> None:
    """The invariant `split` promises, checked on its result. Counts only."""
    wrong_subjects = wrong_blocks = 0
    counts: dict[tuple[str, int], int] = {}
    for sid, entries in out.items():
        if sorted(e["subject"] for e in entries) != sorted(subjects_of[placed[sid]]):
            wrong_subjects += 1
        blocks = [e["block"] for e in entries]
        if len(set(blocks)) != len(blocks) or any(block_of[e["subject"], e["set"]] != e["block"] for e in entries):
            wrong_blocks += 1
        for e in entries:
            counts[e["subject"], e["set"]] = counts.get((e["subject"], e["set"]), 0) + 1
    if wrong_subjects:
        raise ValueError(f"{wrong_subjects} students would not take each of their subjects once")
    if wrong_blocks:
        raise ValueError(f"{wrong_blocks} students would have two subjects in one block")
    off = sum(1 for key in set(size_of) | set(counts) if counts.get(key, 0) != size_of.get(key, 0))
    if off:
        raise ValueError(f"{off} sets would not hold the number of students the answer gives them")


# ---------------------------------------------------------------------------
# teachers
# ---------------------------------------------------------------------------

def _meta(subjects_meta: dict, code: str) -> dict:
    return subjects_meta.get(code) or {}


def _dept_of(plan: dict, code: str, subjects_meta: dict) -> str:
    """A subject's department: its own in subjects_meta, else that of a requirement (by id) teaching a
    subject of that name, else ""."""
    meta = _meta(subjects_meta, code)
    if meta.get("dept"):
        return str(meta["dept"])
    name = norm(meta.get("name") or code)
    for r in sorted(plan.get("requirements") or [], key=lambda r: r["id"]):
        if norm(r.get("subject")) == name and r.get("dept"):
            return str(r["dept"])
    return ""


def _own_output(r: dict, year: str | None, year_classes) -> bool:
    """A requirement an earlier apply wrote for this year (its `blocks` tag is the year key; without a key,
    any blocks output for exactly these classes): the next apply replaces it."""
    if year is not None:
        return same_year(r.get("blocks"), year)
    return bool(r.get("blocks")) and set(r.get("classes") or []) == set(year_classes)


def _has_students(st: dict) -> bool:
    """A set the answer gives students: a set of size 0 becomes nothing (no requirement, band or grouping)."""
    return int(st.get("size", 1) or 0) > 0


def _loads(plan: dict, year: str | None, year_classes) -> tuple[dict[str, float], set[tuple[str, str]]]:
    """Each teacher's current load (the periods of every requirement listing them, in full, except this
    year's own earlier blocks output, which an apply replaces) and the (teacher, subject) pairs they already
    teach to one of the year's classes (that output included)."""
    classes = set(year_classes)
    load: dict[str, float] = {}
    teaching: set[tuple[str, str]] = set()
    for r in plan.get("requirements") or []:
        own = _own_output(r, year, classes)
        for t in dict.fromkeys(r.get("teachers") or []):
            if not own:
                load[t] = load.get(t, 0) + (r.get("periods") or 0)
            if classes & set(r.get("classes") or []):
                teaching.add((t, norm(r.get("subject"))))
    return load, teaching


def allowance_left(plan: dict, settings: dict, year: str) -> dict[str, float]:
    """Staff id -> periods a cycle left: effective allowance less the current load (`_loads`)."""
    load, _ = _loads(plan, year, ())
    return {p["id"]: M.effective_allowance(p, settings) - load.get(p["id"], 0) for p in plan.get("staff") or []}


_FIT = {True: 0, None: 1, False: 2}           # specialisation.covers: covers, lists nothing, outside


def suggest_teachers(sets: list[dict], plan: dict, settings: dict, year_classes: list[str],
                     subjects_meta: dict, year: str | None = None) -> dict[tuple[str, int], str | None]:
    """A teacher for each set, {(subject, set): staff id or None}, the school to confirm.

    Candidates are the staff of the subject's department (`_dept_of`) not already given a set of the same
    block in this call. Among them, those whose allowance left (effective_allowance less their current load
    and the periods suggested so far in this call) still holds the set's periods come first; then a teacher
    already teaching that subject to one of the year's classes; then the most allowance left; then staff id.
    A teacher past their allowance can still be suggested when no one else fits. Sets go in subject, then
    set order. A teacher's current load is the periods of every requirement that lists them, in full,
    except this year's own earlier blocks output (`year`, the year key), which an apply replaces; that
    output still counts as teaching the subject, so a re-run prefers the teachers confirmed last time.
    None when no one fits, and for a set of size 0, which apply skips.

    Subject specialisation (spec 2026-10-20-csv-pack-design.md §1) goes before all of that, once anyone in
    the department lists subjects: a teacher whose subjects cover the set's subject (its name) at the year's
    level (`students.level_of` of its first class) first, then one who lists none, then one placed outside
    their list. While nobody in the department lists subjects the order is the one above, unchanged."""
    load, teaching = _loads(plan, year, year_classes)
    level = level_of(plan, year_classes[0]) if year_classes else ""
    staff = sorted(plan.get("staff") or [], key=lambda p: p["id"])
    allowance = {p["id"]: M.effective_allowance(p, settings) for p in staff}
    given: dict[str, float] = {}
    busy: set[tuple[str, int]] = set()
    out: dict[tuple[str, int], str | None] = {}
    for st in sorted(sets, key=lambda st: (st["subject"], int(st["set"]))):
        code, block = st["subject"], int(st["block"])
        if not _has_students(st):
            out[code, int(st["set"])] = None
            continue
        meta = _meta(subjects_meta, code)
        dept, periods, name = _dept_of(plan, code, subjects_meta), meta.get("periods") or 0, norm(meta.get("name") or code)
        free = [p["id"] for p in staff if dept and p.get("dept") == dept and (p["id"], block) not in busy]
        listing = bool(dept) and any(p.get("subjects") for p in staff if p.get("dept") == dept)
        subject = meta.get("name") or code
        fit = {p["id"]: _FIT[SP.covers(p, subject, level)] for p in staff if listing and p["id"] in free}

        def rank(pid: str):
            left = allowance[pid] - load.get(pid, 0) - given.get(pid, 0)
            return (fit.get(pid, 0), left < periods, (pid, name) not in teaching, -left, pid)

        pick = min(free, key=rank) if free else None
        out[code, int(st["set"])] = pick
        if pick is not None:
            busy.add((pick, block))
            given[pick] = given.get(pick, 0) + periods
    return out


# ---------------------------------------------------------------------------
# into the plan
# ---------------------------------------------------------------------------

def _letter(i: int) -> str:
    """0 -> A, 25 -> Z, 26 -> AA: a set's letter."""
    out = ""
    i += 1
    while i:
        i, r = divmod(i - 1, 26)
        out = chr(ord("A") + r) + out
    return out


def set_groupings(sets: list[dict], level: str) -> dict[tuple[str, int], str]:
    """Each set's grouping, {(subject, set): f"{level}{CODE}{letter}"}, lettered A, B, ... in set order
    per subject: the option code the plan and the students' takes share. Sets of size 0 get none (a set
    without a `size` counts as holding students)."""
    return {(code, k): f"{level}{str(code).upper()}{letter}" for (code, k), letter in _letters(sets).items()}


def _letters(sets: list[dict]) -> dict[tuple[str, int], str]:
    """Each set's letter, {(subject, set): "A"}, A, B, ... in set order per subject; sets of size 0 get none."""
    numbers: dict[str, list[int]] = {}
    for st in sets:
        if not _has_students(st):
            continue
        numbers.setdefault(st["subject"], []).append(int(st["set"]))
    return {(code, k): _letter(i) for code, ks in numbers.items() for i, k in enumerate(sorted(set(ks)))}


def student_takes(split_result: dict[str, list[dict]], division: str,
                  groupings: dict[tuple[str, int], str]) -> dict[str, list[dict]]:
    """Each student's takes for their sets, {sid: [{"division", "grouping"}]}, in `split`'s order, all in
    one given division. `takes_for` is the one to use after `apply_plan`: it finds each set's division as
    the students' groups do."""
    return {sid: [{"division": division, "grouping": groupings[e["subject"], e["set"]]} for e in entries]
            for sid, entries in split_result.items()}


def takes_for(new_plan: dict, year: str, split_result: dict[str, list[dict]]) -> dict[str, list[dict]]:
    """Each student's takes after `apply_plan(new_plan, year, ...)`: {sid: [{"division", "grouping"}]}, in
    `split`'s order. A take names the set's requirement by its grouping and the division its option group
    belongs to (`generate.option_division`, the first division covering its classes, which may be wider
    than the year's), so it is exactly the group the plan has. The groupings are rebuilt from the sets the
    split uses and the level apply wrote; ValueError (codes only) when a set has no requirement."""
    mine = {r["grouping"]: r for r in new_plan.get("requirements") or [] if same_year(r.get("blocks"), year)}
    pairs = {(e["subject"], e["set"]) for entries in split_result.values() for e in entries}
    if not pairs:
        return {sid: [] for sid in split_result}
    levels = {r.get("level") or "" for r in mine.values()}
    if len(levels) != 1:
        raise ValueError(f"the plan holds no single set of option blocks for {year}")
    groupings = set_groupings([{"subject": s, "set": k} for s, k in pairs], levels.pop())
    division_of: dict[str, str] = {}
    for g in groupings.values():
        if g not in mine:
            raise ValueError(f"{g} is not among the plan's option blocks for {year}")
        division_of[g] = option_division(new_plan, mine[g])
    return {sid: [{"division": division_of[groupings[e["subject"], e["set"]]],
                   "grouping": groupings[e["subject"], e["set"]]} for e in entries]
            for sid, entries in split_result.items()}


def _check_groupings(plan: dict, year: str) -> None:
    """Refuse an apply whose sets would share an option group (grouping within a division) with another
    requirement: B1 would merge them into one group. Codes only."""
    seen: dict[str, str] = {}
    others: set[str] = set()
    for r in plan["requirements"]:
        if r["grouping"] == "class":
            continue
        gid = option_group_id(plan, r)
        if not same_year(r.get("blocks"), year):
            others.add(gid)
        elif gid in seen:
            raise ValueError(f"{r['grouping']} and {seen[gid]} would be one option group in this division")
        else:
            seen[gid] = r["grouping"]
    for gid, grouping in sorted(seen.items(), key=lambda x: x[1]):
        if gid in others:
            raise ValueError(f"{grouping} is already used by another requirement in this division")


def apply_plan(plan: dict, year: str, year_classes: list[str], answer: dict,
               teachers: dict[tuple[str, int], str | None], subjects_meta: dict) -> dict:
    """The plan with this year's option blocks written in, normalised; the input is never changed.

    First this year's earlier output goes: the requirements, bands and division tagged `blocks == year`; those
    requirements leave every other band too, and an untagged band left with no options goes (a workbook round
    trip regroups the sets into untagged bands per subject, and keeps the requirements' tag).
    Then each set with students becomes one option requirement over the untagged division whose classes
    are the year's (one is added, tagged with the year, when the plan has none; a division tagged with
    another year key is never adopted), and each block one band of that block's sets, all tagged with the
    year. Other years' output and untagged entries stay as they are. ValueError (codes only) when one of this
    year's earlier sets is locked (nothing is removed past a lock), or when a set's grouping would share an
    option group with another requirement (`_check_groupings`)."""
    if not year_classes:
        raise ValueError("a year needs at least one class")
    locked = sorted({str(r.get("grouping")) for r in plan.get("requirements") or []
                     if same_year(r.get("blocks"), year) and r.get("locked")})
    if locked:                          # an apply replaces this year's sets: never past a lock
        one = len(locked) == 1
        raise ValueError(f"{', '.join(locked)} {'is' if one else 'are'} locked: unlock {'it' if one else 'them'} first")
    out = copy.deepcopy(plan)
    removed = {r["id"] for r in out.get("requirements") or [] if same_year(r.get("blocks"), year)}
    for section in ("requirements", "bands", "divisions"):
        out[section] = [x for x in out.get(section) or [] if not same_year(x.get("blocks"), year)]
    # a workbook round trip regroups the sets into untagged bands per subject: the old sets leave every band,
    # and an untagged band they leave empty goes
    bands = []
    for b in out["bands"]:
        options = [o for o in b.get("options") or [] if o not in removed]
        if len(options) == len(b.get("options") or []):
            bands.append(b)
        elif options or b.get("blocks"):
            bands.append({**b, "options": options})
    out["bands"] = bands
    sets = sorted((st for st in answer.get("sets") or [] if _has_students(st)),
                  key=lambda st: (st["subject"], int(st["set"])))
    if not sets:                        # nothing to write: no division is added for it either
        return M.normalise(out)

    wanted = set(year_classes)
    division = next((d for d in sorted(out["divisions"], key=lambda d: d["id"])
                     if not d.get("blocks") and set(d.get("classes") or []) == wanted), None)
    if division is None:
        division = {"id": M.unique_id(M.division_id(list(year_classes)), {d["id"] for d in out["divisions"]}),
                    "classes": list(year_classes), "blocks": year}
        out["divisions"].append(division)
    classes = list(division["classes"])
    level = level_of(out, year_classes[0])
    groupings = set_groupings(sets, level)
    taken = {r["id"] for r in out["requirements"]}
    by_block: dict[int, list[str]] = {}
    for st in sets:
        code, k = st["subject"], int(st["set"])
        meta = _meta(subjects_meta, code)
        dept, name = _dept_of(plan, code, subjects_meta), meta.get("name") or code
        periods = int(meta.get("periods") or 0)
        grouping = groupings[code, k]
        rid = M.requirement_id_for(dept, level, grouping, classes, name, taken)
        taken.add(rid)
        teacher = teachers.get((code, k))
        out["requirements"].append({
            "id": rid, "dept": dept, "level": level, "subject": name, "periods": periods,
            "lessons": {"1": periods}, "grouping": grouping, "classes": list(classes),
            "teachers": [teacher] if teacher else [], "size": int(st["size"]),
            "venue": {"kind": meta.get("room_kind"), "room": None}, "sync_with": None, "locked": False,
            "blocks": year})
        by_block.setdefault(int(st["block"]), []).append(rid)

    band_ids = {b["id"] for b in out["bands"]}
    for b in sorted(by_block):
        bid = M.unique_id(M.band_id(division["id"], f"blocks{b + 1}"), band_ids)
        band_ids.add(bid)
        out["bands"].append({"id": bid, "division": division["id"], "options": by_block[b], "blocks": year})
    out = M.normalise(out)
    _check_groupings(out, year)
    return out


# ---------------------------------------------------------------------------
# the routes' glue (task 6): year keys, subjects, the engine request, the proposal, takes
# ---------------------------------------------------------------------------

def _level_classes(plan: dict, level: str) -> list[str]:
    """The codes of level `level`'s classes, in plan order: those whose level has the same
    `specialisation.stage_key` (R15, R26: "Sec 3", "S3" and "3" are one level; "3E" and "3N" are two)."""
    key = SP.stage_key(level)
    return [c["code"] for c in plan.get("classes") or [] if SP.stage_key(level_of(plan, c["code"])) == key]


def canonical_key(key) -> str:
    """A year key with its level part as `specialisation.stage_key` ("level:Sec 3" -> "level:3", "level:3E"
    -> "level:3e": the stage spelling folded, a stream kept, R26); a "classes:" key, or one that does not
    read, as it is. The keys `year_key` gives are already canonical; this reads the raw ones stored before
    (R15)."""
    kind, _, rest = str(key or "").partition(":")
    if kind == "level" and rest.strip():
        return f"level:{SP.stage_key(rest)}"
    return str(key or "")


def same_year(tag, year) -> bool:
    """Whether a `blocks` tag (or a stored year key) names year key `year`, either written raw or canonical."""
    return bool(tag) and canonical_key(tag) == canonical_key(year)


class SetupClash(ValueError):
    """Two or more stored setups fold into one year key and say different things (R20): never merged or deleted
    silently. The message names the level and the stored spellings, nothing else."""


_SAME = ("combinations", "limits", "subjects", "overrides")      # what makes two stored setups one setup


def _aliases(setups: dict, want: str) -> list[str]:
    """The stored keys of canonical year key `want` that hold a setup: the canonical one first, then the old raw
    ones in sorted order."""
    found = [k for k in sorted(k for k in setups if isinstance(k, str))
             if canonical_key(k) == want and isinstance(setups[k], dict) and setups[k]]
    return sorted(found, key=lambda k: k != want)


def _and(items: list[str]) -> str:
    return items[0] if len(items) == 1 else ", ".join(items[:-1]) + " and " + items[-1]


def find_setup(setups: dict, key: str) -> tuple[str | None, dict | None]:
    """(the stored key, the setup) of year key `key` in `blocks_setup`: under its canonical key, else under an
    old raw key that canonicalises to it (the first in sorted order); (None, None) when there is none.

    SetupClash (R20) when two or more stored keys fold into it ("level:Sec 3" and "level:S3" are both level 3;
    since R26 "level:3E" and "level:3N" are two years and never clash) and their combinations, limits, subjects or overrides differ: the school keeps one. Copies that agree on
    those are one setup (their placements are placed again)."""
    setups = setups if isinstance(setups, dict) else {}
    want = canonical_key(key)
    found = _aliases(setups, want)
    if not found:
        return None, None
    first = setups[found[0]]
    if any(any(setups[k].get(f) != first.get(f) for f in _SAME) for k in found[1:]):
        spelt = [k.partition(":")[2] for k in found]
        raise SetupClash(f"level {want.partition(':')[2]} has option-block setups stored for {_and(spelt)}; "
                         f"keep one and delete the {'other' if len(found) == 2 else 'others'} first")
    return found[0], first


def clash_keys(setups: dict, key: str) -> list[str]:
    """The stored keys of year key `key` when they clash (`find_setup` refuses them), in its order; else []."""
    setups = setups if isinstance(setups, dict) else {}
    try:
        find_setup(setups, key)
    except SetupClash:
        return _aliases(setups, canonical_key(key))
    return []


def put_setup(setups: dict, key: str, setup: dict) -> dict:
    """`setups` with `setup` stored under the canonical key of `key`, and the old raw key it was found under
    gone (R15: moved on the next write), with any identical copy of it. SetupClash, changing nothing, when the
    stored keys of the year differ (`find_setup`). A new dict; the one given is not changed."""
    setups = setups if isinstance(setups, dict) else {}
    want = canonical_key(key)
    find_setup(setups, want)                        # refuses to write over setups that differ
    gone = set(_aliases(setups, want))              # the one found, and copies identical to it
    out = {k: v for k, v in setups.items() if k not in gone}
    out[want] = setup
    return out


def year_key(plan: dict, spec) -> str:
    """The canonical key of a year group: "level:X" when the classes are exactly level X's classes in the
    plan, else "classes:<codes sorted, comma-joined>". X is the level's `specialisation.stage_key` (R15:
    "Sec 3" and "3" both give "level:3"; R26: "3E" gives "level:3e", its own year). `spec` is {"level": X} or {"classes": [...]}.
    ValueError (codes only) for an unknown level or class, or a spec that is neither."""
    if not isinstance(spec, dict) or set(spec) not in ({"level"}, {"classes"}):
        raise ValueError("choose a level or a list of classes")
    if "level" in spec:
        level = str(spec["level"] or "").strip()
        if not level or not _level_classes(plan, level):
            raise ValueError(f"the plan has no classes at level {level or '(none)'}")
        return f"level:{SP.stage_key(level)}"
    given = spec["classes"]
    if not isinstance(given, list) or not given or not all(isinstance(c, str) for c in given):
        raise ValueError("choose at least one class")
    known = {c["code"] for c in plan.get("classes") or []}
    classes = sorted(set(given))
    for c in classes:
        if c not in known or "," in c:
            raise ValueError(f"the class {c} isn't known")
    levels = {SP.stage_key(level_of(plan, c)) for c in classes}
    if len(levels) == 1:
        (level,) = levels
        if level and set(_level_classes(plan, level)) == set(classes):
            return f"level:{level}"
    return "classes:" + ",".join(classes)


def year_classes(plan: dict, key: str) -> list[str]:
    """The classes of year key `key` (`year_key`), in plan order for a level; ValueError when the key does not
    read or names a class the plan no longer has."""
    kind, _, rest = str(key or "").partition(":")
    if kind == "level" and rest:
        classes = _level_classes(plan, rest)
        if not classes:
            raise ValueError(f"the plan has no classes at level {rest}")
        return classes
    if kind == "classes" and rest:
        classes = rest.split(",")
        known = {c["code"] for c in plan.get("classes") or []}
        for c in classes:
            if c not in known:
                raise ValueError(f"the class {c} isn't known")
        return classes
    raise ValueError("no such year group")


def _plan_requirement(plan: dict, code: str, name: str) -> dict | None:
    """The plan requirement a combination subject takes its defaults from: one whose subject matches the code
    or name, or whose grouping matches the code (students.norm); untagged before blocks output, then by id."""
    keys = {norm(code), norm(name)}
    found = [r for r in plan.get("requirements") or []
             if norm(r.get("subject")) in keys or norm(r.get("grouping")) == norm(code)]
    return min(found, key=lambda r: (bool(r.get("blocks")), r["id"])) if found else None


def _setup_level(plan: dict, setup: dict) -> str:
    """The level of a setup's year group: its "level", else the one level all its classes share, else ""."""
    spec = setup.get("year") if isinstance(setup.get("year"), dict) else {}
    if spec.get("level"):
        return str(spec["level"])
    keys = {SP.level_key(level_of(plan, c)) for c in spec.get("classes") or []}
    return keys.pop() if len(keys) == 1 else ""


def subjects_meta(plan: dict, setup: dict, levels: dict | None = None) -> tuple[dict[str, dict], list[dict]]:
    """Each combination subject's {name, dept, periods, room_kind}, and the blocking checks it raises.

    The setup's own value first; then, given the timetable's `levels` store, the year's level's Banded row for
    the subject (`levels.banded_meta`, matched on the code or the name by `students.norm`), its dept, periods
    and room kind (csv-pack Calls §1); else, from the plan requirement that matches (`_plan_requirement`),
    its dept, periods and venue kind; the name defaults to the code. A subject with no periods a cycle any
    way is a blocking `periods_missing` check naming the code, and one with no department any way a blocking
    `dept_missing` (no teacher could be counted or suggested for it)."""
    given_all = setup.get("subjects") or {}
    level = _setup_level(plan, setup) if levels else ""
    banded = {norm(k): v for k, v in LV.banded_meta(levels, level).items()} if level else {}
    out: dict[str, dict] = {}
    found: list[dict] = []
    for c in setup.get("combinations") or []:
        for code in c.get("subjects") or []:
            if code in out:
                continue
            given = given_all.get(code) or {}
            name = given.get("name") or code
            b = banded.get(norm(code)) or banded.get(norm(name)) or {}
            r = _plan_requirement(plan, code, name) or {}
            out[code] = {"name": name, "dept": given.get("dept") or b.get("dept") or r.get("dept") or "",
                         "periods": given.get("periods") or b.get("periods") or r.get("periods") or None,
                         "room_kind": given.get("room_kind") or b.get("room_kind")
                         or (r.get("venue") or {}).get("kind") or None}
            if out[code]["periods"] is None:
                found.append({"kind": "periods_missing", "text": f"{code} has no periods a cycle", "blocking": True})
            if not out[code]["dept"]:
                found.append({"kind": "dept_missing", "text": f"{code} has no department", "blocking": True})
    return out, found


def no_teacher_left(request: dict, meta: dict[str, dict]) -> list[str]:
    """The subjects of an engine request (`engine_request`) that no teacher of their department has the
    allowance for, each as "GEO: no HUM teacher has 3 periods a cycle left". Codes and counts only."""
    out = []
    for s in request.get("subjects") or []:
        if s["teachers"]:
            continue
        m = meta.get(s["code"]) or {}
        periods = m.get("periods") or 0
        out.append(f"{s['code']}: no {m.get('dept')} teacher has {periods} period{'' if periods == 1 else 's'} "
                   f"a cycle left")
    return out


def engine_request(setup: dict, placed: dict[str, str], meta: dict[str, dict], plan: dict, settings: dict,
                   year: str) -> tuple[dict, dict[str, str]]:
    """The engine's blocks request and the map from its opaque combination ids back to codes.

    Combinations with a student placed go as {id: "c1", "c2", ..., subjects: codes, count}; each of their
    subjects as {code, teachers, room_cap}: `teachers` is how many staff of its department have allowance
    left (`allowance_left`) for its periods, `room_cap` the largest venue of its room kind, else the largest
    set allowed. Limits {blocks, min_size, max_size}. Departments [{subjects, teachers}], one anonymous group per
    department code, sorted by code: its subjects (sorted) and how many of its staff have allowance left for the
    smallest periods among them, so one teacher counted for several subjects is not counted twice in a block.
    No name, student ID, merit rank or department code: counts only."""
    counts: dict[str, int] = {}
    for code in placed.values():
        counts[code] = counts.get(code, 0) + 1
    limits = setup["limits"]
    combos = [c for c in setup.get("combinations") or [] if counts.get(c["code"])]
    mapping = {f"c{i}": c["code"] for i, c in enumerate(combos, 1)}
    left = allowance_left(plan, settings, year)
    venues = plan.get("venues") or []
    staff = plan.get("staff") or []

    def able(dept, periods) -> int:                 # past the engine's cap, more teachers change nothing it decides
        return min(MAX_TEACHERS, sum(1 for p in staff if dept and p.get("dept") == dept and left[p["id"]] >= periods))

    subjects = []
    by_dept: dict[str, list[str]] = {}
    for code in sorted({s for c in combos for s in c["subjects"]}):
        m = meta.get(code) or {}
        periods, dept, kind = m.get("periods") or 0, m.get("dept"), m.get("room_kind")
        caps = [int(v["capacity"]) for v in venues if kind and v.get("capacity") and norm(v.get("kind")) == norm(kind)]
        subjects.append({"code": code, "teachers": able(dept, periods),
                         "room_cap": max(caps) if caps else int(limits["max_size"])})
        if dept:
            by_dept.setdefault(str(dept), []).append(code)
    departments = [{"subjects": codes,
                    "teachers": able(dept, min((meta.get(c) or {}).get("periods") or 0 for c in codes))}
                   for dept, codes in sorted(by_dept.items())]
    request = {"combinations": [{"id": cid, "subjects": list(dict.fromkeys(c["subjects"])), "count": counts[c["code"]]}
                                for cid, c in zip(mapping, combos)],
               "subjects": subjects,
               "limits": {k: int(limits[k]) for k in ("blocks", "min_size", "max_size")},
               "departments": departments}
    return request, mapping


_OPAQUE = re.compile(r"\bc\d+\b")


def named_answer(answer: dict, mapping: dict[str, str]) -> dict:
    """The engine's answer with its opaque combination ids put back to codes, in flows and reasons."""
    out = copy.deepcopy(answer)
    for f in out.get("flows") or []:
        f["combination"] = mapping.get(f["combination"], f["combination"])
    out["reasons"] = [_OPAQUE.sub(lambda m: mapping.get(m.group(0), m.group(0)), str(r)) for r in out.get("reasons") or []]
    return out


def proposal(answer: dict, teachers: dict[tuple[str, int], str | None], plan: dict, settings: dict,
             meta: dict[str, dict], year: str, placed: int = 0, unplaced: int = 0) -> dict:
    """What the page shows of a run: {status, reasons, blocks: [[set]], summary: {sets, students, unplaced}},
    one column per block, each set {subject, set, name ("Physics A"), size, teacher, teacher_name,
    suggested, warnings}. Sets of size 0 are left out (apply skips them). Warnings come from `rewarn`."""
    sets = [st for st in answer.get("sets") or [] if _has_students(st)]
    letters = _letters(sets)
    width = max((int(st["block"]) + 1 for st in answer.get("sets") or []), default=0)
    blocks: list[list[dict]] = [[] for _ in range(width)]
    for st in sets:
        code, k = st["subject"], int(st["set"])
        name = (meta.get(code) or {}).get("name") or code
        blocks[int(st["block"])].append({"subject": code, "set": k, "name": f"{name} {letters[code, k]}",
                                         "size": int(st["size"]), "teacher": teachers.get((code, k)),
                                         "teacher_name": None, "suggested": True, "warnings": []})
    for column in blocks:
        column.sort(key=lambda s: (s["name"], s["subject"], s["set"]))
    out = {"status": answer.get("status"), "reasons": list(answer.get("reasons") or []), "blocks": blocks,
           "summary": {"sets": len(sets), "students": placed, "unplaced": unplaced}}
    return rewarn(out, plan, settings, meta, year)


def _num(x: float) -> str:
    return str(int(x)) if float(x).is_integer() else f"{x:g}"


def _year_classes_or_none(plan: dict, year: str) -> list[str]:
    try:
        return year_classes(plan, year)
    except ValueError:
        return []


def rewarn(prop: dict, plan: dict, settings: dict, meta: dict[str, dict], year: str) -> dict:
    """The proposal with each set's teacher name and warnings worked out again: a teacher in two sets of one
    block, a teacher whose allowance left (`allowance_left`) the proposal's sets go past, and a teacher who
    lists subjects placed outside them (`specialisation.outside`, the set's subject by name at the year's
    level; the option blocks' own subjects count as subjects, not as names that match nothing)."""
    out = copy.deepcopy(prop)
    names = {p["id"]: p.get("name") or p["id"] for p in plan.get("staff") or []}
    staff_by_id = {p["id"]: p for p in plan.get("staff") or []}
    specialists = any(p.get("subjects") for p in staff_by_id.values())
    classes = _year_classes_or_none(plan, year) if specialists else []
    level = level_of(plan, classes[0]) if classes else ""
    known = (SP.plan_subjects(plan) | {norm(m.get("name") or c) for c, m in meta.items()} | {norm(c) for c in meta}
             if specialists else set())
    left = allowance_left(plan, settings, year)
    given: dict[str, float] = {}
    for column in out["blocks"]:
        for s in column:
            if s["teacher"]:
                given[s["teacher"]] = given.get(s["teacher"], 0) + ((meta.get(s["subject"]) or {}).get("periods") or 0)
    for column in out["blocks"]:
        for s in column:
            t = s["teacher"]
            s["teacher_name"] = names.get(t, t) if t else None
            s["warnings"] = []
            if not t:
                continue
            others = [o["name"] for o in column if o is not s and o["teacher"] == t]
            if others:
                s["warnings"].append(f"{s['teacher_name']} also teaches {', '.join(others)} in this block")
            over = given[t] - left.get(t, 0)
            if over > 0:
                s["warnings"].append(f"{s['teacher_name']} would be {_num(over)} periods over allowance")
            if specialists:
                as_req = {"subject": (meta.get(s["subject"]) or {}).get("name") or s["subject"], "level": level,
                          "classes": classes, "teachers": [t]}
                s["warnings"] += SP.outside(plan, as_req, staff_by_id=staff_by_id, known_subjects=known)
    return out


def old_groups(plan: dict, year: str) -> set[str]:
    """The option group ids of this year's blocks requirements in `plan` (an earlier apply's output)."""
    return {option_group_id(plan, r) for r in plan.get("requirements") or []
            if same_year(r.get("blocks"), year) and r.get("grouping") != "class"}


def rebuild_takes(students: list[dict], year_classes, old: set[str], new_takes: dict[str, list[dict]],
                  group_id=None) -> list[dict]:
    """The students after an apply (R12): each student of the year (by class, or placed) loses the takes
    whose group is in `old` and gains `new_takes[sid]` (a placed student; the app chose those sets, so they are
    marked `placed_by_app`); an unplaced one loses the old ones only. Every other student and take is as it
    was. A new list; the one given is not changed."""
    gid = group_id or _group_id
    classes = set(year_classes)
    out = []
    for s in students:
        if s.get("class") not in classes and s.get("id") not in new_takes:
            out.append(s)
            continue
        keep = [t for t in s.get("takes") or [] if gid(t) not in old]
        kept = {t["grouping"] for t in keep}
        added = [t for t in new_takes.get(s["id"], []) if t not in keep]
        by_app = [g for g in s.get("placed_by_app") or [] if g in kept]
        by_app += [t["grouping"] for t in added if t["grouping"] not in by_app]
        out.append({**s, "takes": keep + [dict(t) for t in added], "placed_by_app": by_app})
    return out


def year_fingerprint(students: list[dict], overrides: dict | None) -> str:
    """A digest of what a run's placement rests on: each of the year's students' ID, class, choices and merit
    rank, and the overrides. Never their takes or `placed_by_app`, which an Apply itself writes, so a re-Apply
    of the same run still matches."""
    rows = sorted(([str(s.get("id")), str(s.get("class")), [str(c) for c in s.get("choices") or []], s.get("merit")]
                   for s in students), key=lambda row: (row[0], row[1]))
    body = json.dumps({"students": rows, "overrides": dict(sorted((overrides or {}).items()))}, sort_keys=True,
                      default=str)
    return hashlib.sha1(body.encode()).hexdigest()


def setup_digest(setup: dict | None) -> str:
    """A digest of what a run's engine request rests on in a year's setup: its combinations, limits and subject
    details. Never its overrides (the students fingerprint holds them) or its placement, which saving the setup
    resets, so saving it unchanged still matches."""
    setup = setup or {}
    body = json.dumps({k: setup.get(k) for k in ("combinations", "limits", "subjects")}, sort_keys=True, default=str)
    return hashlib.sha1(body.encode()).hexdigest()
