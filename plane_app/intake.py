"""Turn extracted document text into a draft organisation, and patch drafts."""
from __future__ import annotations

import copy
import re

from plane_timetabling.model import Organisation

from .llm import Provider


class IntakeError(Exception):
    pass


ID_PATTERN = re.compile(r"^[a-z0-9][a-z0-9-]{0,30}$")

# The Anthropic structured-outputs dialect: every object closed with additionalProperties: false,
# nullable fields as anyOf unions, no pattern/minItems/maxItems (those constraints live in _validate).
_STR = {"type": "string"}
_INT = {"type": "integer"}
_STR_LIST = {"type": "array", "items": _STR}
_NULLABLE_STR = {"anyOf": [_STR, {"type": "null"}]}
_NULLABLE_INT = {"anyOf": [_INT, {"type": "null"}]}
_NULLABLE_STR_LIST = {"anyOf": [_STR_LIST, {"type": "null"}]}


def _obj(properties: dict, required: list[str]) -> dict:
    return {"type": "object", "properties": properties, "required": required, "additionalProperties": False}


ORG_SCHEMA = _obj({
    "name": _STR,
    "time_labels": _STR_LIST,
    "time_unit": _STR,
    "rules": _obj({"max_load": _INT, "max_run": _INT, "mandatory_rest": {"type": "array", "items": _INT},
                   "slots_per_day": {"anyOf": [_INT, {"type": "null"}]}},
                  ["max_load", "max_run", "mandatory_rest"]),
    "locations": {"type": "array", "items": _obj(
        {"id": _STR, "name": _STR, "cap": _INT, "shared": {"type": "boolean"}, "rest": {"type": "boolean"},
         "kind": _STR},
        ["id", "name", "cap"])},
    "persons": {"type": "array", "items": _obj(
        {"id": _STR, "name": _STR, "role": _STR, "avail": {"type": "array", "items": _INT}, "eligible": _STR_LIST},
        ["id", "name", "role", "avail", "eligible"])},
    "events": {"type": "array", "items": _obj(
        {"id": _STR, "name": _STR, "members": _STR_LIST, "dur": _INT, "loc": _NULLABLE_STR, "t0": _NULLABLE_INT,
         "sync": _NULLABLE_STR, "eligible_locs": _NULLABLE_STR_LIST, "fixed": {"type": "boolean"}},
        ["id", "name", "members", "dur"])},
    "groups": {"type": "array", "items": _obj(
        {"id": _STR, "name": _STR, "classes": _STR_LIST, "band": _NULLABLE_STR, "option": _NULLABLE_STR, "size": _NULLABLE_INT},
        ["id", "name", "classes", "band", "option"])},
    "bands": {"type": "array", "items": _obj(
        {"id": _STR, "name": _STR, "classes": _STR_LIST, "options": _STR_LIST},
        ["id", "name", "classes", "options"])},
    "notes": {"type": "array", "items": _obj({"section": _STR, "source": _STR, "note": _STR}, ["section", "note"])},
}, ["name", "locations", "persons", "events", "notes"])


def extraction_prompt(settings: dict) -> str:
    t, r = settings["time"], settings["rules"]
    return (
        "You turn timetable documents into structured data for a timetabling engine. In this model every person "
        "is a plane: it spans the slots they work (avail = [first slot, one past the last slot]) and the locations "
        "they may be in (eligible). An event is a lesson, shift, meeting or duty that a group attends together.\n"
        f"Slots are numbered from 0. The slot labels are: {', '.join(t['labels'])}. Time unit: {t['slot_minutes']} minutes.\n"
        f"Rules unless the document says otherwise: max_load: {r['max_load']}, max_run: {r['max_run']}, "
        f"mandatory_rest slots: {r['mandatory_rest']}.\n"
        "Students take part as groups, not as persons: a group is a set of students with an id, a name and the classes it "
        "draws from, and a lesson lists its group ids among its members next to the teachers. When a class splits by subject "
        "at one time (Mother Tongue, electives), that is a band: name it in bands with its classes and its options (the "
        "subjects), and give each (classes, option) its own group with band and option set. Every class also has its "
        "whole-class group: named after the class, classes = [that class], band and option null.\n"
        "Always include one location with id \"rest\" and rest: true, and include it in every person's eligible list. "
        "Use short lowercase ids (letters, digits, hyphens). Leave loc and t0 null unless the document already fixes "
        "when and where an event happens; set fixed: true only for those. Put every uncertainty into notes, naming the "
        "section and the source line. Do not invent people, rooms or lessons that the documents do not mention."
    )


def _ensure_rest(org: dict, settings: dict) -> None:
    if not any(l.get("rest") for l in org.get("locations", [])):
        org.setdefault("locations", []).append({"id": "rest", "name": "Rest", "cap": 999, "shared": True, "rest": True})
    for p in org.get("persons", []):
        if "rest" not in p.get("eligible", []):
            p.setdefault("eligible", []).append("rest")
    if "rules" not in org or not org["rules"]:
        org["rules"] = copy.deepcopy(settings["rules"])
    org.setdefault("time_labels", list(settings["time"]["labels"]))
    org.setdefault("time_unit", f"{settings['time']['slot_minutes']}-minute slot")
    for e in org.get("events", []):
        for k, v in (("loc", None), ("t0", None), ("sync", None), ("eligible_locs", None), ("fixed", False)):
            e.setdefault(k, v)
    for l in org.get("locations", []):
        l.setdefault("shared", False); l.setdefault("rest", False)


