"""Subject specialisation: what a teacher can teach, and at which levels (spec
docs/superpowers/specs/2026-10-20-csv-pack-design.md §1 and §9).

A staff member may carry `subjects`: [{subject, levels}], where `levels` is a list of `level_key`
values and [] means every level. A staff member without `subjects` has no opinion, so a school that
never fills this in sees no warning and no change. Everything here is a warning, never a refusal.

The text form, in files and in the Manpower editor, is semicolon-separated subjects with optional
levels in brackets: "Physics (Sec 1–2); Chemistry; Science (Sec 1, Sec 2)".

No student data is read here; the problem texts carry staff names and subject and level words."""
from __future__ import annotations

import re

from ..students import level_of, norm

# ---------------------------------------------------------------------------
# the one level rule
# ---------------------------------------------------------------------------

# School stages by how a level is written. Secondary is the default stage, so it has no prefix in a
# key: "Sec 3", "S3" and "3" are all "3". "Primary 4" and "P4" are "p4"; "JC1" is "jc1".
_STAGES = {"": "", "s": "", "sec": "", "secondary": "",
           "p": "p", "pri": "p", "primary": "p",
           "jc": "jc", "j": "jc", "junior": "jc", "juniorcollege": "jc"}
_STAGE_LABEL = {"": "Sec ", "p": "P", "jc": "JC"}
_NUMBERED = re.compile(r"^([a-z]*)0*(\d+)$")        # on norm(text): letters, then a number
_STREAMED = re.compile(r"^0*(\d+)[a-z]")             # on norm(text): a level number, then its stream ("1g3")


def _stage_number(text) -> tuple[str, int] | None:
    """(stage, number) when `text` is a stage word (or none) followed by a number, else None."""
    m = _NUMBERED.match(norm(text))
    if not m or m.group(1) not in _STAGES:
        return None
    return _STAGES[m.group(1)], int(m.group(2))


def level_key(text) -> str:
    """The one rule for matching a level, used everywhere. A numbered level is its number together
    with its school stage when one is written: "Sec 3", "S3", "3", "sec3" -> "3"; "Primary 4", "P4"
    -> "p4"; "JC1" -> "jc1". A level number followed by a stream is that number, as the board's level
    tabs read it (board.level_tab): "1G3", "4 Express" -> "1", "4". A level with no number is its
    `students.norm` ("Year Seven" -> "yearseven")."""
    found = _stage_number(text)
    if found is None:
        streamed = _STREAMED.match(norm(text))
        return str(int(streamed.group(1))) if streamed else norm(text)
    stage, number = found
    return f"{stage}{number}"


_STAGED = re.compile(r"^([a-z]*)0*(\d+)(.*)$")    # on norm(text): letters, a number, then anything (a stream)


def stage_key(text) -> str:
    """A level as option blocks keys a year (R26, re-ruling R19): only the stage spelling is folded, a
    stream is never stripped. "Sec 3", "S3" and "3" -> "3"; "3E" -> "3e", "Sec 3E" -> "3e", "3G2" ->
    "3g2"; "P4" -> "p4"; a level with no number is its `students.norm` ("Year Seven" -> "yearseven").
    `level_key` (which strips the stream, R8) stays the rule for specialisation and the levels store."""
    t = norm(text)
    m = _STAGED.match(t)
    if not m or m.group(1) not in _STAGES:
        return t
    return f"{_STAGES[m.group(1)]}{int(m.group(2))}{m.group(3)}"


def class_prefix(level_text) -> str:
    """What a level's form classes are named from: a secondary level's number ("Sec 3", "S3", "3" ->
    "3", so 3A, 3B); another stage keeps its letters ("Primary 4", "P4" -> "P4"; "JC1" -> "JC1"), so
    Sec 1 and JC1 never both make 1A; a level with no number uses its own name ("Year Seven" ->
    "Year Seven", so Year SevenA...)."""
    text = " ".join(str(level_text or "").split())
    found = _stage_number(text)
    if found is None:
        return text
    stage, number = found
    return f"{_STAGE_LABEL[stage] if stage else ''}{number}"


