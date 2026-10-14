"""The setup tabs' campus document (spec docs/superpowers/specs/2026-10-13-setup-campus-design.md §3): the
campus layout from the organisation's rooms (or the plan's venues before there is a timetable), and what the
plan says: staff with their loads and allowances, departments, classes with their periods, the week's rules,
the notes read from documents and the files read. A pure function over stored data that may be odd; the
route follows below. Nothing here writes."""
from __future__ import annotations

import re
from collections import Counter

from fastapi import APIRouter, Depends

from . import auth, campus as C
from .plan import board as B, model as M
from .plan.reader import inbox, routes as RR

CLASSES_MAX = 120
NO_DEPARTMENT = "No department"
UNREAD = "The plan does not read in full; some counts are missing."
BAD_PERIODS = "Some requirements have periods that could not be counted."
BAD_ALLOWANCE = "Some teachers' allowances could not be worked out."
BAD_CAMPUS = "The rooms could not be laid out; the campus is empty."
BAD_WEEK = "The week could not be read from the timetable; the settings' week is shown."
BAD_LEGEND = "The timetable's colours could not be read; departments are coloured by name."
# a weekday as a word ("Fri", "Fridays", "Thurs"), not a word that merely begins like one ("fried", "Sunny")
DAY = re.compile(r"\b(mon(?:day)?|tue(?:s|sday)?|wed(?:nesday)?|thu(?:r|rs|rsday)?|fri(?:day)?|sat(?:urday)?|sun(?:day)?)s?\b", re.I)


def _d(v) -> dict:
    return v if isinstance(v, dict) else {}


def _l(v) -> list:
    return v if isinstance(v, list) else []


def _s(v) -> str:
    return v if isinstance(v, str) else ""


def _read(plan) -> tuple[dict, bool]:
    """(the plan normalised, True) or, when it does not read, (the raw plan with only the parts that have
    the right shape, False)."""
    try:
        return M.normalise(plan), True
    except (M.PlanError, TypeError, ValueError, AttributeError):
        p = _d(plan)
        rules = _d(p.get("rules"))
        return {"staff": [s for s in _l(p.get("staff")) if isinstance(s, dict) and isinstance(s.get("id"), str)],
                "classes": [c for c in _l(p.get("classes")) if isinstance(c, dict)],
                "venues": [v for v in _l(p.get("venues")) if isinstance(v, dict)],
                "requirements": [r for r in _l(p.get("requirements")) if isinstance(r, dict)],
                "rules": {"edge_subjects": _l(rules.get("edge_subjects")), "pinned": _l(rules.get("pinned")),
                          "no_double_across_rest": rules.get("no_double_across_rest", True)},
                "notes": [n for n in _l(p.get("notes")) if isinstance(n, dict)]}, False


def _periods(req: dict, bad: set) -> int:
    try:
        return B._periods(req)
    except Exception:                                    # junk, Infinity, NaN: counted as 0
        bad.add(BAD_PERIODS)
        return 0


def _ids(v) -> list[str]:
    return list(dict.fromkeys(i for i in _l(v) if isinstance(i, str)))


def _plan_rooms(plan: dict) -> list[dict]:
    """One room per plan venue: the rooms of a school whose timetable is not made yet."""
    homes: dict[str, str] = {}
    for c in plan["classes"]:
        if isinstance(c.get("home"), str) and c["home"] and isinstance(c.get("code"), str):
            homes.setdefault(c["home"], c["code"])
    out, seen = [], set()
    for v in plan["venues"]:
        vid, name = v.get("id"), v.get("name")
        if not isinstance(vid, str) or not isinstance(name, str) or not name or vid in seen:
            continue
        seen.add(vid)
        out.append({"id": vid, "name": name, "block": v.get("block") or None, "level": v.get("level") or None,
                    "kind": str(v.get("kind") or ""), "cap": v.get("capacity"), "home": homes.get(vid)})
    return out


def _campus(org: dict | None, plan: dict, bad: set) -> dict:
    try:
        rooms = C.rooms(org or {}, plan) or _plan_rooms(plan)
        return {"rooms": rooms, **C.place(*C.buildings(rooms))}
    except Exception:                                    # odd stored rooms: an empty campus, said so
        bad.add(BAD_CAMPUS)
        return {"rooms": [], **C.place([], [])}


def _week(org: dict | None, plan: dict, settings: dict, bad: set) -> dict:
    try:
        t = C.time_of(org or {}, settings)
    except Exception:                                    # an odd organisation: the settings' week only
        bad.add(BAD_WEEK)
        t = C.time_of({}, settings)
    labels, spd = t["labels"], t["slots_per_day"]
    if not labels:                                       # no timetable yet: the settings' own week
        labels = [str(x) for x in _l(_d(_d(settings).get("time")).get("labels"))]
    labels = [str(x) for x in labels]
    days = len(labels) // spd if labels else 0
    rests = [n for n in (C._int(x, None) for x in _l(_d(_d(settings).get("rules")).get("mandatory_rest"))) if n is not None]
    rules = _d(plan.get("rules"))
    pinned = []
    for p in _l(rules.get("pinned")):
        if isinstance(p, dict):
            pinned.append({"name": str(p.get("name") or ""), "day": C._int(p.get("day"), 0) or 0,
                           "slot": C._int(p.get("slot"), 0) or 0, "dur": C._int(p.get("dur"), 1) or 1})
    return {"days": days, "slots_per_day": spd, "labels": labels, "rests": rests, "pinned": pinned,
            "edge_subjects": [str(s) for s in _l(rules.get("edge_subjects"))],
            "no_double_across_rest": bool(rules.get("no_double_across_rest", True))}


