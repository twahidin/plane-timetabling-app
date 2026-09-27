"""The curriculum plan document (spec docs/superpowers/specs/2026-09-19-curriculum-plan-design.md §2):
staff, classes, divisions, requirements and bands, plus the rules that generation always applies.
Ids are slugs derived from content so a re-import merges rather than duplicates."""
from __future__ import annotations

import copy
import re

from ..asc_import import slug as _slug

PLAN_VERSION = 1

# The plan is the school's own document and never reaches the engine, so its ids have a longer
# budget than the organisation's 31 characters (intake.ID_PATTERN): a requirement that names six
# classes needs the room. plan.generate fits them back to the organisation's pattern.
PLAN_ID_MAX = 63
PLAN_ID_PATTERN = re.compile(r"^[a-z0-9][a-z0-9-]{0,62}$")

_ID_BAD = re.compile(r"[^a-z0-9-]+")          # the character rules of asc_import.slug

slug = _slug                                  # 31 characters: ids that become organisation ids


def plan_slug(text: str) -> str:
    """asc_import.slug's character rules with the plan's longer budget. A requirement or division
    that names six classes runs past 31 characters, and truncating there would make two divisions
    that differ only in their last class collide; staff ids keep the shorter `slug` because
    generation copies them into the organisation as person ids unchanged."""
    s = _ID_BAD.sub("-", text.lower()).strip("-")
    s = re.sub(r"-+", "-", s)
    return (s or "x")[:PLAN_ID_MAX]


_LESSON_LENGTHS = ("1", "2", "3", "4")

# A plan lesson is not capped at a quadruple period any more (a 12-hour ward shift is one 12-slot
# lesson): any positive integer count of slots up to a working day and a half is accepted. The
# four defaults above still always appear in `lessons` (padded to zero) so every requirement built
# from a deployment workbook keeps its familiar shape; a duty's own length is simply an extra key.
LESSON_LENGTH_MAX = 48

# The most lessons one requirement may hold (normalise_lessons). split_lessons searches exhaustively,
# and a pattern past this many lessons is not a teaching requirement.
SPLIT_LESSONS_MAX = 200

# The plan's own vocabulary: the words the Plan tab, issues and printouts use for a person, a
# group, a requirement and a venue. Education's are the default; the start wizard sets its own
# for every other domain (spec docs/superpowers/specs/2026-09-20-start-wizard-design.md §5), and a
# re-import must not reset a vocabulary the user (or the wizard) already chose (`merge_import`).
DEFAULT_VOCABULARY = {"person": "teacher", "group": "class", "requirement": "lesson", "venue": "room"}


def vocabulary_of(db) -> dict:
    """The timetable's words: the stored plan's vocabulary over the defaults (education's)."""
    plan = db.get_value("plan") or {}
    v = plan.get("vocabulary") or {}
    return {k: (v.get(k) or d) for k, d in DEFAULT_VOCABULARY.items()}


class PlanError(ValueError):
    pass


def empty_plan() -> dict:
    return {
        "version": PLAN_VERSION,
        "staff": [],
        "classes": [],
        "divisions": [],
        "requirements": [],
        "bands": [],
        "rules": {"edge_subjects": [], "no_double_across_rest": True, "pinned": []},
        "vocabulary": dict(DEFAULT_VOCABULARY),
        "source": None,
    }


def requirement_id(dept: str, level: str | None, grouping: str | None, classes: list[str]) -> str:
    parts = [dept or "", level or "", grouping or "class", *classes]
    return plan_slug("-".join(str(p) for p in parts))


def unique_id(wanted: str, taken, limit: int = PLAN_ID_MAX) -> str:
    """`wanted` (trimmed to `limit`), or `<wanted>-2`, `-3`, ... when `taken` already holds it."""
    wanted = wanted[:limit].rstrip("-") or "x"
    n = 1
    candidate = wanted
    while candidate in taken:
        n += 1
        suffix = f"-{n}"
        candidate = wanted[:limit - len(suffix)].rstrip("-") + suffix
    return candidate