def _label(key: str) -> str:
    """A level key as a person writes it: "1" -> "Sec 1", "p4" -> "P4", "jc1" -> "JC1"; the key of a
    level with no number stands as it is."""
    m = _NUMBERED.match(key)
    if m and m.group(1) in _STAGE_LABEL:
        return f"{_STAGE_LABEL[m.group(1)]}{int(m.group(2))}"
    return key


# ---------------------------------------------------------------------------
# the text form
# ---------------------------------------------------------------------------

_DASH = re.compile(r"\s*[–—-]\s*")        # en dash, em dash, hyphen
RANGE_MAX = 20                             # levels one range may name: past it, a slip ("Sec 1–99")


def _split_top(text: str, sep: str) -> list[str]:
    """`text` cut at `sep` outside brackets, so a bracket may hold commas."""
    parts, depth, current = [], 0, []
    for ch in text:
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth = max(0, depth - 1)
        if ch == sep and depth == 0:
            parts.append("".join(current))
            current = []
        else:
            current.append(ch)
    parts.append("".join(current))
    return parts


def _levels_of(bracket: str, subject: str) -> tuple[list[str], str | None]:
    """The level keys a bracket names, or (levels, problem). A range such as "Sec 1–2" or
    "Sec 1-Sec 3" expands to every numbered level between."""
    keys: list[str] = []
    for piece in bracket.split(","):
        piece = piece.strip()
        if not piece:
            return [], f"{subject}: a level is missing in ({bracket.strip()})"
        ends = [e for e in _DASH.split(piece) if e] if _DASH.search(piece) else [piece]
        if len(ends) == 1:
            new = [level_key(piece)]
        elif len(ends) > 2 and all(_stage_number(e) for e in ends):
            return [], f"{subject}: {piece!r} has more than one dash; write a range as its two ends (Sec 1–3)"
        elif len(ends) == 2 and all(_stage_number(e) for e in ends):
            (s1, n1), (s2, n2) = _stage_number(ends[0]), _stage_number(ends[1])
            if re.fullmatch(r"\d+", ends[1].strip()):
                s2 = s1                         # "P4–6": the end takes the start's stage
            if s1 != s2:
                return [], f"{subject}: {piece!r} runs between different stages"
            if n1 > n2:
                return [], f"{subject}: {piece!r} runs backwards, from {n1} down to {n2}"
            if n2 - n1 + 1 > RANGE_MAX:
                return [], f"{subject}: {piece!r} names {n2 - n1 + 1} levels; a range may name at most {RANGE_MAX}"
            new = [f"{s1}{n}" for n in range(n1, n2 + 1)]
        elif all(_stage_number(e) or _STREAMED.match(norm(e)) for e in ends):
            # "1G3–1G4": read as a name it would become level 1 (a stream is its level), silently
            return [], f"{subject}: {piece!r} runs between streams; write the levels (Sec 1–2) or list them"
        else:
            # a dash in a level's own name ("Year-Seven"), not a range
            new = [level_key(piece)]
        keys += [k for k in new if k not in keys]
    return list(dict.fromkeys(keys)), None


def parse_subjects(text) -> tuple[list[dict], list[str]]:
    """(entries, problems) for the text form. An entry is {subject, levels}; `levels` is a list of
    `level_key` values and [] means every level. An entry that cannot be read is not kept, and says
    why in the problems, so nothing is dropped silently."""
    entries: list[dict] = []
    problems: list[str] = []
    for raw in _split_top(str(text or ""), ";"):
        part = raw.strip()
        if not part:
            continue
        m = re.fullmatch(r"([^()]*)\((.*)\)", part, re.S)
        if m:
            subject, bracket = " ".join(m.group(1).split()), m.group(2)
        elif "(" in part or ")" in part:
            problems.append(f"{part!r}: the bracket is not closed or not placed after the subject")
            continue
        else:
            subject, bracket = " ".join(part.split()), None
        if not subject:
            problems.append(f"{part!r}: the subject is missing")
            continue
        if bracket is None:
            entries.append({"subject": subject, "levels": []})
            continue
        if not bracket.strip():
            problems.append(f"{subject}: the brackets are empty; leave them out for every level")
            continue
        levels, problem = _levels_of(bracket, subject)
        if problem:
            problems.append(problem)
        else:
            entries.append({"subject": subject, "levels": levels})
    return entries, problems