def _names_class(g: dict, c: str) -> bool:
    return str(g.get("name", "")).lower() == c.lower() or g["id"] == re.sub(r"[^a-z0-9-]+", "-", c.lower()).strip("-")


def _validate(org: dict) -> dict:
    try:
        Organisation.from_dict(org)
    except (KeyError, TypeError, ValueError, AttributeError) as e:
        raise IntakeError(f"The extracted data is not a valid organisation: {e!r}")
    for section in ("locations", "persons", "events", "groups", "bands"):
        for item in org.get(section, []):
            if not isinstance(item.get("id"), str) or not ID_PATTERN.match(item["id"]):
                raise IntakeError(f"{section}: id {item.get('id')!r} must be 1-31 lowercase letters, digits or hyphens")
    n = len(org["time_labels"])
    for p in org["persons"]:
        a = p["avail"]
        if len(a) != 2 or not all(isinstance(x, int) for x in a) or not (0 <= a[0] < a[1] <= n):
            raise IntakeError(f"{p['id']}: avail {a} must be [start, end) within 0..{n}")
    ids = {l["id"] for l in org["locations"]}
    for p in org["persons"]:
        bad = [x for x in p["eligible"] if x not in ids]
        if bad:
            raise IntakeError(f"{p['id']}: unknown locations {bad}")
    groups, bands = org.get("groups", []), org.get("bands", [])
    # the whole-class group of class X is X itself: no band, classes [X], named X (or with id X's slug); those define the classes
    classes = {g["classes"][0] for g in groups
               if not g.get("band") and len(g["classes"]) == 1 and _names_class(g, g["classes"][0])}
    band_ids = {b["id"] for b in bands}
    for g in groups:
        bad = [c for c in g["classes"] if c not in classes]
        if bad:
            raise IntakeError(f"group {g['id']}: unknown class {bad}: every class needs its own whole-class group, "
                              "named after the class with no band")
        if g.get("band") and g["band"] not in band_ids:
            raise IntakeError(f"group {g['id']}: unknown band {g['band']}")
    for b in bands:
        bad = [c for c in b["classes"] if c not in classes]
        if bad:
            raise IntakeError(f"band {b['id']}: unknown class {bad}")
        options = {g["option"] for g in groups if g.get("band") == b["id"]}
        bad = [o for o in b["options"] if o not in options]
        if bad or not b["options"]:
            raise IntakeError(f"band {b['id']}: no group takes option {bad or '(none)'}")
    pids = {p["id"] for p in org["persons"]} | {g["id"] for g in groups}       # groups are planes too
    for e in org["events"]:
        bad = [m for m in e["members"] if m not in pids]
        if bad:
            raise IntakeError(f"{e['id']}: unknown members {bad}")
        if isinstance(e.get("eligible_locs"), list):
            bad = [x for x in e["eligible_locs"] if x not in ids]
            if bad:
                raise IntakeError(f"{e['id']}: unknown eligible locations {bad}")
        if e.get("loc") is not None and e["loc"] not in ids:
            raise IntakeError(f"{e['id']}: unknown location {e['loc']}")
    return org


def extract_organisation(provider: Provider, settings: dict, texts: list[tuple[str, str]]) -> tuple[dict, list[dict]]:
    user = "\n\n".join(f"# Document: {name}\n{text}" for name, text in texts)
    data = provider.extract_json(extraction_prompt(settings), user, ORG_SCHEMA)
    if not isinstance(data, dict):
        raise IntakeError("The model did not return an object.")
    notes = data.pop("notes", []) or []
    for key in ("locations", "persons", "events"):
        if not isinstance(data.get(key), list):
            raise IntakeError(f"The extracted data has no {key} list.")
    _ensure_rest(data, settings)
    return _validate(data), notes


def apply_patch(org: dict, patch: dict) -> dict:
    out = copy.deepcopy(org)
    try:
        for key, value in patch.items():
            if key in ("persons", "locations", "events") and isinstance(value, dict):
                items = out.setdefault(key, [])
                for item_id, item_patch in value.items():
                    idx = next((i for i, it in enumerate(items) if it["id"] == item_id), None)
                    if item_patch is None:
                        if idx is not None:
                            items.pop(idx)
                    elif idx is None:
                        items.append({"id": item_id, **item_patch})
                    else:
                        items[idx] = {**items[idx], **item_patch}
            elif value is None:
                out.pop(key, None)
            elif isinstance(value, dict) and isinstance(out.get(key), dict):
                out[key] = {**out[key], **value}
            else:
                out[key] = value
    except (TypeError, AttributeError) as e:
        raise IntakeError(f"bad patch: {e}")
    return _validate(out)


def empty_organisation(settings: dict, name: str = "New timetable") -> dict:
    """A valid draft with no people or events, ready for the chat to fill from criteria."""
    org = {"name": name, "locations": [], "persons": [], "events": []}
    _ensure_rest(org, settings)
    return _validate(org)


def clone_for_rebuild(org: dict) -> dict:
    """Copy an organisation as a draft for the next period: fixed events keep their placement,
    other events are unplaced so the engine re-places them, and engine-created rest tiles are dropped."""
    out = copy.deepcopy(org)
    out["events"] = [e for e in out.get("events", []) if not str(e.get("id", "")).startswith("rest-")]
    for e in out["events"]:
        if not e.get("fixed"):
            e["loc"], e["t0"] = None, None
    return out


def summarise(org: dict) -> dict:
    return {"persons": len(org.get("persons", [])), "locations": len(org.get("locations", [])),
            "events": len(org.get("events", [])),
            "placed": sum(1 for e in org.get("events", []) if e.get("loc") is not None and e.get("t0") is not None)}