def _legend(org: dict | None, plan) -> dict:
    """Build's legend ({"d:<dept>" | "s:<subject>": label}, in Build's order), so a department keeps its
    colour between the setup tabs and Build; the same call /api/campus makes, over the stored plan."""
    try:
        return C.lessons(org or {}, plan if isinstance(plan, dict) else None)[1]
    except Exception:                                    # odd events or persons: colours by name instead
        return None


def _day_names(week: dict) -> list[str]:
    """Each cycle day's name: the first word of its first label ("Fri P1" is "Fri")."""
    labels, spd = week["labels"], week["slots_per_day"]
    return [(labels[d * spd].split() or [""])[0].lower() for d in range(week["days"])]


def _notes(plan: dict, week: dict) -> list[dict]:
    names = _day_names(week)
    out = []
    for n in plan.get("notes") or []:
        text = " ".join(_s(n.get("text")).split())
        if not text:
            continue
        m = DAY.search(text)
        day = next((i for i, name in enumerate(names) if m and name.startswith(m.group(1)[:3].lower())), None)
        src = n.get("source")
        out.append({"text": text, "source": str(src) if src else None, "day": day})
    return out


def setup_view(plan: dict | None, org: dict | None, settings: dict, readings: list[dict], ground: str = C.DEFAULT_GROUND) -> dict:
    bad: set = set()
    legend = _legend(org, plan)
    if legend is None:
        bad.add(BAD_LEGEND)
        legend = {}
    plan, ok = _read(plan)
    if not ok:
        bad.add(UNREAD)
    levels = {c["code"]: c.get("level") or None for c in plan["classes"] if isinstance(c.get("code"), str)}

    load: Counter = Counter()
    by_class: dict[str, dict] = {}
    for r in plan["requirements"]:
        p = _periods(r, bad)
        teachers = _ids(r.get("teachers"))
        for t in teachers:
            load[t] += p
        subject = _s(r.get("subject")) or "Other"
        for code in _ids(r.get("classes")):
            c = by_class.setdefault(code, {"code": code, "level": levels.get(code), "periods": 0, "by_subject": {}, "untaught": 0, "untaught_by_subject": {}})
            c["periods"] += p
            c["by_subject"][subject] = c["by_subject"].get(subject, 0) + p
            if not teachers:
                c["untaught"] += p
                c["untaught_by_subject"][subject] = c["untaught_by_subject"].get(subject, 0) + p

    staff = []
    for s in plan["staff"]:
        try:
            allowance = round(M.capacity_in(s, s.get("dept"), settings))
        except Exception:                                # odd stored allowances, shares or reductions
            bad.add(BAD_ALLOWANCE)
            allowance = 0
        staff.append({"id": s["id"], "name": _s(s.get("name")) or s["id"], "dept": _s(s.get("dept")), "load": load[s["id"]],
                      "allowance": allowance, "over": load[s["id"]] > allowance, "provisional": s.get("provisional") is True})

    depts: dict[str, dict] = {}
    for s in staff:
        d = depts.setdefault(s["dept"], {"name": s["dept"] or NO_DEPARTMENT, "teachers": 0, "load": 0, "allowance": 0})
        d["teachers"] += 1
        d["load"] += s["load"]
        d["allowance"] += s["allowance"]

    classes = sorted(by_class.values(), key=lambda c: C.natural(c["code"]))
    week = _week(org, plan, settings, bad)
    return {"campus": {**_campus(org, plan, bad), "ground": ground}, "ground": ground, "staff": staff,
            "departments": sorted(depts.values(), key=lambda d: d["name"].lower()),
            "classes": classes[:CLASSES_MAX], "classes_hidden": max(len(classes) - CLASSES_MAX, 0),
            "cycle_periods": week["days"] * week["slots_per_day"] - len(week["rests"]) * week["days"],
            "week": week, "notes": _notes(plan, week), "legend": legend,
            "files": [v for v in (_view(e) for e in _l(readings)) if v], "problems": sorted(bad)}


def _view(entry):
    if not isinstance(entry, dict):
        return None
    try:
        return inbox.view(entry)
    except (AttributeError, TypeError):
        return None


def make_router(db) -> APIRouter:
    r = APIRouter()

    @r.get("/api/campus/setup")
    def get_setup(sid: str = Depends(auth.require_session)):
        return setup_view(db.get_value("plan"), db.get_org("live") or db.get_org("draft"), db.get_settings(), RR._stored(db),
                          C.ground_of(db))

    return r