def requirement_id_for(dept: str, level: str | None, grouping: str | None, classes: list[str],
                       subject: str | None, taken) -> str:
    """The id a requirement gets where some ids are already `taken`: `requirement_id`, or, when that
    is taken (a second subject for the same department, level and classes: E Math and A Math for
    1A2), the subject-qualified `<dept>-<level>-<subject>-<grouping>-<classes>`, kept unique with
    -2, -3, ... The board's Add subject and both workbook readers name a second subject this way,
    so it comes back through the workbook with the id it went out with."""
    plain = requirement_id(dept, level, grouping, classes)
    if plain not in taken:
        return plain
    parts = [dept or "", level or "", subject or "", grouping or "class", *classes]
    return unique_id(plan_slug("-".join(str(p) for p in parts)), taken)


def division_id(classes: list[str]) -> str:
    return plan_slug("-".join(sorted((str(c) for c in classes), key=str.lower)))


def band_id(division: str, family: str) -> str:
    return plan_slug(f"{division}-{family}")


# ---------------------------------------------------------------------------
# normalise
# ---------------------------------------------------------------------------

def _check_id(value, section: str) -> str:
    if not isinstance(value, str) or not PLAN_ID_PATTERN.match(value):
        raise PlanError(f"{section}: id {value!r} must be 1-{PLAN_ID_MAX} lowercase letters, digits or hyphens")
    return value


def _as_list(value, name: str) -> list:
    if value is None:
        return []
    if not isinstance(value, list):
        raise PlanError(f"{name} must be a list")
    return list(value)


def as_dict(value, name: str) -> dict:
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise PlanError(f"{name} must be an object")
    return value


def _whole_number(value, name: str) -> int:
    """A count of periods a person typed or a spreadsheet stored (30, 30.0, "30"): a non-negative
    whole number, or PlanError naming the field."""
    try:
        number = float(value)
    except (TypeError, ValueError):
        raise PlanError(f"{name}: {value!r} must be a whole number of periods")
    if not number.is_integer() or number < 0:
        raise PlanError(f"{name}: {value!r} must be a whole number of periods, 0 or more")
    return int(number)


_FLAG_WORDS = {"true": True, "yes": True, "false": False, "no": False}


def _flag(value, name: str) -> bool:
    """A yes/no field: a real boolean, or the words true/false/yes/no; absent is no. Anything else
    is refused, so a string such as "false" can never read as a lock."""
    if value is None:
        return False
    if isinstance(value, bool):
        return value
    if isinstance(value, str) and value.strip().lower() in _FLAG_WORDS:
        return _FLAG_WORDS[value.strip().lower()]
    raise PlanError(f"{name}: {value!r} must be true or false")


def _normalise_reductions(raw, person_id: str) -> list[dict]:
    out = []
    for i, entry in enumerate(_as_list(raw, f"staff {person_id} reductions")):
        entry = as_dict(entry, f"staff {person_id} reduction")
        out.append({"reason": str(entry.get("reason") or "").strip(),
                    "periods": _whole_number(entry.get("periods"), f"staff {person_id} reductions[{i}].periods")})
    return out


def _normalise_staff(item: dict) -> dict:
    item = as_dict(item, "staff item")
    person_id = _check_id(item.get("id"), "staff")
    load_factor = item.get("load_factor", 1.0)
    allowance = item.get("allowance")
    return {
        "id": person_id,
        "name": item.get("name", ""),
        "short": item.get("short", ""),
        "dept": item.get("dept", ""),
        "load_factor": 1.0 if load_factor is None else float(load_factor),
        "avail": item.get("avail"),
        "max_periods_day": item.get("max_periods_day"),
        # periods a cycle the person is expected to teach before reductions; None: the derived
        # capacity (load_factor × max_load × days), see effective_allowance
        "allowance": None if allowance in (None, "") else _whole_number(allowance, f"staff {person_id} allowance"),
        "reductions": _normalise_reductions(item.get("reductions"), person_id),
        # a placeholder to be named later ("New MATH teacher 1")
        "provisional": _flag(item.get("provisional"), f"staff {person_id} provisional"),
        "source_hash": item.get("source_hash"),
    }


def _normalise_class(item: dict) -> dict:
    item = as_dict(item, "class item")
    code = item.get("code")
    if not isinstance(code, str) or not code:
        raise PlanError(f"class: code {code!r} must be a non-empty string")
    return {"code": code, "level": item.get("level", ""), "size": item.get("size")}


