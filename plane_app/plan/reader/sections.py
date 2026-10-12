"""A plan reading as sections (spec docs/superpowers/specs/2026-10-12-assistant-files-design.md §1, §3.3).

`merged` is the plan a reading would make; `sections_of` what it adds or changes, by section; `select`
the current plan with only the ticked sections taken from it. Pure: nothing is stored here."""
from __future__ import annotations

import copy

from .. import model as M

SECTIONS: dict[str, tuple[str, ...]] = {
    "staff": ("staff",), "classes": ("classes", "divisions"), "rooms": ("venues",),
    "requirements": ("requirements", "bands"), "rules": ("rules",), "notes": ("notes",),
}
SAMPLES = 5
_KEY = {"staff": "id", "classes": "code", "divisions": "id", "venues": "id", "requirements": "id", "bands": "id"}


def merged(current: dict, reading: dict) -> dict:
    if reading.get("merged") is not None:                     # an exact workbook: merged by the importer
        out = copy.deepcopy(reading["merged"])
    else:
        out = M.apply_patch(current, reading.get("patch") or {})
    rules = reading.get("rules") or {}
    if rules:
        r = dict(out.get("rules") or {})
        edge = list(r.get("edge_subjects") or [])
        edge += [s for s in rules.get("edge_subjects") or [] if s not in edge]
        pinned = list(r.get("pinned") or [])
        names = {p.get("name") for p in pinned}
        pinned += [p for p in rules.get("pinned") or [] if p.get("name") not in names]
        r.update(edge_subjects=edge, pinned=pinned)
        if rules.get("no_double_across_rest") is not None:
            r["no_double_across_rest"] = bool(rules["no_double_across_rest"])
        out["rules"] = r
    notes = list(out.get("notes") or [])
    have = {n["text"] for n in notes}
    for text in reading.get("notes") or []:
        text = " ".join(str(text).split())
        if text and text not in have:
            notes.append({"text": text, "source": reading.get("file")})
            have.add(text)
    if notes:
        out["notes"] = notes
    return M.normalise(out)


def _label(key: str, item: dict) -> str:
    if key == "staff":
        return item.get("name") or item["id"]
    if key == "classes":
        return item["code"]
    if key == "venues":
        return item.get("name") or item["id"]
    if key == "requirements":
        periods = sum(int(k) * int(v) for k, v in (item.get("lessons") or {}).items() if str(k).isdigit())
        return " · ".join(x for x in (", ".join(item.get("classes") or []), item.get("subject") or item["id"],
                                      f"{periods} a week" if periods else "") if x)
    return item.get("name") or item.get("id") or ""


_BOOKKEEPING = ("rev_at", "source_hash")      # stamped by the store or the importer: not what a reading changes


def _strip(item: dict) -> dict:
    return {k: v for k, v in item.items() if k not in _BOOKKEEPING}


def sections_of(current: dict, merged_plan: dict) -> dict[str, dict]:
    out: dict[str, dict] = {}
    for name, keys in SECTIONS.items():
        added = changed = 0
        samples: list[str] = []
        for key in keys:
            if key == "rules":
                old, new = current.get("rules") or {}, merged_plan.get("rules") or {}
                fresh = ([s for s in new.get("edge_subjects") or [] if s not in (old.get("edge_subjects") or [])]
                         + [p.get("name") or "Pinned" for p in new.get("pinned") or []
                            if p.get("name") not in {q.get("name") for q in old.get("pinned") or []}])
                added += len(fresh)
                samples += fresh
                continue
            if key == "notes":
                old = {n["text"] for n in current.get("notes") or []}
                fresh = [n["text"] for n in merged_plan.get("notes") or [] if n["text"] not in old]
                added += len(fresh)
                samples += fresh
                continue
            before = {x[_KEY[key]]: _strip(x) for x in current.get(key) or []}
            for x in merged_plan.get(key) or []:
                was = before.get(x[_KEY[key]])
                if was is None:
                    added += 1
                elif was != _strip(x):
                    changed += 1
                else:
                    continue
                if key in ("staff", "classes", "venues", "requirements"):
                    samples.append(_label(key, x))
        if added + changed:
            out[name] = {"count": added + changed, "added": added, "changed": changed,
                         "samples": samples[:SAMPLES], "ticked": True}
    return out


class SelectError(M.PlanError):
    """A selection refused for what was (not) ticked, or for names gone from the plan: its own words."""


TITLES = {"staff": "Staff", "classes": "Classes", "rooms": "Rooms"}


def select(current: dict, merged_plan: dict, ticked: list[str]) -> dict:
    out = copy.deepcopy(current)
    for name in ticked:
        for key in SECTIONS.get(name, ()):
            if key in merged_plan:
                out[key] = copy.deepcopy(merged_plan[key])
    # only references the selection introduced: a stored plan may already hold dangling ones (an issue, not an error)
    fresh = _dangling(out) - _dangling(current)
    if fresh:
        raise SelectError(_refusal(fresh, merged_plan, ticked))
    return M.normalise(out)


def _refusal(fresh: set[tuple[str, str, str]], merged_plan: dict, ticked: list[str]) -> str:
    """Why a selection with references to things the plan lacks is refused: "tick X too" when an unticked
    section of this reading would bring them all; else the ones no section brings, gone from the plan."""
    have = {"staff": {s["id"] for s in merged_plan.get("staff") or []},
            "classes": {c["code"] for c in merged_plan.get("classes") or []},
            "rooms": {v["id"] for v in merged_plan.get("venues") or []}}
    section = {"teacher": "staff", "class": "classes", "home": "rooms"}
    supply = {ref: section[ref[1]] for ref in fresh
              if section[ref[1]] not in ticked and ref[2] in have[section[ref[1]]]}
    gone = sorted({ref[2] for ref in fresh if ref not in supply})
    if gone:
        return f"this reading names staff, classes or rooms no longer in the plan: {', '.join(gone[:8])}"
    homes = sorted(f"{owner} ({name})" for owner, kind, name in fresh if kind == "home")
    names = sorted({name for _, kind, name in fresh if kind != "home"})
    what = "; ".join(x for x in (
        f"requirements name staff or classes this plan does not have: {', '.join(names[:8])}" if names else "",
        f"classes have home rooms this plan does not have: {', '.join(homes[:8])}" if homes else "") if x)
    need = [TITLES[s] for s in TITLES if s in set(supply.values())]
    return f"{what}; tick {' and '.join(need)} too"


def _dangling(plan: dict) -> set[tuple[str, str, str]]:
    """(requirement id or class code, "teacher" | "class" | "home", name) for each reference to a staff id,
    class code or room the plan lacks."""
    staff = {s["id"] for s in plan.get("staff") or []}
    classes = {c["code"] for c in plan.get("classes") or []}
    rooms = {v["id"] for v in plan.get("venues") or []}
    reqs = plan.get("requirements") or []
    return ({(r["id"], "teacher", t) for r in reqs for t in r.get("teachers") or [] if t not in staff}
            | {(r["id"], "class", c) for r in reqs for c in r.get("classes") or [] if c not in classes}
            | {(c["code"], "home", c["home"]) for c in plan.get("classes") or []
               if c.get("home") not in (None, "") and c["home"] not in rooms})
