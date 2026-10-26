"""Forming the classes of a level (spec docs/superpowers/specs/2026-10-20-csv-pack-design.md §3). Pure
functions, no database or web imports.

`propose` works out a level's form classes from its students (or the number the school typed), fills
them, writes the requirements of its Class and Combined subjects and suggests a teacher for each, and
writes each Runs-with subject (Mother Tongue) as groups across the level, side by side in one band; the
school reviews that, then `apply_proposal` writes it into the plan, tagged `formed` with the level key so
forming the level again replaces it. `student_classes` gives each student's class from a proposal.

What it says carries class codes, groupings, subject words, level names and staff names only: never a
student's name, and a warning never names a student at all."""
from __future__ import annotations

import copy
import math
from itertools import accumulate

from . import blocks as B
from .plan import model as M
from .plan import specialisation as SP
from .plan.generate import option_division, option_group_id
from .students import _id_key, group_id, level_of, norm


# ---------------------------------------------------------------------------
# how many classes, and their codes
# ---------------------------------------------------------------------------

def class_count(students: int, class_size: int) -> int:
    """ceil(students / class_size), and at least one class."""
    return max(1, math.ceil(max(0, int(students)) / max(1, int(class_size))))


DEFAULT_NAMING = "{n}{x}"


def class_codes(level_name, n: int, naming) -> list[str]:
    """The codes of a level's `n` classes. `naming` is a pattern in which "{n}" stands for the level's
    class prefix (`specialisation.class_prefix`: "Sec 3" -> "3", "P4" -> "P4", "Year Seven" -> "Year
    Seven") and "{x}" for the letters A..Z, AA, AB...; "" or None is the default "{n}{x}" (3A, 3B). A
    pattern without "{x}" has the letters added at its end, so the codes always differ."""
    pattern = naming or DEFAULT_NAMING
    if "{x}" not in pattern:
        pattern += "{x}"
    prefix = SP.class_prefix(level_name)
    return [pattern.replace("{n}", prefix).replace("{x}", B._letter(i)) for i in range(n)]


# ---------------------------------------------------------------------------
# filling them
# ---------------------------------------------------------------------------

def _sizes(total: int, k: int) -> list[int]:
    """`total` split over `k` classes within one of each other, the larger ones first (in code order)."""
    q, r = divmod(total, k)
    return [q + 1 if i < r else q for i in range(k)]


def fill(students, codes: list[str], placement: dict | None = None) -> dict[str, list[str]]:
    """Each class's students, {code: [sid]}, every list in natural ID order. `students` are student IDs.

    The sizes are fixed first: the students split over the classes within one of each other, the larger
    classes first in code order. Even sizes therefore always win over a perfect grouping.

    Without a placement (Sec 1 and 2, or any level whose students carry no choices) the students go in
    natural ID order: the first class takes the first of them, and so on.

    With a placement ({sid: combination code}, `blocks.place`), students of one combination share a class
    where they can:
    1. the combinations go largest first, ties by combination code (natural order), each one's students
       in natural ID order;
    2. while a combination has at least as many students left as an empty class holds, it fills that
       class whole (the largest such class, ties in code order);
    3. what is left of it goes into the class with the fewest free places that still holds it all, ties in
       code order (a partly filled class before an empty one, keeping empty classes for large combinations);
    4. when no class holds it all, it fills the class with the most free places, ties in code order, and
       the rest goes round again from step 3.
    Students the placement leaves out (no code) go last, in natural ID order, into the free places in code
    order. Every step breaks ties by code or natural ID, so the result never depends on the input order."""
    sids = sorted(dict.fromkeys(students), key=_id_key)
    codes = list(codes)
    if not codes:
        return {}
    room = dict(zip(codes, _sizes(len(sids), len(codes))))
    out: dict[str, list[str]] = {code: [] for code in codes}

    def put(code: str, members: list[str]) -> None:
        out[code] += members
        room[code] -= len(members)

    if placement is None:
        queue = iter(sids)
        for code in codes:
            put(code, [next(queue) for _ in range(room[code])])
        return out

    groups: dict[str, list[str]] = {}
    unplaced: list[str] = []
    for sid in sids:
        code = placement.get(sid)
        if code:
            groups.setdefault(str(code), []).append(sid)
        else:
            unplaced.append(sid)
    for combo in sorted(groups, key=lambda c: (-len(groups[c]), _id_key(c))):
        left = groups[combo]
        while left:
            whole = [c for c in codes if not out[c] and 0 < room[c] <= len(left)]
            if whole:                                   # step 2: a whole empty class
                code = max(whole, key=lambda c: room[c])            # max keeps the first of a tie
            else:
                holds = [c for c in codes if room[c] >= len(left)]
                if holds:                               # step 3: the tightest class that holds the rest
                    code = min(holds, key=lambda c: room[c])
                else:                                   # step 4: the roomiest class, then round again
                    code = max(codes, key=lambda c: room[c])
            take = min(room[code], len(left))
            put(code, left[:take])
            left = left[take:]
    queue = iter(unplaced)
    for code in codes:
        put(code, [next(queue) for _ in range(room[code])])
    return {code: sorted(members, key=_id_key) for code, members in out.items()}


