"""What applying one of the five CSV files writes (csv-pack spec docs/superpowers/specs/2026-10-20-csv-pack-design.md
§2, and "Calls made by the designer" §2 and §4), as pure functions shared by the reading card (its counts)
and Apply (the write), so the card says exactly what Apply does.

- staff.csv updates the plan's staff by name (`students.norm`), venues.csv its rooms by room name; anyone
  or any room the file leaves out is kept, never deleted.
- requirements.csv replaces the subject rows of the levels it names; each level keeps its name, expected
  students, class size and naming.
- the level students list replaces the students of the levels it names (and any stored student with one
  of its IDs); students of other levels are untouched.
- a combination code stays with its subject set (R23): `remapped_codes` names each stored code that new
  combinations would make name other subjects while a student's choices or an override use it; the card
  lists it and Apply refuses.

Pure: no database, no logging."""
from __future__ import annotations

import copy
import re

from ... import blocks as BK, levels as LV, students as ST
from .. import model as M
from ..specialisation import level_key

STAFF_ID_MAX = 31           # staff ids become organisation person ids (model.slug's budget, as the board's)


def _norm(text) -> str:
    return ST.norm(text)


def initials(name: str) -> str:
    """A new teacher's Short when the file leaves it blank: the first letter of each word of their name."""
    return "".join(w[0] for w in re.findall(r"[^\W\d_]+", str(name or ""))).upper()


def merge_staff(staff: list[dict], items: list[dict]) -> tuple[list[dict], dict]:
    """(the plan's staff with the file's teachers merged in, {added, changed, missing: [names]}). A teacher
    matches by name. A match takes the file's Load (or Load factor, which clears the allowance); a blank
    Short, Department or Subjects cell keeps theirs (R24; R4 for subjects, as for the workbook). A new
    teacher gets an id as the board's Add teacher gives one, and the initials of their name when Short is
    blank. Nobody is removed."""
    out = [copy.deepcopy(s) for s in staff]
    at: dict[str, int] = {}
    for i, s in enumerate(out):
        at.setdefault(_norm(s.get("name")), i)
    taken = {s.get("id") for s in out}
    added = changed = 0
    seen: set[str] = set()
    for item in items:
        key = _norm(item["name"])
        seen.add(key)
        i = at.get(key)
        if i is None:
            person = {"id": M.unique_id(M.plan_slug(item["name"]), taken, STAFF_ID_MAX), "name": item["name"],
                      "short": item.get("short") or initials(item["name"]), "dept": item.get("dept") or "",
                      "load_factor": item.get("load_factor", 1.0), "allowance": item.get("allowance"),
                      "avail": None, "max_periods_day": None}
            if item.get("subjects"):
                person["subjects"] = copy.deepcopy(item["subjects"])
            taken.add(person["id"])
            out.append(person)
            at[key] = len(out) - 1
            added += 1
            continue
        old = out[i]
        new = dict(old)
        if item.get("short"):
            new["short"] = item["short"]
        if item.get("dept"):
            new["dept"] = item["dept"]
        if "allowance" in item:
            new["allowance"] = item["allowance"]
        elif "load_factor" in item:
            new["load_factor"], new["allowance"] = item["load_factor"], None
        if item.get("subjects"):
            new["subjects"] = copy.deepcopy(item["subjects"])
        if new != old:
            changed += 1
        out[i] = new
    missing = [s.get("name") or s.get("id") for s in staff if _norm(s.get("name")) not in seen]
    return out, {"added": added, "changed": changed, "missing": missing}


def merge_venues(venues: list[dict], items: list[dict]) -> tuple[list[dict], dict]:
    """(the plan's rooms with the file's merged in, {added, changed, missing: [names]}). A room matches by
    name, else by its id; a match takes the file's Kind and Capacity when the file gives them. A new room
    gets the id of its name. No room is removed."""
    out = [copy.deepcopy(v) for v in venues]
    by_name = {_norm(v.get("name")): i for i, v in enumerate(out)}
    by_id = {v.get("id"): i for i, v in enumerate(out)}
    taken = set(by_id)
    added = changed = 0
    seen: set[int] = set()
    for item in items:
        i = by_name.get(_norm(item["name"]))
        if i is None:
            i = by_id.get(M.venue_id(item["name"]))
        if i is None:
            vid = M.unique_id(M.venue_id(item["name"]), taken)
            taken.add(vid)
            out.append({"id": vid, "name": item["name"], "block": None, "level": None,
                        "capacity": item.get("capacity"), "kind": item.get("kind")})
            seen.add(len(out) - 1)
            added += 1
            continue
        seen.add(i)
        old = out[i]
        new = dict(old)
        for field in ("kind", "capacity"):
            if item.get(field) is not None:
                new[field] = item[field]
        if new != old:
            changed += 1
        out[i] = new
    missing = [v.get("name") for i, v in enumerate(venues) if i not in seen]
    return out, {"added": added, "changed": changed, "missing": missing}


