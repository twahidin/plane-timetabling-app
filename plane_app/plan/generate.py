"""Generate a draft organisation from the curriculum plan (spec
docs/superpowers/specs/2026-09-19-curriculum-plan-design.md §6).

The plan says what must be taught, by whom and to which classes; the draft organisation says
which planes (teachers, groups) meet in which events. Generation refuses while any block issue
stands, and reports its assumptions as warnings rather than silently guessing.
"""
from __future__ import annotations

import copy
import hashlib

from .. import intake
from .issues import capped, has_blocks, plan_issues
from .model import PlanError, plan_slug

_LENGTHS = ("1", "2", "3", "4")

_ORG_ID_MAX = 31          # intake.ID_PATTERN; plan ids may be longer (model.PLAN_ID_MAX)
_ORG_ID_HEAD = 24         # 24 + "-" + 6 hex digits = 31


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def _fit_id(base: str, *parts: str) -> str:
    """Join `base` and `parts` into an id the organisation accepts (intake.ID_PATTERN, 31
    characters). A plan id that names six classes overflows that on its own, and event ids add
    "-<k>-<i>" on top, so an over-long id keeps a readable head and ends in a hash of the whole
    string: the mapping stays stable across generations and distinguishes ids that share a head."""
    full = "-".join((base, *parts))
    if len(full) <= _ORG_ID_MAX:
        return full
    head = full[:_ORG_ID_HEAD].rstrip("-")
    return f"{head}-{hashlib.sha1(full.encode()).hexdigest()[:6]}"

def _source_name(plan: dict) -> str:
    source = plan.get("source") or {}
    file = source.get("file") if isinstance(source, dict) else None
    if not file:
        return "New timetable"
    stem = str(file).rsplit("/", 1)[-1]
    return stem.rsplit(".", 1)[0] or stem


def _division_of(plan: dict, classes: list[str]) -> str | None:
    """The division whose classes cover this requirement's classes; plan issues block when none does."""
    wanted = set(classes)
    for d in plan["divisions"]:
        if wanted <= set(d["classes"]):
            return d["id"]
    return None


def _band_name(band: dict) -> str:
    division = band.get("division") or ""
    prefix = f"{division}-"
    if division and band["id"].startswith(prefix) and band["id"][len(prefix):]:
        return band["id"][len(prefix):]
    return band["id"]


# ---------------------------------------------------------------------------
# sections
# ---------------------------------------------------------------------------

def _persons(plan: dict, base: dict, n_slots: int, loc_ids: list[str]) -> list[dict]:
    staff_ids = {s["id"] for s in plan["staff"]}
    persons = [
        {"id": s["id"], "name": s["name"] or s["id"], "role": "Teacher",
         "avail": copy.deepcopy(s["avail"]) if s.get("avail") else [[0, n_slots]],
         "eligible": list(loc_ids)}
        for s in plan["staff"]
    ]
    # planes the plan does not describe (support staff, rooms booked as people) survive untouched
    persons += [copy.deepcopy(p) for p in base.get("persons", [])
                if not str(p.get("role", "")).startswith("Teacher") and p["id"] not in staff_ids]
    return persons


def _groups_and_bands(plan: dict, warnings: list[str]) -> tuple[list[dict], list[dict], dict[str, str]]:
    """Whole-class groups (one per class) and option groups (one per grouping per division),
    plus the map from requirement id to the group that attends it."""
    sizes = {c["code"]: c.get("size") for c in plan["classes"]}
    codes = list(sizes)
    for source in (*plan["divisions"], *plan["requirements"]):
        for code in source["classes"]:
            if code not in sizes:
                sizes[code] = None
                codes.append(code)
                warnings.append(f"{code}: not in the plan's classes; added as a whole-class group")

    groups = [{"id": _fit_id(plan_slug(code)), "name": code, "classes": [code], "band": None,
               "option": None, "size": sizes[code]} for code in codes]
    by_id = {g["id"]: g for g in groups}

    band_of_req = {option: b["id"] for b in plan["bands"] for option in b["options"]}
    group_of_req: dict[str, str] = {}
    for r in plan["requirements"]:
        if r["grouping"] == "class":
            group_of_req[r["id"]] = _fit_id(plan_slug(r["classes"][0]))
            continue
        division = _division_of(plan, r["classes"])
        gid = _fit_id(plan_slug(f"{r['grouping']}-{division or '-'.join(r['classes'])}"))
        group_of_req[r["id"]] = gid
        if gid not in by_id:
            group = {"id": gid,
                     "name": f"{'/'.join(r['classes'])} · {r['subject']} · {r['grouping']}",
                     "classes": list(r["classes"]), "band": band_of_req.get(r["id"]),
                     "option": r["grouping"], "size": r.get("size")}
            groups.append(group)
            by_id[gid] = group

    divisions = {d["id"]: d for d in plan["divisions"]}
    bands = []
    for b in plan["bands"]:
        division = divisions.get(b["division"])
        options = [by_id[group_of_req[o]]["option"] for o in b["options"]
                   if o in group_of_req and by_id[group_of_req[o]].get("band") == b["id"]]
        if not options:
            warnings.append(f"{b['id']}: no option group, band dropped")
            continue
        bands.append({"id": b["id"], "name": _band_name(b),
                      "classes": list(division["classes"]) if division else [],
                      "options": list(dict.fromkeys(options))})
    return groups, bands, group_of_req