# ---------------------------------------------------------------------------
# the proposal
# ---------------------------------------------------------------------------

def _level_students(plan: dict, key: str, students) -> list[dict]:
    """The students of level `key`: their class's level, or their own while they have no class. One per ID."""
    out: dict[str, dict] = {}
    for s in students or []:
        if SP.level_key(level_of(plan, s)) == key:
            out.setdefault(str(s["id"]), s)
    return list(out.values())


def _placement(level_students: list[dict], setup: dict | None, name: str, warnings: list[str]) -> dict | None:
    """{sid: combination code} from `blocks.place` when the level has combinations and its students carry
    choices, else None (natural ID order)."""
    if not setup or not setup.get("combinations") or not any(s.get("choices") for s in level_students):
        return None
    try:
        combinations = B.check_combinations(setup["combinations"])
    except ValueError as e:                     # codes and counts only
        warnings.append(f"{name}: the combinations were not used ({e})")
        return None
    result = B.place(level_students, combinations, setup.get("overrides") or {})
    if result["unplaced"]:
        n = len(result["unplaced"])
        warnings.append(f"{name}: {n} student{'' if n == 1 else 's'} with no combination fill the last places")
    return result["placed"]


def taking(students: list[dict], level_key: str, subject: str, plan: dict | None = None) -> int:
    """How many of level `level_key`'s students take `subject`: those with it (compared under `norm`) among
    the comma-split cells of their `choices`, or of their `subjects`. A student's level is their class's
    (in `plan`, else the digits its code starts with), or their own while they have no class. A count only:
    no student is named."""
    return sum(1 for s in _level_students(plan or {}, SP.level_key(level_key), students) if _lists(s, subject))


def _lists(student: dict, subject: str) -> bool:
    """Whether `student` has `subject` (under `norm`) among the comma-split cells of their `choices` or
    `subjects`."""
    want = norm(subject)
    return bool(want) and any(norm(part) == want
                              for cell in [*(student.get("choices") or []), *(student.get("subjects") or [])]
                              for part in str(cell if cell is not None else "").split(","))


def _requirements(entry: dict, key: str, classes: list[dict], plan: dict, warnings: list[str],
                  students: list[dict] | None = None) -> tuple[list[dict], list[dict]]:
    """(requirements, bands) of the level's Class, Combined and Runs-with subjects, the requirements in the
    subject rows' order. A Class or Combined row's are in code order (`_class_requirements`), with the
    teachers the deployment names (`_named`); a Runs-with row's are its groups across the level
    (`_runs_with`). Each Runs-with name is one band, {name, requirements: [indexes into the requirements]},
    in the order the names first come; names that differ only in case and spaces ("MT", "m t") are one,
    under its first spelling. A band whose subjects' lessons (a pattern, else single periods) share no length
    cannot line up side by side, and is said. Banded subjects write nothing."""
    name = entry.get("name") or key
    size = {c["code"]: c["size"] for c in classes}
    codes = [c["code"] for c in classes]
    out: list[dict] = []
    bands: dict[str, tuple[str, list[int]]] = {}       # norm(name) -> (first spelling, indexes)
    lettered: dict[str, int] = {}               # Runs-with code -> groups lettered so far
    for row in entry.get("subjects") or []:
        if (row.get("taught_as") or "class") == "banded":
            continue
        if row.get("runs_with"):
            reqs = _runs_with(row, key, codes, entry, plan, students or [], name, warnings, lettered)
            bands.setdefault(norm(row["runs_with"]), (row["runs_with"], []))[1].extend(
                range(len(out), len(out) + len(reqs)))
        else:
            reqs = _class_requirements(row, key, codes, size, name, warnings)
            _named(row, reqs, plan, name, warnings)
        out += reqs
    for band, ix in bands.values():
        lengths: dict[str, set] = {}            # norm(subject) -> the lesson lengths its groups have
        for i in ix:
            lessons = out[i].get("lessons") or {"1": out[i]["periods"]}
            lengths.setdefault(norm(out[i]["subject"]), set()).update(k for k, v in lessons.items() if v)
        if len(lengths) > 1 and not set.intersection(*lengths.values()):
            warnings.append(f"{name}: {band}: its subjects' lesson patterns differ, so they can't all run side by side")
    return out, [{"name": n, "requirements": ix} for n, ix in bands.values()]