def _normalise_division(item: dict) -> dict:
    item = as_dict(item, "division item")
    div_id = _check_id(item.get("id"), "divisions")
    return {"id": div_id, "classes": _as_list(item.get("classes"), "division classes")}


def normalise_lessons(raw) -> dict:
    """`{length: count}` with the four default lengths always present; PlanError for a length or
    count that is not a whole number in range, or for more than SPLIT_LESSONS_MAX lessons in all."""
    raw = as_dict(raw, "lessons")
    lessons = {k: 0 for k in _LESSON_LENGTHS}
    for k, v in raw.items():
        key = str(k)
        if not key.isdigit() or not (1 <= int(key) <= LESSON_LENGTH_MAX):
            raise PlanError(f"lessons: unknown lesson length {key!r}, must be a positive integer "
                             f"up to {LESSON_LENGTH_MAX}")
        try:
            count = int(v)
        except (TypeError, ValueError):
            raise PlanError(f"lessons[{key}]: count {v!r} must be an integer")
        if count < 0:
            raise PlanError(f"lessons[{key}]: count {count} must not be negative")
        lessons[key] = count
    if sum(lessons.values()) > SPLIT_LESSONS_MAX:
        raise PlanError(f"lessons: at most {SPLIT_LESSONS_MAX} lessons, not {sum(lessons.values())}")
    return lessons


def _normalise_requirement(item: dict) -> dict:
    item = as_dict(item, "requirement item")
    req_id = _check_id(item.get("id"), "requirements")
    try:
        lessons = normalise_lessons(item.get("lessons"))
    except PlanError as e:          # name the requirement: a plan that does not read says which one
        raise PlanError(f"{req_id}: {e}")
    periods = item.get("periods")
    if not periods:
        periods = sum(int(k) * v for k, v in lessons.items())
    venue = as_dict(item.get("venue"), "requirement venue")
    return {
        "id": req_id,
        "dept": item.get("dept", ""),
        "level": item.get("level", ""),
        "subject": item.get("subject", ""),
        "periods": periods,
        "lessons": lessons,
        "grouping": item.get("grouping") or "class",
        "classes": _as_list(item.get("classes"), "requirement classes"),
        "teachers": _as_list(item.get("teachers"), "requirement teachers"),
        "size": item.get("size"),
        "venue": {"kind": venue.get("kind"), "room": venue.get("room")},
        "sync_with": item.get("sync_with"),
        # a head of department's sign-off: teachers, lessons, periods and classes stay as they are
        # until it is unlocked (apply_patch); generation ignores it
        "locked": _flag(item.get("locked"), f"requirement {req_id} locked"),
        "source_hash": item.get("source_hash"),
    }


def _normalise_band(item: dict) -> dict:
    item = as_dict(item, "band item")
    b_id = _check_id(item.get("id"), "bands")
    return {"id": b_id, "division": item.get("division"), "options": _as_list(item.get("options"), "band options")}


def _normalise_vocabulary(raw) -> dict:
    raw = as_dict(raw, "vocabulary")
    out = dict(DEFAULT_VOCABULARY)
    for key in DEFAULT_VOCABULARY:
        value = raw.get(key)
        if value:
            out[key] = str(value)
    return out


def _normalise_rules(raw) -> dict:
    raw = as_dict(raw, "rules")
    return {
        "edge_subjects": _as_list(raw.get("edge_subjects"), "rules.edge_subjects"),
        "no_double_across_rest": raw.get("no_double_across_rest", True),
        "pinned": _as_list(raw.get("pinned"), "rules.pinned"),
    }


def normalise(plan: dict) -> dict:
    plan = as_dict(plan, "plan")
    out = {
        "version": plan.get("version", PLAN_VERSION),
        "staff": [_normalise_staff(s) for s in _as_list(plan.get("staff"), "staff")],
        "classes": [_normalise_class(c) for c in _as_list(plan.get("classes"), "classes")],
        "divisions": [_normalise_division(d) for d in _as_list(plan.get("divisions"), "divisions")],
        "requirements": [_normalise_requirement(r) for r in _as_list(plan.get("requirements"), "requirements")],
        "bands": [_normalise_band(b) for b in _as_list(plan.get("bands"), "bands")],
        "rules": _normalise_rules(plan.get("rules")),
        "vocabulary": _normalise_vocabulary(plan.get("vocabulary")),
        "source": plan.get("source"),
    }
    out["staff"].sort(key=lambda x: x["id"])
    out["classes"].sort(key=lambda x: x["code"])
    out["divisions"].sort(key=lambda x: x["id"])
    out["requirements"].sort(key=lambda x: x["id"])
    out["bands"].sort(key=lambda x: x["id"])
    return out