def _eligible_locs(r: dict, locations: list[dict], warnings: list[str]) -> list[str]:
    non_rest = [l["id"] for l in locations if not l.get("rest")]
    room, kind = r["venue"].get("room"), r["venue"].get("kind")
    if room:
        if room in {l["id"] for l in locations}:
            return [room]
        warnings.append(f"{r['id']}: unknown room {room!r}, any suitable room may be used")
    if kind:
        of_kind = [l["id"] for l in locations if not l.get("rest") and l.get("kind") == kind]
        if of_kind:
            return of_kind
        warnings.append(f"{r['id']}: no {kind} location in the organisation, any room may be used")
    return non_rest


def _events(plan: dict, locations: list[dict], group_of_req: dict[str, str],
            warnings: list[str]) -> list[dict]:
    reqs = {r["id"]: r for r in plan["requirements"]}
    # the i-th lesson of length k of every option of a band starts together; an option with
    # fewer lessons of that length simply has no partner at that i, so it is left unsynced
    synced: dict[str, dict[str, int]] = {}
    for b in plan["bands"]:
        options = [reqs[o] for o in b["options"] if o in reqs]
        if len(options) > 1:
            synced[b["id"]] = {k: min(o["lessons"][k] for o in options) for k in _LENGTHS}
    band_of_req = {o: b["id"] for b in plan["bands"] for o in b["options"] if b["id"] in synced}

    events = []
    for r in plan["requirements"]:
        grouped = r["grouping"] != "class"
        label = r["grouping"] if grouped else (r["classes"][0] if r["classes"] else r["id"])
        name = f"{r['subject']} {label}".strip()
        eligible = _eligible_locs(r, locations, warnings)
        band = band_of_req.get(r["id"])
        members = list(r["teachers"]) + [group_of_req[r["id"]]]
        for k in _LENGTHS:
            for i in range(r["lessons"][k]):
                sync = _fit_id(band, k, str(i)) if band and i < synced[band][k] else None
                events.append({"id": _fit_id(r["id"], k, str(i)), "name": name, "members": list(members),
                               "dur": int(k), "loc": None, "t0": None, "sync": sync,
                               "eligible_locs": list(eligible), "fixed": False, "double": int(k) >= 2})
    return events


def _pinned(plan: dict, settings: dict, teacher_ids: list[str], locations: list[dict]) -> list[dict]:
    slots_per_day = settings["time"]["slots_per_day"]
    non_rest = [l["id"] for l in locations if not l.get("rest")]
    events = []
    for i, pin in enumerate(plan["rules"].get("pinned", [])):
        name = pin.get("name") or f"Pinned {i + 1}"
        day, slot = int(pin.get("day", 0)), int(pin.get("slot", 0))
        loc = pin.get("loc")
        events.append({"id": _fit_id(f"pin-{plan_slug(name)}", str(day), str(slot)), "name": name,
                       "members": list(teacher_ids) if pin.get("everyone") else [],
                       "dur": int(pin.get("dur", 1) or 1), "loc": loc,
                       "t0": day * slots_per_day + slot, "sync": None,
                       "eligible_locs": [loc] if loc else list(non_rest), "fixed": True,
                       "double": int(pin.get("dur", 1) or 1) >= 2})
    return events


# ---------------------------------------------------------------------------
# generate
# ---------------------------------------------------------------------------

def generate(plan: dict, base_org: dict | None, settings: dict) -> tuple[dict, dict]:
    """Return (draft organisation, summary). Raises PlanError while the plan has block issues."""
    base = copy.deepcopy(base_org) if base_org else intake.empty_organisation(settings, _source_name(plan))

    issues = plan_issues(plan, base, settings)
    if has_blocks(issues):
        blocks = [i.text for i in issues if i.level == "block"]
        raise PlanError(f"{len(blocks)} blocking issues: " + "; ".join(capped(blocks)))
    warnings = [i.text for i in issues if i.level == "warn"]

    locations = [copy.deepcopy(l) for l in base.get("locations", [])]
    loc_ids = [l["id"] for l in locations]
    n_slots = len(base["time_labels"])

    persons = _persons(plan, base, n_slots, loc_ids)
    groups, bands, group_of_req = _groups_and_bands(plan, warnings)
    teacher_ids = [s["id"] for s in plan["staff"]]
    events = _events(plan, locations, group_of_req, warnings)
    events += _pinned(plan, settings, teacher_ids, locations)

    rules = copy.deepcopy(base.get("rules")) if base.get("rules") else intake.rules_from_settings(settings)
    # a base organisation built before this rule (or by hand) may not say how long a day is, and on
    # a multi-day cycle the engine would then judge load and runs across the whole week
    spd = (settings.get("time") or {}).get("slots_per_day")
    if not rules.get("slots_per_day") and spd and n_slots > spd:
        rules["slots_per_day"] = spd
    rules["soft_edge_subjects"] = list(plan["rules"].get("edge_subjects", []))

    org = {"name": base.get("name") or _source_name(plan),
           "time_labels": list(base["time_labels"]), "time_unit": base["time_unit"],
           "rules": rules, "locations": locations, "persons": persons,
           "events": events, "groups": groups, "bands": bands}

    try:
        org = intake._validate(org)
    except intake.IntakeError as e:
        raise PlanError(f"the generated draft is not a valid organisation: {e}")

    summary = {"teachers": len(teacher_ids), "groups": len(groups), "bands": len(bands),
               "events": len(events), "warnings": capped(warnings)}
    return org, summary