def _runs(keys: list[str]) -> list[str]:
    """Level keys as text, a run of consecutive numbers of one stage as a range ("Sec 1–3")."""
    out, i = [], 0
    while i < len(keys):
        j = i
        a = _stage_number(keys[i]) if _NUMBERED.match(keys[i]) else None
        while a and j + 1 < len(keys) and _NUMBERED.match(keys[j + 1]):
            b, c = _stage_number(keys[j]), _stage_number(keys[j + 1])
            if not (c and b and c[0] == b[0] and c[1] == b[1] + 1):
                break
            j += 1
        if j == i:
            out.append(_label(keys[i]))
        else:       # "Sec 1–3" for the default stage, "P4–P6" for another
            end = _label(keys[j])[len(_STAGE_LABEL[""]):] if a[0] == "" else _label(keys[j])
            out.append(f"{_label(keys[i])}–{end}")
        i = j + 1
    return out


def format_subjects(entries) -> str:
    """The text form of a teacher's subjects; `parse_subjects(format_subjects(e))[0] == e`."""
    parts = []
    for entry in entries or []:
        levels = list(dict.fromkeys(level_key(level) for level in entry.get("levels") or []))
        subject = str(entry.get("subject") or "").strip()
        parts.append(f"{subject} ({', '.join(_runs(levels))})" if levels else subject)
    return "; ".join(parts)


# ---------------------------------------------------------------------------
# matching a teacher to a subject at a level
# ---------------------------------------------------------------------------

def _entries_for(staff: dict, subject) -> list[dict]:
    return [e for e in staff.get("subjects") or [] if norm(e.get("subject")) == norm(subject)]


def _covers_level(entry: dict, level) -> bool:
    levels = entry.get("levels") or []
    key = level_key(level)
    # an entry for every level covers any; a requirement whose level is not known cannot be judged
    return not levels or not key or key in {level_key(x) for x in levels}


def covers(staff: dict, subject, level) -> bool | None:
    """Whether `staff` may teach `subject` at `level`: None when they list no subjects (no opinion),
    else True when some entry has the subject (ignoring capitals and spaces) and covers the level.
    A level that is not known is not held against anyone."""
    if not staff.get("subjects"):
        return None
    return any(_covers_level(e, level) for e in _entries_for(staff, subject))


# ---------------------------------------------------------------------------
# checks against the plan
# ---------------------------------------------------------------------------

def _name(staff: dict) -> str:
    return str(staff.get("name") or "").strip() or str(staff.get("id") or "")


def staff_problems(plan: dict, levels: dict | None = None) -> list[dict]:
    """{kind, staff, text} for each subject a teacher lists that no requirement has ("unknown_subject")
    and each level that the plan does not know ("unknown_level"). They are listed, never read as
    "can't teach". A subject is known from a requirement or a subject row of the timetable's `levels`
    store when it is given; a level from a class, a requirement's own level, or a key of that store
    (R27(e)). Nothing is checked until some subject is known (subjects) or some level is (levels)."""
    requirements = plan.get("requirements") or []
    subjects = (plan_subjects(plan) | level_subjects(levels)) - {""}
    levels = ({level_key(level_of(plan, c["code"])) for c in plan.get("classes") or []}
              | {level_key(r.get("level")) for r in requirements}
              | {level_key(k) for k in levels or {}}) - {""}
    out = []
    for person in plan.get("staff") or []:
        who = _name(person)
        for entry in person.get("subjects") or []:
            subject = entry.get("subject")
            if subjects and norm(subject) not in subjects:
                out.append({"kind": "unknown_subject", "staff": person["id"],
                            "text": f"{who} lists {subject}, which no requirement in the plan has"})
            if not levels:
                continue
            for level in entry.get("levels") or []:
                if level_key(level) not in levels:
                    out.append({"kind": "unknown_level", "staff": person["id"],
                                "text": f"{who} lists {subject} at {_label(level_key(level))}, "
                                        f"which no class in the plan has"})
    return out