def merge_levels(levels: dict, found: dict) -> tuple[dict, dict]:
    """(the levels store with each level of `found` (read from requirements.csv) taking the file's subject
    rows, {added, changed}). A level already stored keeps its name, expected students, class size and
    naming; a new one gets the file's spelling and the defaults. Levels not in the file are untouched."""
    out = copy.deepcopy(levels)
    added = changed = 0
    for key, entry in found.items():
        old = out.get(key)
        new = {"name": old["name"] if old else entry["name"],
               "students_expected": old["students_expected"] if old else None,
               "class_size": old["class_size"] if old else LV.DEFAULT_CLASS_SIZE,
               "naming": old["naming"] if old else "",
               "subjects": copy.deepcopy(entry["subjects"])}
        if old is None:
            added += 1
        elif new != old:
            changed += 1
        out[key] = new
    return out, {"added": added, "changed": changed}


def student_level(plan: dict, student: dict) -> str:
    """A stored student's level as a `level_key`: their class's level, or their own when they have no class."""
    return level_key(ST.level_of(plan, student))


def merge_students(students: list, items: list[dict], plan: dict) -> tuple[list, dict]:
    """(the stored students with those of the file's levels (and any with one of the file's IDs) replaced
    by the file's, {added, changed, removed}). Students of other levels keep their place, first."""
    levels = {level_key(s.get("level")) for s in items}
    ids = {s["id"] for s in items}
    before = {s.get("id"): s for s in students if isinstance(s, dict)}
    kept = [s for s in students
            if not isinstance(s, dict) or (s.get("id") not in ids and student_level(plan, s) not in levels)]
    gone = sum(1 for s in students if isinstance(s, dict) and s.get("id") not in ids
               and student_level(plan, s) in levels)
    added = sum(1 for s in items if s["id"] not in before)
    changed = sum(1 for s in items if s["id"] in before and before[s["id"]] != s)
    return [*copy.deepcopy(kept), *copy.deepcopy(items)], {"added": added, "changed": changed, "removed": gone}


def _in_year(plan: dict, student: dict, key: str) -> bool:
    """Whether a stored student belongs to year key `key` ("level:3", "classes:3A,3B"): by their level's
    canonical year key (their class's level, or their own while they have no class), or by their class."""
    kind, _, rest = BK.canonical_key(key).partition(":")
    if kind == "classes":
        return str(student.get("class") or "") in rest.split(",")
    return BK.setup_key_for(ST.level_of(plan, student)) == f"level:{rest}"


def remapped_codes(old: list, new: list, students: list, plan: dict, key: str, overrides) -> list[dict]:
    """[{code, old, new, choices, overrides}] for each code of `new` (in its order) that a stored combination
    of `old` has with a different subject set (`students.norm` on each subject) while some student of year
    `key` chooses it or an override (`overrides`, {sid: code}) names it: `choices` and `overrides` are counts,
    never a student. A code `new` drops or keeps with its subjects is not listed."""
    def subjects(c) -> frozenset:
        return frozenset(ST.norm(s) for s in c.get("subjects") or [])
    stored = {ST.norm(c.get("code")): c for c in old or [] if isinstance(c, dict) and c.get("code")}
    chosen: dict[str, int] = {}
    for s in students or []:
        if isinstance(s, dict) and _in_year(plan, s, key):
            for code in dict.fromkeys(ST.norm(c) for c in s.get("choices") or []):
                chosen[code] = chosen.get(code, 0) + 1
    named: dict[str, int] = {}
    for code in (overrides or {}).values() if isinstance(overrides, dict) else ():
        named[ST.norm(code)] = named.get(ST.norm(code), 0) + 1
    out = []
    for c in new or []:
        code = ST.norm(c.get("code"))
        was = stored.get(code)
        if was is None or subjects(was) == subjects(c) or not (chosen.get(code) or named.get(code)):
            continue
        out.append({"code": c["code"], "old": list(was.get("subjects") or []), "new": list(c.get("subjects") or []),
                    "choices": chosen.get(code, 0), "overrides": named.get(code, 0)})
    return out


REMAP_HELP = "keep each code's subjects or leave Code empty"


def remap_doubt(item: dict) -> str:
    """A card's doubt for one `remapped_codes` item: the code, both subject sets and the counts using it."""
    whose = "student's" if item["choices"] == 1 else "students'"
    uses = [*([f"{item['choices']:,} {whose} choices"] if item["choices"] else []),
            *([f"{item['overrides']:,} override{'' if item['overrides'] == 1 else 's'}"] if item["overrides"] else [])]
    verb = "uses" if not item["choices"] and item["overrides"] == 1 else "use"
    return (f"{item['code']} would name {', '.join(item['new'])} instead of {', '.join(item['old'])} while "
            f"{' and '.join(uses)} {verb} it; {REMAP_HELP}")


def remap_refusal(items: list[dict]) -> str:
    """Apply's 409 for `remapped_codes` items: the codes, never a student."""
    codes = sorted(dict.fromkeys(i["code"] for i in items), key=ST._natural)
    shown = codes[0] if len(codes) == 1 else f"{', '.join(codes[:-1])} and {codes[-1]}"
    return (f"{shown} would name other subjects while students' choices or overrides use "
            f"{'it' if len(codes) == 1 else 'them'}; {REMAP_HELP}")