def _runs_with(row: dict, key: str, codes: list[str], entry: dict, plan: dict, students: list[dict],
               name: str, warnings: list[str], lettered: dict[str, int]) -> list[dict]:
    """A Runs-with row's groups, each one requirement over all the level's classes, lettered by
    `blocks.set_groupings` from the level's class prefix and the subject's code (its letters and digits,
    upper case, the first four: Sec 3 Chinese gives 3CHINA, 3CHINB). A code an earlier row of the level
    made too (Higher Chinese and Higher Malay both make HIGH) goes on from that row's letters (`lettered`,
    updated), so no two groups share a grouping. The count is the sum of the row's
    `deploy` groups when every entry gives one; else `class_count` of the level's students `taking` it
    over the class size (one group when none does, and said). Each group's size is those students split
    evenly, the larger groups first (None with no student). Lessons as `_pattern`. Teachers as `_grouped`;
    no group is suggested (`suggested: False`): a group no one is named for stays unassigned."""
    subject = row["subject"]
    deploy = row.get("deploy") or []
    n = taking(students, key, subject, plan)
    if deploy and all(d.get("groups") for d in deploy):
        g = sum(int(d["groups"]) for d in deploy)
    else:
        g = class_count(n, entry.get("class_size") or 40)
        if not n:
            warnings.append(f"{name}: no student lists {subject}; give Groups in requirements.csv, "
                            f"or name the subject in each student's choices in students.csv")
    code = "".join(ch for ch in subject.upper() if ch.isalnum())[:4]
    first = lettered.get(code, 0)
    lettered[code] = first + g
    groupings = B.set_groupings([{"subject": code, "set": i} for i in range(first + g)], SP.class_prefix(name))
    sizes = _sizes(n, g) if n else [None] * g
    lessons = _pattern(row, name, warnings)
    out = []
    for i in range(g):
        req = {"subject": subject, "classes": list(codes), "grouping": groupings[code, first + i],
               "level": key, "periods": int(row["periods"]), "room_kind": row.get("room_kind"),
               "dept": row.get("dept") or "", "size": sizes[i],
               "teacher": None, "suggested": False, "warnings": []}
        if lessons:
            req["lessons"] = dict(lessons)
        out.append(req)
    _grouped(row, out, plan, name, warnings)
    return out


def _grouped(row: dict, reqs: list[dict], plan: dict, name: str, warnings: list[str]) -> None:
    """The teachers a Runs-with row's `deploy` names, set in place on its groups `reqs`, each then
    `named: True`. In file order, a teacher takes their count of groups (one without a count), the next
    starting after; a blank teacher takes nothing. A teacher with no group left, or fewer than their count
    (trimmed), is said; groups left over
    stay unassigned, and are said. A name `_resolve` finds no one for, or more than one, leaves its groups
    unassigned; a teacher of another department is kept as named (`_said`)."""
    subject, g = row["subject"], len(reqs)
    dept_of = {p["id"]: p.get("dept") or "" for p in plan.get("staff") or []}
    t = 0                                       # the groups named so far
    for d in row.get("deploy") or []:
        written = d.get("teacher")
        if not written:
            continue
        count = int(d.get("groups") or 1)
        share = range(min(t, g), min(t + count, g))
        t += count
        if not share:
            warnings.append(f"{name}: {subject}: {written} has no group")
            continue
        if len(share) < count:
            warnings.append(f"{name}: {subject}: {written}'s count is trimmed to {len(share)}")
        pid, text = _said(plan, row, written, name, dept_of, f"so {len(share)} group(s) have no teacher")
        if text and text not in warnings:
            warnings.append(text)
        for i in share if pid else ():
            reqs[i].update(teacher=pid, named=True)
    if t < g:
        warnings.append(f"{name}: {subject} has {g} groups and teachers for {t}; {g - t} group(s) have no teacher")


def _class_requirements(row: dict, key: str, codes: list[str], size: dict, name: str,
                        warnings: list[str]) -> list[dict]:
    """One subject row's requirements, in code order: one per class, or Combined N grouping the classes N at
    a time; a short last group is taught on its own (one class alone, or the few left together) and said in
    `warnings`. Each is unassigned and `suggested`, and carries the row's lesson pattern as `lessons` when
    it has one that still adds up to the row's periods (none: `apply_proposal` writes single periods); one
    that no longer does (the periods changed since) is left out, and said."""
    taught = row.get("taught_as") or "class"
    n = int(row.get("combine") or 1) if taught == "combined" else 1
    groups = [codes[i:i + n] for i in range(0, len(codes), n)]
    if n > 1 and groups and len(groups[-1]) < n:
        last = groups[-1]
        if len(last) == 1:
            warnings.append(f"{name}: {row['subject']} is combined {n} at a time; {last[0]} is taught alone")
        else:
            warnings.append(f"{name}: {row['subject']} is combined {n} at a time; "
                            f"{'+'.join(last)} are taught together, {len(last)} classes")
    lessons = _pattern(row, name, warnings)
    out = []
    for group in groups:
        req = {"subject": row["subject"], "classes": list(group),
               "grouping": "class" if len(group) == 1 else "+".join(group),
               "level": key, "periods": int(row["periods"]), "room_kind": row.get("room_kind"),
               "dept": row.get("dept") or "", "size": sum(size[c] or 0 for c in group),
               "teacher": None, "suggested": True, "warnings": []}
        if lessons:
            req["lessons"] = dict(lessons)
        out.append(req)
    return out


