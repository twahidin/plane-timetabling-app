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

# The plan's own vocabulary: the words the Plan tab, issues and printouts use for a person, a
# group, a requirement and a venue. Education's are the default; the start wizard sets its own
# for every other domain (spec docs/superpowers/specs/2026-09-20-start-wizard-design.md §5), and a
# re-import must not reset a vocabulary the user (or the wizard) already chose (`merge_import`).
DEFAULT_VOCABULARY = {"person": "teacher", "group": "class", "requirement": "lesson", "venue": "room"}


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


def _as_dict(value, name: str) -> dict:
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise PlanError(f"{name} must be an object")
    return value


def _normalise_staff(item: dict) -> dict:
    item = _as_dict(item, "staff item")
    person_id = _check_id(item.get("id"), "staff")
    load_factor = item.get("load_factor", 1.0)
    return {
        "id": person_id,
        "name": item.get("name", ""),
        "short": item.get("short", ""),
        "dept": item.get("dept", ""),
        "load_factor": 1.0 if load_factor is None else float(load_factor),
        "avail": item.get("avail"),
        "max_periods_day": item.get("max_periods_day"),
        "source_hash": item.get("source_hash"),
    }


def _normalise_class(item: dict) -> dict:
    item = _as_dict(item, "class item")
    code = item.get("code")
    if not isinstance(code, str) or not code:
        raise PlanError(f"class: code {code!r} must be a non-empty string")
    return {"code": code, "level": item.get("level", ""), "size": item.get("size")}


def _normalise_division(item: dict) -> dict:
    item = _as_dict(item, "division item")
    div_id = _check_id(item.get("id"), "divisions")
    return {"id": div_id, "classes": _as_list(item.get("classes"), "division classes")}


def _normalise_lessons(raw) -> dict:
    raw = _as_dict(raw, "lessons")
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
    return lessons


def _normalise_requirement(item: dict) -> dict:
    item = _as_dict(item, "requirement item")
    req_id = _check_id(item.get("id"), "requirements")
    lessons = _normalise_lessons(item.get("lessons"))
    periods = item.get("periods")
    if not periods:
        periods = sum(int(k) * v for k, v in lessons.items())
    venue = _as_dict(item.get("venue"), "requirement venue")
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
        "source_hash": item.get("source_hash"),
    }


def _normalise_band(item: dict) -> dict:
    item = _as_dict(item, "band item")
    b_id = _check_id(item.get("id"), "bands")
    return {"id": b_id, "division": item.get("division"), "options": _as_list(item.get("options"), "band options")}


def _normalise_vocabulary(raw) -> dict:
    raw = _as_dict(raw, "vocabulary")
    out = dict(DEFAULT_VOCABULARY)
    for key in DEFAULT_VOCABULARY:
        value = raw.get(key)
        if value:
            out[key] = str(value)
    return out


def _normalise_rules(raw) -> dict:
    raw = _as_dict(raw, "rules")
    return {
        "edge_subjects": _as_list(raw.get("edge_subjects"), "rules.edge_subjects"),
        "no_double_across_rest": raw.get("no_double_across_rest", True),
        "pinned": _as_list(raw.get("pinned"), "rules.pinned"),
    }


def normalise(plan: dict) -> dict:
    plan = _as_dict(plan, "plan")
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


def apply_patch(plan: dict, patch: dict) -> dict:
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
    return normalise(out)


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


def merge_import(existing: dict, imported: dict) -> dict:
    existing_n = normalise(existing)
    imported = _as_dict(imported, "imported plan")

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
        if old is not None and old.get("source_hash") == r.get("source_hash"):
            r["venue"] = old["venue"]
            r["sync_with"] = old["sync_with"]

    old_staff = {s["id"]: s for s in existing_n["staff"]}
    for s in out["staff"]:
        old = old_staff.get(s["id"])
        if old is not None and old.get("source_hash") == s.get("source_hash"):
            s["avail"] = old["avail"]
            s["max_periods_day"] = old["max_periods_day"]

    return out
