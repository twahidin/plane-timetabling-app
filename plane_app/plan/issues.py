"""Curriculum plan issues (spec docs/superpowers/specs/2026-09-19-curriculum-plan-design.md §3),
computed fresh from a normalised plan document, never stored. Generation refuses while any
"block" issue exists; "warn" issues are surfaced but do not stop generation."""
from __future__ import annotations

from dataclasses import dataclass

from .. import intake
from . import model as M

# Issue lists are read by people, in a toast, a chat reply or a 409 body: past ten they stop being
# a list of things to fix and become a wall (spec §6 reports "the first ten").
ISSUE_LIMIT = 10


def _plain(number: float) -> str:
    """A capacity a person reads: 168, not 168.0 — a half-time load factor still shows its half."""
    return str(int(number)) if float(number).is_integer() else f"{number:g}"


def _load_unit(plan: dict, settings: dict) -> str:
    """The word a load is counted in, for a person to read. A slot an hour long is an hour; otherwise
    it is whatever the plan calls the thing that fills one — the start wizard sets that vocabulary per
    template — and "periods" for a plan that has no word of its own."""
    if int(((settings or {}).get("time") or {}).get("slot_minutes") or 0) == 60:
        return "hours"
    word = str((plan.get("vocabulary") or {}).get("requirement") or "").strip()
    return f"{word}s" if word and word != M.DEFAULT_VOCABULARY["requirement"] else "periods"


@dataclass
class Issue:
    level: str   # "block" | "warn"
    where: str   # id of the plan item the issue concerns
    text: str


def has_blocks(issues: list[Issue]) -> bool:
    return any(i.level == "block" for i in issues)


def capped(texts: list[str], limit: int = ISSUE_LIMIT) -> list[str]:
    """The first `limit` texts, followed by a line counting the rest when there are more."""
    texts = list(texts)
    if len(texts) <= limit:
        return texts
    return [*texts[:limit], f"and {len(texts) - limit} more"]


def plan_issues(plan: dict, org: dict | None, settings: dict) -> list[Issue]:
    issues: list[Issue] = []

    staff_ids = {s["id"] for s in plan["staff"]}
    divisions = {d["id"]: d for d in plan["divisions"]}
    requirements_by_id = {r["id"]: r for r in plan["requirements"]}

    for r in plan["requirements"]:
        if not r["teachers"]:
            issues.append(Issue("block", r["id"], f"{r['id']}: no teacher assigned"))
        else:
            for t in r["teachers"]:
                if t not in staff_ids:
                    issues.append(Issue("block", r["id"], f"{r['id']}: unknown teacher {t!r}"))

        expected = sum(int(k) * v for k, v in r["lessons"].items())
        if r["periods"] != expected:
            issues.append(Issue("block", r["id"],
                                 f"{r['id']}: periods {r['periods']} does not match lessons total {expected}"))

        if r["grouping"] != "class":
            classes = set(r["classes"])
            if not any(classes <= set(d["classes"]) for d in divisions.values()):
                issues.append(Issue("block", r["id"],
                                     f"{r['id']}: classes {sorted(classes)} are in no division"))
        elif len(r["classes"]) != 1:
            issues.append(Issue("block", r["id"],
                                 f"{r['id']}: an entire-class requirement names exactly one class"))

        if not r["venue"].get("kind") and not r["venue"].get("room"):
            issues.append(Issue("warn", r["id"], f"{r['id']}: no venue kind or room set"))

    for b in plan["bands"]:
        division = divisions.get(b["division"])
        division_classes = set(division["classes"]) if division else set()
        for option_id in b["options"]:
            option = requirements_by_id.get(option_id)
            if option is not None and set(option["classes"]) != division_classes:
                issues.append(Issue("block", b["id"],
                                     f"{b['id']}: band option {option_id} classes differ from division {b['division']}"))
        if len(b["options"]) == 1:
            issues.append(Issue("warn", b["id"], f"{b['id']}: division has a single option in this band"))

    for c in plan["classes"]:
        if c.get("size") is None:
            issues.append(Issue("warn", c["code"], f"{c['code']}: no size set"))

    if org is not None:
        location_ids = {loc["id"] for loc in org.get("locations", [])}
        for i, pin in enumerate(plan["rules"].get("pinned", [])):
            loc = pin.get("loc")
            if loc is not None and loc not in location_ids:
                where = pin.get("name") or f"pinned-{i}"
                issues.append(Issue("block", where, f"{where}: unknown venue {loc!r}"))

    days = len(settings["time"]["labels"]) // settings["time"]["slots_per_day"]
    max_load = settings["rules"]["max_load"]
    assigned: dict[str, int] = {}
    for r in plan["requirements"]:
        for t in r["teachers"]:
            assigned[t] = assigned.get(t, 0) + r["periods"]

    # Issues are read by the person who filled the workbook in, who wrote names into it, not ids.
    unit = _load_unit(plan, settings)
    for s in plan["staff"]:
        who = str(s.get("name") or "").strip() or s["id"]
        # a staff id becomes a person id of the organisation unchanged, so one written by hand past
        # the organisation's 31 characters must read here rather than fail inside generation
        if not intake.ID_PATTERN.match(s["id"]):
            issues.append(Issue("block", s["id"],
                                 f"{who}: the staff id {s['id']!r} must be 1-31 lowercase letters, digits "
                                 f"or hyphens (it becomes a person id of the timetable)"))
        capacity = s["load_factor"] * max_load * days
        load = assigned.get(s["id"], 0)
        if load > capacity:
            issues.append(Issue("warn", s["id"],
                                 f"{who}: assigned load {load} {unit} exceeds capacity {_plain(capacity)}"))
        if not s.get("avail"):
            issues.append(Issue("warn", s["id"], f"{who}: no availability window"))

    return issues