def _pattern(row: dict, name: str, warnings: list[str]) -> dict | None:
    """The row's lesson pattern while it still adds up to the row's periods; None without one, or for one
    that no longer does (the periods changed since), which is said."""
    lessons = row.get("lessons") or None
    if lessons and _total(lessons) != int(row["periods"]):
        warnings.append(f"{name}: {row['subject']}: the lesson pattern no longer adds up to {int(row['periods'])} "
                        f"periods, so single periods are used")
        lessons = None
    return lessons


def _total(lessons: dict) -> int:
    """The periods a lesson pattern {length: count} makes."""
    return sum(int(length) * int(n) for length, n in lessons.items())


def resolve_teacher(plan: dict, written: str) -> str | None:
    """The staff id of the teacher a file names as `written` (`_resolve`), else None."""
    return _resolve(plan, written)[0]


def deployed_ids(plan: dict, entry: dict | None, subject: str) -> list[str]:
    """The staff ids the deployment of the level `entry` (a levels entry) names for `subject` (its subject
    rows matched under `norm`), each as `_resolve` finds them; a name it finds no one for is left out."""
    rows = [r for r in (entry or {}).get("subjects") or [] if norm(r.get("subject")) == norm(subject)]
    found = (resolve_teacher(plan, d["teacher"]) for r in rows for d in r.get("deploy") or [] if d.get("teacher"))
    return list(dict.fromkeys(pid for pid in found if pid))


def _resolve(plan: dict, written: str) -> tuple[str | None, str | None]:
    """(staff id, None) for the one teacher whose name matches `written` under `students.norm` (case and
    spaces aside), else the one whose short name does; (None, "tie") when two people match at the first
    that any does (a name two staff share is no one's); (None, "missing") when nobody's does."""
    want = norm(written)
    for field in ("name", "short"):
        found = [p["id"] for p in plan.get("staff") or [] if want and norm(p.get(field)) == want]
        if found:
            return (found[0], None) if len(found) == 1 else (None, "tie")
    return None, "missing"


def _named(row: dict, reqs: list[dict], plan: dict, name: str, warnings: list[str]) -> None:
    """The teachers the row's `deploy` names, set in place on its requirements `reqs` (code order), each
    then `suggested: False` and `named: True`. In file order, a teacher with a count of classes takes that
    many whole classes in code order, and so every group (Combined N) holding one of them; the next starts
    after the last such group. A count past the classes left is trimmed and said: with how many classes the row
    names in all when that is more than the level has, else (Combined N, whose groups round each count up)
    as the groups running out. Teachers named without a count share the groups left in file order, as evenly
    as they go, the larger shares first. A teacher whose share comes out empty is said ("has no class"). A
    blank teacher takes nothing. A name `_resolve` finds no one for, or more than one,
    keeps its share for the suggestion, and is said. A teacher of another department than the row's is
    kept as named, and said. Whatever is not named stays for `_suggest`."""
    deploy = [d for d in row.get("deploy") or [] if d.get("teacher")]
    if not deploy or not reqs:
        return
    subject, k = row["subject"], sum(len(r["classes"]) for r in reqs)
    starts = [0, *accumulate(len(r["classes"]) for r in reqs)]     # class position each group starts at
    counted = [d for d in deploy if d.get("classes")]
    asked = sum(d["classes"] for d in counted)
    n_at_a_time = int(row.get("combine") or 1)  # Combined N: the classes a group holds
    shares: list[tuple[str, range]] = []
    g = 0                                       # the next group not yet named
    for d in counted:
        who = d["teacher"]
        through = starts[g] + d["classes"]      # this teacher's classes end before this position
        h = g
        while h < len(reqs) and starts[h] < through:
            h += 1
        if through > k:
            got = starts[h] - starts[g]
            if asked > k:
                said = f"so {who} has no class" if not got else f"{who}'s count is trimmed"
                warnings.append(f"{name}: {subject} names {asked} classes for teachers but the level has {k}"
                                f"{', ' if not got else '; '}{said}")
            else:                               # the counts fit, but whole groups rounded them up
                said = f"so {who} has no group left" if not got else f"so {who}'s count is trimmed to {got}"
                warnings.append(f"{name}: {subject} is combined {n_at_a_time} at a time, {said}")
        shares.append((who, range(g, h)))
        g = h
    rest = [d for d in deploy if not d.get("classes")]
    for d, n in zip(rest, _sizes(len(reqs) - g, len(rest)) if rest else []):
        if not n:
            warnings.append(f"{name}: {subject}: {d['teacher']} has no class")
        shares.append((d["teacher"], range(g, g + n)))
        g += n
    dept_of = {p["id"]: p.get("dept") or "" for p in plan.get("staff") or []}
    for written, groups in shares:
        pid, text = _said(plan, row, written, name, dept_of, "so a teacher is suggested")
        if text and groups and text not in warnings:
            warnings.append(text)
        for i in groups if pid else ():
            reqs[i].update(teacher=pid, suggested=False, named=True)