# ---------------------------------------------------------------------------
# patch
# ---------------------------------------------------------------------------

_BY_ID = ("staff", "divisions", "requirements", "bands")


def _merge_item(old: dict, patch: dict) -> dict:
    """Merge a patch into an existing item one level deep: a dict-valued field merges with
    the existing dict (so `{"venue": {"kind": "lab"}}` keeps `venue["room"]`); anything else
    replaces the old value outright, same as the top-level merge patch."""
    out = dict(old)
    for key, val in patch.items():
        if isinstance(val, dict) and isinstance(out.get(key), dict):
            out[key] = {**out[key], **val}
        else:
            out[key] = val
    return out


def _apply_keyed_patch(items: list, value: dict, key_field: str) -> None:
    for item_key, item_patch in value.items():
        idx = next((i for i, it in enumerate(items) if it.get(key_field) == item_key), None)
        if item_patch is None:
            if idx is not None:
                items.pop(idx)
            continue
        # the dict key is authoritative; a payload that also carries id/code must not override it
        item_patch = {k: v for k, v in item_patch.items() if k != key_field}
        if idx is None:
            items.append({key_field: item_key, **item_patch})
        else:
            items[idx] = _merge_item(items[idx], item_patch)


# What a lock holds still: who teaches a requirement, what and whom they teach, and the cell it is
# in (a locked requirement cannot be moved to another subject, department, level or group).
LOCKED_FIELDS = ("teachers", "lessons", "periods", "classes", "subject", "dept", "level", "grouping")


class LockError(PlanError):
    """A change a lock refuses. `req` is the locked requirement (as it was before the change), so a
    caller can name it in its own words (the board says "MATH Mathematics (G3) 1A2")."""
    def __init__(self, req: dict, message: str):
        super().__init__(message)
        self.req = req


def check_locks(before: dict, after: dict) -> None:
    """A locked requirement of `before` must survive into `after` with its LOCKED_FIELDS unchanged,
    unless `after` has it unlocked (the same patch set `locked: false`). Compared on the normalised
    documents, so rewriting a locked field with the value it already has is not a change. A lock
    holds its whole cell (cell_key) too: while a requirement of a cell stays locked, no requirement
    joins that cell (a new one, or one moved there from another cell). LockError otherwise."""
    after_reqs = {r["id"]: r for r in after["requirements"]}
    before_reqs = {r["id"]: r for r in before["requirements"]}
    held: dict = {}
    for old in before["requirements"]:
        if not old["locked"]:
            continue
        new = after_reqs.get(old["id"])
        if new is None or (new["locked"] and any(new[f] != old[f] for f in LOCKED_FIELDS)):
            raise LockError(old, f"{old['id']} is locked: unlock it first")
        if new["locked"]:
            held.setdefault(cell_key(old), old)
    if not held:
        return
    for r in after["requirements"]:
        locked = held.get(cell_key(r))
        if locked is None:
            continue
        prev = before_reqs.get(r["id"])
        if prev is None or cell_key(prev) != cell_key(r):
            raise LockError(locked, f"{locked['id']} is locked: unlock it before adding {r['id']} to its cell")


def apply_patch(plan: dict, patch: dict) -> dict:
    try:
        before = normalise(plan)
    except PlanError:
        before = None       # a plan that does not normalise has no lock to honour; the patch may be its fix
    out = copy.deepcopy(plan)
    try:
        for key, value in patch.items():
            if key in _BY_ID and isinstance(value, dict):
                _apply_keyed_patch(out.setdefault(key, []), value, "id")
            elif key == "classes" and isinstance(value, dict):
                _apply_keyed_patch(out.setdefault(key, []), value, "code")
            elif key == "rules" and isinstance(value, dict):
                out["rules"] = {**out.get("rules", {}), **value}
            elif value is None:
                out.pop(key, None)
            elif isinstance(value, dict) and isinstance(out.get(key), dict):
                out[key] = {**out[key], **value}
            else:
                out[key] = value
    except (TypeError, AttributeError) as e:
        raise PlanError(f"bad patch: {e}")
    after = normalise(out)
    if before is not None:
        check_locks(before, after)
    return after