def _requirement_levels(plan: dict, req: dict) -> list[str]:
    """The level texts a requirement is taught at: its own `level`, else those of its classes."""
    own = str(req.get("level") or "").strip()
    if own:
        return [own]
    seen = {}
    for code in req.get("classes") or []:
        level = level_of(plan, code)
        if level:
            seen.setdefault(level_key(level), level)
    return list(seen.values())


def _only(entries: list[dict]) -> str:
    """"up to Sec 2" for levels 1..k of numbered levels (k of 2 or more), else "at Sec 1, Sec 3"."""
    keys = list(dict.fromkeys(level_key(x) for e in entries for x in e.get("levels") or []))
    numbers = sorted(int(k) for k in keys if k.isdigit())
    if len(keys) >= 2 and numbers == list(range(1, len(keys) + 1)) and len(numbers) == len(keys):
        return f"up to {_label(str(len(keys)))}"
    return "at " + ", ".join(_label(k) for k in keys)


def plan_subjects(plan: dict) -> set[str]:
    """The plan's subjects as `students.norm` keys: those its requirements teach."""
    return {norm(r.get("subject")) for r in plan.get("requirements") or []} - {""}


def level_subjects(levels: dict | None) -> set[str]:
    """The subjects of the timetable's `levels` store (requirements.csv before any class is formed), as
    `students.norm` keys."""
    return {norm(s.get("subject")) for lv in (levels or {}).values() if isinstance(lv, dict)
            for s in lv.get("subjects") or [] if isinstance(s, dict)} - {""}


def outside(plan: dict, req: dict, *, staff_by_id: dict | None = None,
            known_subjects: set | None = None) -> list[str]:
    """A warning for each teacher on `req` who lists subjects and is placed outside them: the subject
    is not one of theirs, or not at this level. Teachers who list no subjects give none. A teacher who
    does not teach the subject and whose own list has subjects matching nothing the plan teaches hears
    which ("… — their list has Maths, which matches no subject"): likely a spelling, not a fact.

    `staff_by_id` ({id: staff}) saves a caller checking every requirement from indexing the staff
    again. `known_subjects` is what counts as a subject (default `plan_subjects(plan)`; a caller may
    pass more, such as an option block's subjects); `req`'s own subject always counts."""
    staff = staff_by_id if staff_by_id is not None else {p["id"]: p for p in plan.get("staff") or []}
    subject = str(req.get("subject") or "").strip()
    levels = _requirement_levels(plan, req) or [""]
    known: set[str] | None = None
    out = []
    for tid in req.get("teachers") or []:
        person = staff.get(tid)
        if person is None:
            continue
        results = [covers(person, subject, level) for level in levels]
        if None in results or all(results):
            continue
        entries = _entries_for(person, subject)
        if entries:
            out.append(f"{_name(person)} teaches {subject} only {_only(entries)}")
            continue
        if known is None:
            known = {norm(x) for x in (plan_subjects(plan) if known_subjects is None else known_subjects)}
            known.add(norm(subject))
        unknown = list(dict.fromkeys(str(e.get("subject") or "").strip() for e in person.get("subjects") or []
                                     if norm(e.get("subject")) not in known))
        text = f"{_name(person)} does not teach {subject}"
        if unknown:
            text += f" — their list has {', '.join(unknown)}, which match{'es' if len(unknown) == 1 else ''} no subject"
        out.append(text)
    return out