def _said(plan: dict, row: dict, written: str, name: str, dept_of: dict, unfound: str) -> tuple[str | None, str | None]:
    """(staff id or None, the warning or None) for a teacher the row's deployment names as `written`: one
    `_resolve` finds no one for ("is not on the staff list") or more than one for ("matches more than one
    teacher"), followed by `unfound`; one of another department than the row's ("kept as named")."""
    pid, why = _resolve(plan, written)
    if pid is None:
        said = "matches more than one teacher" if why == "tie" else "is not on the staff list"
        return None, f"{name}: {row['subject']}: {written} {said}, {unfound}"
    if row.get("dept") and dept_of[pid] != row["dept"]:
        return pid, (f"{name}: {row['subject']}: {written} is in {dept_of[pid] or 'no department'}, "
                     f"not {row['dept']}; kept as named")
    return pid, None


def _without_own(plan: dict, key: str) -> dict:
    return {**plan, "requirements": [r for r in plan.get("requirements") or [] if r.get("formed") != key]}


def _allowance_left(plan: dict, settings: dict, key: str) -> dict[str, float]:
    """Staff id -> periods a cycle left (`blocks.allowance_left`), this level's own earlier output aside:
    forming the level again replaces it. "" is a year key no option-blocks output carries."""
    return B.allowance_left(_without_own(plan, key), settings, "")


def _suggest(reqs: list[dict], plan: dict, settings: dict, key: str) -> None:
    """A teacher for each requirement still `suggested`, in order, set in place; one the deployment names
    keeps its teacher, whose periods count against their allowance first. Candidates are the staff of its
    department.
    They rank by `specialisation.covers` (True, then None for a teacher who lists no subjects, then False);
    then whether their allowance left (less what this call has given them) still holds the periods; then
    whether they were already suggested this subject on another class of the level, so one teacher keeps a
    set of classes; then the most allowance left; then staff id. A teacher past their allowance, or outside
    their subjects, is still suggested when no one better fits (`rewarn` says so); None when the department
    has no staff or none is given."""
    staff = sorted(plan.get("staff") or [], key=lambda p: p["id"])
    left = _allowance_left(plan, settings, key)
    given: dict[str, float] = {}
    for r in reqs:
        if not r["suggested"] and r["teacher"]:
            given[r["teacher"]] = given.get(r["teacher"], 0) + r["periods"]
    kept: set[tuple[str, str]] = set()              # (teacher, subject) suggested so far
    fit_cache: dict[tuple[str, str], int] = {}
    for r in reqs:
        if not r["suggested"]:
            continue
        dept, subject, periods = r["dept"], r["subject"], r["periods"]
        free = [p for p in staff if dept and p.get("dept") == dept]

        def rank(p: dict):
            pid = p["id"]
            fit = fit_cache.get((pid, norm(subject)))
            if fit is None:
                fit = fit_cache[pid, norm(subject)] = B._FIT[SP.covers(p, subject, key)]
            room = left.get(pid, 0) - given.get(pid, 0)
            return (fit, room < periods, (pid, norm(subject)) not in kept, -room, pid)

        pick = min(free, key=rank)["id"] if free else None
        r["teacher"] = pick
        if pick is not None:
            given[pick] = given.get(pick, 0) + periods
            kept.add((pick, norm(subject)))


def _num(x: float) -> str:
    return str(int(x)) if float(x).is_integer() else f"{x:g}"