# ---------------------------------------------------------------------------
# import merge
# ---------------------------------------------------------------------------

_SECTIONS = ("staff", "classes", "divisions", "requirements", "bands")


def _rules_are_default(rules) -> bool:
    """The workbook carries no rules, so the importer emits the defaults; those must not overwrite
    edge subjects or pinned events the user set in the app."""
    r = _normalise_rules(rules)
    return r["edge_subjects"] == [] and r["pinned"] == [] and r["no_double_across_rest"] is True


def _vocabulary_is_default(vocabulary) -> bool:
    """Neither importer sets a vocabulary, so a re-import must not reset the one the wizard (or
    the user, via a patch) already chose for this plan."""
    return _normalise_vocabulary(vocabulary) == DEFAULT_VOCABULARY


def cell_key(req: dict) -> tuple:
    """The deployment board's cell a requirement belongs to: one teacher's (or co-taught pair's)
    requirement, or every part of a split, share (dept, level, subject, grouping, classes)."""
    return (req.get("dept", ""), req.get("level", ""), req.get("subject", ""),
            req.get("grouping") or "class", tuple(req.get("classes") or ()))


def _cell_name(req: dict) -> str:
    """A cell as the person who filled the workbook in reads it: "MATH Mathematics 1A"."""
    return " ".join(p for p in (str(req.get("dept") or ""), str(req.get("subject") or ""),
                                ",".join(req.get("classes") or ())) if p)


def _lock_signature(reqs: list[dict]) -> list:
    return sorted((r["id"], repr(cell_key(r)), *(repr(r[f]) for f in LOCKED_FIELDS)) for r in reqs)


def _keep_locked(existing_n: dict, out: dict, kept: list | None) -> None:
    """A re-import never overrides a lock, and a lock protects its whole cell (cell_key): when any
    requirement of a cell is locked in the existing plan, every existing requirement of that cell
    (locked and unlocked split parts alike) stays exactly as it is and the workbook's rows for that
    cell are dropped, so a re-split or a return to co-teaching cannot half-apply. A workbook row
    that reuses one of those ids from another cell is dropped too (ids must stay unique).

    The one field of a kept requirement that still follows the workbook is `size` (not a locked
    field): taken from the workbook's requirement of the same cell with the same id, else from any of the cell's.

    A workbook row for the same subject whose classes overlap a locked cell's but differ (another
    id, another cell) is added beside the locked cell rather than dropped: the person decides which
    to keep. `kept`, when given, collects (where, text) notes for the importer to report."""
    existing_reqs = existing_n["requirements"]
    locked_cells = {cell_key(r) for r in existing_reqs if r["locked"]}
    if not locked_cells:
        return
    keep = [copy.deepcopy(r) for r in existing_reqs if cell_key(r) in locked_cells]
    keep_ids = {r["id"] for r in keep}
    incoming = out["requirements"]
    replaced = [r for r in incoming if cell_key(r) in locked_cells or r["id"] in keep_ids]
    remaining = [r for r in incoming if not (cell_key(r) in locked_cells or r["id"] in keep_ids)]

    # size is not a locked field: the workbook's count of students follows through to the kept cell
    incoming_by_id = {r["id"]: r for r in replaced}
    incoming_by_cell: dict = {}
    for r in replaced:
        incoming_by_cell.setdefault(cell_key(r), r)
    for r in keep:
        source = incoming_by_id.get(r["id"])
        if source is None or cell_key(source) != cell_key(r):
            source = incoming_by_cell.get(cell_key(r))
        if source is not None:
            r["size"] = source["size"]

    notes: list[tuple[str, str]] = []
    for cell in sorted(locked_cells, key=repr):      # a hand patch may mix types (level 1 beside "1G3")
        mine = [r for r in keep if cell_key(r) == cell]
        theirs = [r for r in replaced if cell_key(r) == cell or r["id"] in {m["id"] for m in mine}]
        where, name = mine[0]["id"], _cell_name(mine[0])
        # the workbook moved this subject's row onto other classes: another id, added beside it
        beside = [r for r in remaining if cell_key(r)[:4] == cell[:4] and set(r["classes"]) & set(cell[4])
                  and r["id"] not in {e["id"] for e in existing_reqs}]
        if not theirs and beside:
            for r in beside:
                notes.append((where, f"{name}: locked {','.join(cell[4])} kept; the workbook's row for "
                                     f"{','.join(r['classes'])} was added beside it — remove one"))
        elif _lock_signature(theirs) != _lock_signature(mine):
            notes.append((where, f"{name}: locked, the workbook's change was not applied"))

    out["requirements"] = sorted(remaining + keep, key=lambda x: x["id"])
    # a kept requirement still names its classes, so the class list keeps them too
    codes = {c["code"] for c in out["classes"]}
    old_classes = {c["code"]: c for c in existing_n["classes"]}
    for r in keep:
        for code in r["classes"]:
            if code not in codes:
                out["classes"].append(copy.deepcopy(old_classes.get(code) or {"code": code, "level": "", "size": None}))
                codes.add(code)
    out["classes"].sort(key=lambda x: x["code"])
    if kept is not None:
        kept.extend(notes)