def rewarn(prop: dict, plan: dict, settings: dict, key: str) -> dict:
    """The proposal with each requirement's warnings worked out again from its teacher (the school may have
    changed one): "say which department for <subject>" when the row names none, no teacher when its
    department has none to give, a teacher the proposal takes past their allowance left (this level's own
    earlier output aside: on each requirement, in the proposal's order, from the one that goes past it,
    giving how far over the whole proposal takes them), and a teacher placed outside their subjects
    (`specialisation.outside`)."""
    out = copy.deepcopy(prop)
    key = SP.level_key(key)
    staff_by_id = {p["id"]: p for p in plan.get("staff") or []}
    names = {pid: str(p.get("name") or "").strip() or pid for pid, p in staff_by_id.items()}
    known = SP.plan_subjects(plan) | {norm(r["subject"]) for r in out["requirements"]}
    left = _allowance_left(plan, settings, key)
    total: dict[str, float] = {}
    for r in out["requirements"]:
        if r.get("teacher"):
            total[r["teacher"]] = total.get(r["teacher"], 0) + (r.get("periods") or 0)
    given: dict[str, float] = {}                # periods so far, in the proposal's order
    for r in out["requirements"]:
        r["warnings"] = []
        t = r.get("teacher")
        if not r.get("dept"):
            r["warnings"].append(f"say which department for {r['subject']}")
        elif not t:
            r["warnings"].append(f"no {r['dept']} teacher for {r['subject']}")
        if not t:
            continue
        given[t] = given.get(t, 0) + (r.get("periods") or 0)
        over = total[t] - left.get(t, 0)
        if given[t] > left.get(t, 0):                   # only from the requirement that takes them past it
            r["warnings"].append(f"{names.get(t, t)} would be {_num(over)} periods over allowance")
        as_req = {"subject": r["subject"], "level": key, "classes": r["classes"], "teachers": [t]}
        r["warnings"] += SP.outside(plan, as_req, staff_by_id=staff_by_id, known_subjects=known)
    return out


def propose(plan: dict, settings: dict, level_key: str, levels_entry: dict, students,
            setup: dict | None = None, *, count: int | None = None) -> dict:
    """A level's classes, requirements and suggested teachers, for the school to review:
    {level, classes: [{code, size, students: [sid]}], requirements: [{subject, classes, grouping, level,
    periods, room_kind, dept, size, teacher, suggested, warnings, lessons (only with a pattern), named
    (only on a teacher the deployment names)}],
    bands: [{name, requirements: [index]}] (only when a subject runs with others),
    warnings: [str]}.

    The student count is that of the level's students in `students` (their class's level, or their own
    while they have no class) when there are any, else the entry's `students_expected`; with neither,
    no classes and the warning "<level>: no students and no expected number". The classes are
    `class_count` of the class size (or `count`, the school's own number), coded by `class_codes`, filled
    by `fill`. When `setup` (the level's option-blocks setup: combinations and overrides) has combinations
    and the students carry choices, `blocks.place` gives the placement `fill` groups by. Requirements and
    bands are `_requirements`', with the teachers the deployment names (a Runs-with subject's groups sized
    from the level's students' choices); the rest are `_suggest`'s; `rewarn` gives
    each its warnings (a named teacher past allowance or outside their subjects among them)."""
    key = SP.level_key(level_key)
    entry = levels_entry or {}
    name = entry.get("name") or key
    warnings: list[str] = []
    mine = _level_students(plan, key, students)
    if mine:
        total = len(mine)
    elif entry.get("students_expected") is not None:
        total = int(entry["students_expected"])
    else:
        return {"level": key, "classes": [], "requirements": [],
                "warnings": [f"{name}: no students and no expected number"]}
    k = int(count) if count else class_count(total, entry.get("class_size") or 40)
    codes = class_codes(name, k, entry.get("naming"))
    if mine:
        filled = fill([s["id"] for s in mine], codes, _placement(mine, setup, name, warnings))
        classes = [{"code": c, "size": len(filled[c]), "students": filled[c]} for c in codes]
    else:
        classes = [{"code": c, "size": n, "students": []} for c, n in zip(codes, _sizes(total, k))]
    reqs, bands = _requirements(entry, key, classes, plan, warnings, mine)
    _suggest(reqs, plan, settings, key)
    prop = {"level": key, "classes": classes, "requirements": reqs, "warnings": warnings}
    if bands:
        prop["bands"] = bands
    return rewarn(prop, plan, settings, key)


def student_classes(prop: dict) -> dict[str, str]:
    """Each student's class in a proposal, {sid: code}."""
    return {sid: c["code"] for c in prop.get("classes") or [] for sid in c.get("students") or []}


# ---------------------------------------------------------------------------
# into the plan
# ---------------------------------------------------------------------------

def _label(r: dict) -> str:
    """A requirement as the school reads it: "English 1A", "PE 1A+1B"."""
    where = r.get("grouping") if r.get("grouping") not in (None, "", "class") else "+".join(r.get("classes") or [])
    return f"{r.get('subject')} {where}".strip()


def _list(items: list[str]) -> str:
    return ", ".join(items)


def apply_proposal(plan: dict, level_key: str, prop: dict, *, name: str | None = None) -> dict:
    """The plan with the proposal written in, normalised; neither input is changed.

    This level's earlier output (classes, division, requirements and bands tagged `formed` == the level key)
    goes first, and leaves any band that listed one of those requirements (a band it leaves empty goes too).
    Then the proposal's classes are added as {code, level, size}, one division over them, and each
    requirement, all tagged, its lessons the requirement's `lessons` (the lesson pattern) while they add up
    to its periods, else single periods; then one band over that division per proposal band (a Runs-with
    name), its options the requirements written for it, tagged too. `level`
    is `name`, the level's display name ("Sec 3", "P4", "Year Seven"; the key when not given), R27(a):
    every comparison of levels goes through `level_key`/`stage_key`, and the `formed` tag keeps the key.
    Everything else stays as it is, including option blocks' output for the level (it is applied again
    after the classes are formed).

    ValueError (codes, groupings and subject words only) when an earlier requirement of this level is
    locked, naming each; when a class code or a grouping is already used by other plan data (option blocks'
    output aside), naming each; when the proposal has no classes; or when it names a teacher the plan
    does not have."""
    key = SP.level_key(level_key)
    if prop.get("level") not in (None, key):
        raise ValueError(f"this proposal is for level {prop.get('level')}, not {key}")
    classes = prop.get("classes") or []
    if not classes:
        raise ValueError("the proposal has no classes to apply")
    locked = sorted(_label(r) for r in plan.get("requirements") or [] if r.get("formed") == key and r.get("locked"))
    if locked:
        one = len(locked) == 1
        raise ValueError(f"{_list(locked)} {'is' if one else 'are'} locked: unlock {'it' if one else 'them'} first")

    out = copy.deepcopy(plan)
    removed = {r["id"] for r in out.get("requirements") or [] if r.get("formed") == key}
    for section in ("classes", "divisions", "requirements"):
        out[section] = [x for x in out.get(section) or [] if x.get("formed") != key]
    bands = []
    for b in out.get("bands") or []:
        if b.get("formed") == key:
            continue
        options = [o for o in b.get("options") or [] if o not in removed]
        if len(options) == len(b.get("options") or []):
            bands.append(b)
        elif options:
            bands.append({**b, "options": options})
    out["bands"] = bands

    codes = [c["code"] for c in classes]
    if len({norm(c) for c in codes}) != len(codes):
        raise ValueError("the proposal names a class twice")
    others = [x for section in ("divisions", "requirements") for x in out[section] if not x.get("blocks")]
    used = {norm(c["code"]) for c in out["classes"]} | {norm(c) for x in others for c in x.get("classes") or []}
    clash = [c for c in codes if norm(c) in used]
    if clash:
        raise ValueError(f"{_list(clash)} {'is' if len(clash) == 1 else 'are'} already in the plan: "
                         f"rename or remove {'it' if len(clash) == 1 else 'them'} first")
    groupings = list(dict.fromkeys(r["grouping"] for r in prop.get("requirements") or []
                                   if r.get("grouping") not in (None, "", "class")))
    taken_groupings = {norm(r.get("grouping")) for r in out["requirements"]
                       if not r.get("blocks") and r.get("grouping") not in (None, "", "class")}
    clash = [g for g in groupings if norm(g) in taken_groupings]
    if clash:
        raise ValueError(f"the grouping{'' if len(clash) == 1 else 's'} {_list(clash)} "
                         f"{'is' if len(clash) == 1 else 'are'} already used in the plan")
    staff_ids = {p["id"] for p in out.get("staff") or []}
    unknown = sorted({r["teacher"] for r in prop.get("requirements") or [] if r.get("teacher")} - staff_ids)
    if unknown:
        raise ValueError(f"{_list(unknown)} {'is' if len(unknown) == 1 else 'are'} not on the staff list")

    shown = " ".join(str(name or "").split()) or key
    out["classes"] += [{"code": c["code"], "level": shown, "size": c["size"], "formed": key} for c in classes]
    division_id = M.unique_id(M.division_id(codes), {d["id"] for d in out["divisions"]})
    out["divisions"].append({"id": division_id, "classes": list(codes), "formed": key})
    taken = {r["id"] for r in out["requirements"]}
    written: dict[int, str] = {}                # proposal index -> the requirement id written for it
    for i, r in enumerate(prop.get("requirements") or []):
        dept, periods, lessons = r.get("dept") or "", int(r["periods"]), r.get("lessons")
        rid = M.requirement_id_for(dept, key, r["grouping"], r["classes"], r["subject"], taken)
        taken.add(rid)
        written[i] = rid
        out["requirements"].append({
            "id": rid, "dept": dept, "level": shown, "subject": r["subject"], "periods": periods,
            "lessons": dict(lessons if lessons and _total(lessons) == periods else {"1": periods}),
            "grouping": r["grouping"],
            "classes": list(r["classes"]),
            "teachers": [r["teacher"]] if r.get("teacher") else [], "size": r.get("size"),
            "venue": {"kind": r.get("room_kind"), "room": None}, "sync_with": None, "locked": False,
            "formed": key})
    band_ids = {b["id"] for b in out["bands"]}
    for band in prop.get("bands") or []:        # a Runs-with name: its groups side by side
        options = [written[i] for i in band.get("requirements") or [] if i in written]
        if not options:
            continue
        bid = M.unique_id(M.band_id(division_id, str(band["name"])), band_ids)
        band_ids.add(bid)
        out["bands"].append({"id": bid, "division": division_id, "options": options, "formed": key})
    return M.normalise(out)