# What a staff-deployment workbook never carries, so an upload keeps the app's value whenever the id
# matches, however the row changed (an exported workbook's rows hash differently from the school's).
# A duties workbook does carry availability and "Together with", so its import passes a shorter list.
DEPLOYMENT_UNCARRIED = {"staff": ("avail", "max_periods_day"), "requirements": ("venue.room", "sync_with")}
DUTIES_UNCARRIED = {"staff": ("max_periods_day",), "requirements": ("venue.room",)}


def _keep_uncarried(old: dict, new: dict, fields) -> None:
    for field in fields:
        if "." in field:
            outer, inner = field.split(".", 1)
            new[outer] = {**(new.get(outer) or {}), inner: (old.get(outer) or {}).get(inner)}
        else:
            new[field] = old.get(field)


def merge_import(existing: dict, imported: dict, kept: list | None = None, uncarried: dict | None = None) -> dict:
    """Merge a freshly imported plan into the existing one. `kept`, when given, collects (where, text)
    notes on the locked cells whose workbook change was not applied (_keep_locked). `uncarried`
    ({"staff": (...), "requirements": (...)}, e.g. DEPLOYMENT_UNCARRIED) names the fields the workbook
    never carries: those keep the existing value whenever the id matches. Every other app-set field
    is kept only while the row's source_hash is unchanged."""
    uncarried = uncarried or {}
    existing_n = normalise(existing)
    imported = as_dict(imported, "imported plan")

    out = {}
    for key in _SECTIONS:
        out[key] = imported[key] if key in imported and imported[key] is not None else existing_n[key]
    imported_rules = imported.get("rules")
    out["rules"] = existing_n["rules"] if imported_rules is None or _rules_are_default(imported_rules) else imported_rules
    imported_vocabulary = imported.get("vocabulary")
    out["vocabulary"] = (existing_n["vocabulary"] if imported_vocabulary is None or _vocabulary_is_default(imported_vocabulary)
                          else imported_vocabulary)
    out["version"] = imported.get("version", existing_n["version"])
    out["source"] = imported.get("source", existing_n["source"])

    out = normalise(out)

    # The class list comes from the imported requirements, but its sizes and levels come from a
    # sizes CSV or the user's own edit: a class that survives the re-import keeps them.
    old_classes = {c["code"]: c for c in existing_n["classes"]}
    for c in out["classes"]:
        old = old_classes.get(c["code"])
        if old is None:
            continue
        if c.get("size") is None:
            c["size"] = old.get("size")
        if not c.get("level"):
            c["level"] = old.get("level", "")

    old_reqs = {r["id"]: r for r in existing_n["requirements"]}
    for r in out["requirements"]:
        old = old_reqs.get(r["id"])
        if old is None:
            continue
        if old.get("source_hash") == r.get("source_hash"):
            r["venue"] = old["venue"]
            r["sync_with"] = old["sync_with"]
            r["locked"] = old["locked"]
        _keep_uncarried(old, r, uncarried.get("requirements", ()))

    old_staff = {s["id"]: s for s in existing_n["staff"]}
    for s in out["staff"]:
        old = old_staff.get(s["id"])
        if old is None:
            continue
        if old.get("source_hash") == s.get("source_hash"):
            s["avail"] = old["avail"]
            s["max_periods_day"] = old["max_periods_day"]
            s["allowance"] = old["allowance"]
            s["reductions"] = old["reductions"]
            s["provisional"] = old["provisional"]
        _keep_uncarried(old, s, uncarried.get("staff", ()))

    _keep_locked(existing_n, out, kept)
    return out