def _combined(plan: dict, key: str) -> list[dict]:
    """This level's formed requirements taught to two or more classes together ("1A+1B"): each is an option
    group in generate and B1, so its students need a take for it. A Runs-with group (one a band of this
    level's lists) is not one: only some of the classes' students take it."""
    grouped = {r["id"] for r in _run_groups(plan, key)}
    return [r for r in plan.get("requirements") or []
            if r.get("formed") == key and r.get("grouping") not in (None, "", "class") and r["id"] not in grouped]


def _run_groups(plan: dict, key: str) -> list[dict]:
    """This level's Runs-with groups: the formed requirements a band of this level's lists, in the bands'
    id order, each band's in its options' order (the proposal's: A, B, C per subject)."""
    by_id = {r["id"]: r for r in plan.get("requirements") or [] if r.get("formed") == key}
    return [by_id[o] for b in sorted(plan.get("bands") or [], key=lambda b: b["id"]) if b.get("formed") == key
            for o in b.get("options") or [] if o in by_id]


def _run_takes(students: list[dict], placed: dict[str, str], after: dict, key: str) -> dict[str, list[dict]]:
    """{sid: [take]}: each placed student who lists a Runs-with subject (`_lists`, as `taking` counts them)
    gets one take of exactly one of its groups. A subject's takers, in student-ID order, fill its groups in
    order to the sizes `_sizes` gives (the proposal's own), the larger groups first. Counts only."""
    by_subject: dict[str, list[dict]] = {}
    for r in _run_groups(after, key):
        by_subject.setdefault(r["subject"], []).append(r)
    mine = sorted((s for s in students if str(s.get("id")) in placed), key=lambda s: _id_key(str(s["id"])))
    out: dict[str, list[dict]] = {}
    for subject, groups in by_subject.items():
        takers = [str(s["id"]) for s in mine if _lists(s, subject)]
        queue = iter(takers)
        for r, n in zip(groups, _sizes(len(takers), len(groups))):
            take = {"division": option_division(after, r), "grouping": r["grouping"]}
            for _ in range(n):
                out.setdefault(next(queue), []).append(dict(take))
    return out


def students_after(students: list[dict], before: dict, after: dict, level_key: str, prop: dict) -> list[dict]:
    """The students once `after` (the plan `apply_proposal` wrote from `before`) is stored: each student the
    proposal places gets its class, and loses the takes of this level's earlier combined and Runs-with groups
    (in `before`) and gains one take {division, grouping} per combined requirement of its new class in
    `after`, and one of one group of each Runs-with subject it lists (`_run_takes`), its division
    `generate.option_division`, so it is exactly the option group the plan has. A student holding an earlier
    such take but not placed loses it only. Every other student, and every other take
    (option blocks' sets, another level's), is as it was; `placed_by_app` is not touched (these takes follow
    from the class, not a choice). A new list; the one given is not changed.

    Known limit: B1's `_index` sees only the first subject of a group, so two combined subjects that share
    one pairing ("1A+1B" for PE and Music) read as one group in B1's subject lookups."""
    key = SP.level_key(level_key)
    placed = student_classes(prop)
    old = {option_group_id(before, r) for r in _combined(before, key) + _run_groups(before, key)}
    runs = _run_takes(students, placed, after, key)
    by_class: dict[str, list[dict]] = {}
    for r in _combined(after, key):
        take = {"division": option_division(after, r), "grouping": r["grouping"]}
        for code in r.get("classes") or []:
            if take not in by_class.setdefault(code, []):
                by_class[code].append(take)
    out = []
    for s in students:
        code = placed.get(str(s.get("id")))
        takes = list(s.get("takes") or [])
        keep = [t for t in takes if group_id(t) not in old]
        if code is None and len(keep) == len(takes):
            out.append(s)
            continue
        new = {**s, "takes": keep}
        if code is not None:
            new["class"] = code
            new["takes"] = keep + [dict(t) for t in by_class.get(code, []) if t not in keep]
            new["takes"] += [t for t in runs.get(str(s.get("id")), []) if t not in new["takes"]]
        out.append(new)
    return out