# ---------------------------------------------------------------------------
# allowances and splits (spec docs/superpowers/specs/2026-09-26-deployment-board-design.md §2)
# ---------------------------------------------------------------------------

def cycle_days(settings: dict) -> int:
    time = settings["time"]
    return len(time["labels"]) // time["slots_per_day"]


def effective_allowance(staff: dict, settings: dict) -> float:
    """Periods a cycle a person can be given: their allowance (or, without one, the derived
    capacity load_factor × max_load × days) less every reduction (HOD 8, CCA 2, ...), never below 0."""
    allowance = base_allowance(staff, settings)
    return max(0, allowance - reduction_total(staff))


def reduction_total(staff: dict) -> int:
    return sum(int(r["periods"]) for r in staff.get("reductions") or [])


def base_allowance(staff: dict, settings: dict) -> float:
    """The allowance before reductions: the staff's own, or the derived capacity."""
    allowance = staff.get("allowance")
    if allowance is None:
        return float(staff.get("load_factor", 1.0)) * settings["rules"]["max_load"] * cycle_days(settings)
    return allowance


def split_part_id(base: str, index: int) -> str:
    """The id of part `index` of a split: `<base>-a`, `<base>-b`, ... The base is trimmed (and any
    hyphen left dangling dropped) so the part still fits PLAN_ID_MAX."""
    if not 0 <= index < 26:
        raise PlanError(f"a split has at most 26 parts, not {index + 1}")
    suffix = "-" + "abcdefghijklmnopqrstuvwxyz"[index]
    trimmed = base[:PLAN_ID_MAX - len(suffix)].rstrip("-") or "x"
    return trimmed + suffix


def split_lessons(lessons: dict, shares: list[int]) -> list[dict]:
    """Share a requirement's lesson pattern out between teachers: share i gets whole lessons that
    add up to exactly shares[i] periods. Largest lessons first, each into the first share that still
    has room; when that strands a lesson, the search backtracks through the other placements
    (patterns are small). Deterministic: the same pattern and shares always give the same answer.
    Each share comes back as {length: count} with only the lengths it holds.
    PlanError when the shares do not add up to the pattern's periods or no exact sharing exists."""
    pattern = normalise_lessons(lessons)          # at most SPLIT_LESSONS_MAX lessons
    lengths = sorted((int(k) for k, n in pattern.items() for _ in range(n)), reverse=True)
    shares = [int(x) for x in shares]
    label = "/".join(str(x) for x in shares)
    total = sum(lengths)
    if not shares or any(x <= 0 for x in shares):
        raise PlanError(f"a {label} split needs one share of at least 1 period for each teacher")
    if sum(shares) != total:
        raise PlanError(f"a {label} split does not add up to the {total} periods of the lessons")

    room = list(shares)
    placed = [0] * len(lengths)
    failed: set = set()

    def place(i: int) -> bool:
        if i == len(lengths):
            return True
        state = (i, tuple(room))
        if state in failed:
            return False
        tried: set[int] = set()
        for s in range(len(room)):
            # two shares with the same room left are interchangeable for what remains
            if room[s] < lengths[i] or room[s] in tried:
                continue
            tried.add(room[s])
            room[s] -= lengths[i]
            placed[i] = s
            if place(i + 1):
                return True
            room[s] += lengths[i]
        failed.add(state)
        return False

    if not place(0):
        raise PlanError(f"a {label} split does not fit lessons {','.join(str(n) for n in lengths)}")
    out: list[dict] = [{} for _ in shares]
    for length, s in zip(lengths, placed):
        key = str(length)
        out[s][key] = out[s].get(key, 0) + 1
    return out
