"""The campus (spec docs/superpowers/specs/2026-10-11-campus-looks-design.md §2.2): the timetable's rooms
with the block and level the plan's venues give them, the lessons in each room with a department colour,
the people, and the last check's clashes placed on rooms and slots. Pure functions over the organisation,
the plan and the check; the layout and the route follow below."""
from __future__ import annotations

import re

from fastapi import APIRouter, Depends

from . import auth
from .learning import log as decisions


MAX_DUR = 64                          # a lesson longer than this is clipped (bad data must not hang a request)


def _key(name) -> str:
    return " ".join(str(name or "").split()).lower()


def _int(v, default: int | None) -> int | None:
    """An int from stored data that may be a numeric string, junk or missing."""
    try:
        return int(v)
    except (TypeError, ValueError):
        return default


def _rest_locs(org: dict) -> set:
    return {l.get("id") for l in (org or {}).get("locations") or [] if isinstance(l, dict) and l.get("rest")}


def rooms(org: dict, plan: dict | None) -> list[dict]:
    """[{id, name, block, level, kind, cap, home}] for every location that is not a rest tile; block and
    level from the plan's venue of the same name (as plan/generate.py matches venues to locations)."""
    venues = {_key(v.get("name")): v for v in (plan or {}).get("venues") or [] if isinstance(v, dict)}
    homes: dict[str, str] = {}
    for c in (plan or {}).get("classes") or []:
        if isinstance(c, dict) and c.get("home"):
            homes.setdefault(c["home"], c.get("code"))
    out, seen = [], set()
    for loc in (org or {}).get("locations") or []:
        if not isinstance(loc, dict) or loc.get("rest") or not isinstance(loc.get("id"), str) or loc["id"] in seen:
            continue
        seen.add(loc["id"])
        v = venues.get(_key(loc.get("name"))) or {}
        out.append({"id": loc["id"], "name": str(loc.get("name") or loc["id"]),
                    "block": v.get("block") or None, "level": v.get("level") or None,
                    "kind": str(loc.get("kind") or v.get("kind") or ""), "cap": loc.get("cap"),
                    "home": homes.get(v.get("id")) if v else None})
    return out


def lessons(org: dict, plan: dict | None) -> tuple[dict, dict]:
    """({room id: [lesson, …] by t0}, {colour key: label}). A lesson's colour is its requirement's
    department ("d:<dept>") when the event traces to the plan, else its subject ("s:<subject>"). Only
    people are teachers: groups in `members` are left out. Unplaced events and rest tiles are skipped."""
    persons = {p.get("id") for p in (org or {}).get("persons") or [] if isinstance(p, dict)}
    rest = _rest_locs(org)
    out: dict[str, list] = {}
    legend: dict[str, str] = {}
    for e in (org or {}).get("events") or []:
        if not isinstance(e, dict) or not isinstance(e.get("loc"), str) or _int(e.get("t0"), None) is None:
            continue
        if str(e.get("id") or "").startswith("rest-") or e["loc"] in rest:
            continue
        subject, group = decisions.subject_of(plan, e)
        dept = str((decisions.requirement_of(plan, e) or {}).get("dept") or "")
        key, label = (f"d:{dept}", dept) if dept else (f"s:{subject}", subject or str(e.get("name") or "Lesson"))
        legend.setdefault(key, label)
        out.setdefault(e["loc"], []).append({
            "event": e.get("id"), "t0": _int(e["t0"], 0), "dur": min(max(_int(e.get("dur"), 1) or 1, 1), MAX_DUR),
            "subject": subject, "group": group,
            "teachers": [m for m in e.get("members") or [] if m in persons], "colour": key})
    for v in out.values():
        v.sort(key=lambda x: x["t0"])
    return out, legend


def people(org: dict, plan: dict | None) -> list[dict]:
    """[{id, name, dept}]: the department from the plan's staff, else the text after a comma in the
    person's role ("Teacher, Maths")."""
    staff = {s.get("id"): s for s in (plan or {}).get("staff") or [] if isinstance(s, dict)}
    out = []
    for p in (org or {}).get("persons") or []:
        if not isinstance(p, dict) or not p.get("id"):
            continue
        dept = (staff.get(p["id"]) or {}).get("dept") or ""
        if not dept and "," in str(p.get("role") or ""):
            dept = str(p["role"]).split(",", 1)[1].strip()
        out.append({"id": p["id"], "name": str(p.get("name") or p["id"]), "dept": str(dept)})
    return out


def clashes(check: dict | None, org: dict) -> list[dict]:
    """The check's clashes on rooms: [{type, message, rooms, slots, events}] for each clash with at least
    one placed event (the clash's `event` and its tiles' events)."""
    events = {e.get("id"): e for e in (org or {}).get("events") or [] if isinstance(e, dict)}
    out = []
    for c in (check or {}).get("clashes") or []:
        if not isinstance(c, dict):
            continue
        ids = list(dict.fromkeys(i for i in [c.get("event")] + [str(t).split("@", 1)[0] for t in c.get("tiles") or []] if i))
        rooms_, slots, placed = [], set(), []
        for i in ids:
            e = events.get(i)
            t0 = _int((e or {}).get("t0"), None)
            if not e or not isinstance(e.get("loc"), str) or t0 is None:
                continue
            placed.append(i)
            if e["loc"] not in rooms_:
                rooms_.append(e["loc"])
            slots.update(range(t0, t0 + min(max(_int(e.get("dur"), 1) or 1, 1), MAX_DUR)))
        if rooms_:
            out.append({"type": str(c.get("type") or ""), "message": str(c.get("message") or ""),
                        "rooms": rooms_, "slots": sorted(slots), "events": placed})
    return out


def time_of(org: dict, settings: dict) -> dict:
    """{labels, slots_per_day, days, start, slot_minutes}: the cycle is `len(labels) // slots_per_day` days
    (plan/model.py::cycle_days); slots per day from the organisation's rules, else the settings."""
    t = (settings or {}).get("time") or {}
    labels = list((org or {}).get("time_labels") or [])
    spd = _int(((org or {}).get("rules") or {}).get("slots_per_day"), None) or _int(t.get("slots_per_day"), None) or len(labels) or 1
    spd = max(spd, 1)
    return {"labels": labels, "slots_per_day": spd, "days": len(labels) // spd if labels else 0,
            "start": str(t.get("start") or "07:30"), "slot_minutes": _int(t.get("slot_minutes"), 40) or 40}


ROOM_W, ROOM_D, HALL_W, HALL_D, FIELD_W, FIELD_D = 2.6, 6.5, 9.0, 10.0, 16.0, 11.0
PER_FACADE = 10                       # rooms along one side of a floor before the back side is used
OTHER_LEVELS = 3                      # an "Other rooms" building's floors
GAP, MARGIN = 5.0, 6.0
HALL = re.compile(r"\b(hall|gym|gymnasium|auditorium|ish)\b", re.I)
FIELD = re.compile(r"\b(field|court|pitch|track|playground)\b", re.I)
HINT = "Add blocks and levels with your venue sheet or the template, and the campus is laid out by block."


def natural(s) -> list:
    return [(0, int(p), "") if p.isdigit() else (1, 0, p.lower()) for p in re.split(r"(\d+)", str(s or "")) if p]


def _role(r: dict) -> str:
    text = f"{r.get('kind') or ''} {r.get('name') or ''}"
    if FIELD.search(text):
        return "field"
    if HALL.search(text):
        return "hall"
    return "block" if r.get("block") else "other"


GROUND = {"g", "gf", "ground", "ground floor", "l0", "level 0", "0"}


def floor_of(level) -> tuple:
    """A sort key that reads a level as a floor: "2", "02", "L2" and "Level 2" are floor 2; "G", "Ground"
    and "L0" are 0; "B1" or "Basement" are below; anything else keeps its text after the numbered floors."""
    text = " ".join(str(level or "1").split()).lower()
    if text in GROUND:
        return (0, 0, "")
    m = re.fullmatch(r"(basement|b)\s*(\d*)", text)
    if m:
        return (0, -(int(m.group(2) or 1)), "")
    m = re.search(r"\d+", text)
    return (0, int(m.group()), "") if m else (1, 0, text)


def _per_facade(levels: list[list]) -> int:
    widest = max((len(l) for l in levels), default=1)
    return widest if widest <= PER_FACADE else -(-widest // 2)


def buildings(rooms_: list[dict]) -> tuple[list[dict], list[dict]]:
    """Buildings (unplaced) and field rooms. A block is a building with a floor per level (natural order;
    no level is "1"), rooms in natural name order; halls are one-room buildings; rooms with no block go to
    "Other rooms" buildings of up to OTHER_LEVELS floors of 2 × PER_FACADE rooms."""
    by_block: dict[str, list] = {}
    halls, others, fields = [], [], []
    for r in sorted(rooms_, key=lambda r: natural(r.get("name"))):
        role = _role(r)
        (fields if role == "field" else halls if role == "hall" else others if role == "other"
         else by_block.setdefault(str(r["block"]), [])).append(r)
    out = []
    for block in sorted(by_block, key=natural):
        floors: dict[tuple, list] = {}
        for r in by_block[block]:
            floors.setdefault(floor_of(r.get("level")), []).append(r["id"])
        levels = [floors[k] for k in sorted(floors)]
        label = f"Block {block}" if len(block) <= 3 else block
        wide = 2 * PER_FACADE                                    # a longer floor continues in another wing
        wings = max(-(-len(l) // wide) for l in levels)
        for w in range(wings):
            part = [l[w * wide:(w + 1) * wide] for l in levels]
            while part and not part[-1]:
                part.pop()
            out.append({"key": f"block:{block}" + (f":{w + 1}" if w else ""), "label": label + (f" · wing {w + 1}" if w else ""),
                        "kind": "block", "levels": part, "per_facade": _per_facade(part)})
    for r in halls:
        out.append({"key": f"hall:{r['id']}", "label": r["name"], "kind": "hall", "levels": [[r["id"]]], "per_facade": 1})
    per_building = OTHER_LEVELS * 2 * PER_FACADE
    for n, i in enumerate(range(0, len(others), per_building)):
        ids = [r["id"] for r in others[i:i + per_building]]
        levels = [ids[j:j + 2 * PER_FACADE] for j in range(0, len(ids), 2 * PER_FACADE)]
        out.append({"key": f"other:{n + 1}", "label": "Other rooms" + (f" {n + 1}" if n else ""), "kind": "other",
                    "levels": levels, "per_facade": _per_facade(levels)})
    return out, [{"room": r["id"], "name": r["name"]} for r in fields]


def _size(b: dict) -> tuple[float, float]:
    if b["kind"] == "hall":
        return HALL_W, HALL_D
    return round(b["per_facade"] * ROOM_W + 1.0, 2), ROOM_D


def place(blds: list[dict], fields: list[dict]) -> dict:
    """Rows of buildings north and south of a courtyard that holds the fields (one plain field when the
    school lists none), each row centred on x = 0; the grounds fit everything with a margin."""
    areas = [{"kind": "field", "room": f["room"], "name": f["name"], "w": FIELD_W, "d": FIELD_D} for f in fields] \
        or [{"kind": "field", "room": None, "name": "Field", "w": FIELD_W, "d": FIELD_D}]
    items = [dict(b, w=_size(b)[0], d=_size(b)[1]) for b in blds]
    area = sum(i["w"] * i["d"] for i in items) + sum(a["w"] * a["d"] for a in areas)
    row_w = max(36.0, (area ** 0.5) * 1.6, max((i["w"] for i in items), default=0))
    rows: list[list] = []
    for it in items:
        if rows and sum(x["w"] for x in rows[-1]) + GAP * len(rows[-1]) + it["w"] <= row_w:
            rows[-1].append(it)
        else:
            rows.append([it])
    court_rows: list[list] = []
    for a in areas:
        if court_rows and sum(x["w"] for x in court_rows[-1]) + GAP * len(court_rows[-1]) + a["w"] <= row_w:
            court_rows[-1].append(a)
        else:
            court_rows.append([a])
    north, south = rows[0::2], rows[1::2]

    def lay(row, z):
        width = sum(x["w"] for x in row) + GAP * (len(row) - 1)
        x = -width / 2
        for it in row:
            it["x"], it["z"] = round(x + it["w"] / 2, 2), round(z, 2)
            x += it["w"] + GAP
        return width

    widths = []
    court_d = sum(max(a["d"] for a in r) for r in court_rows) + GAP * (len(court_rows) - 1)
    z = -court_d / 2
    for r in court_rows:
        depth = max(a["d"] for a in r)
        widths.append(lay(r, z + depth / 2))
        z += depth + GAP
    top = -court_d / 2 - GAP
    for r in north:
        depth = max(i["d"] for i in r)
        widths.append(lay(r, top - depth / 2))
        top -= depth + GAP
    bottom = court_d / 2 + GAP
    for r in south:
        depth = max(i["d"] for i in r)
        widths.append(lay(r, bottom + depth / 2))
        bottom += depth + GAP
    # recentre on z = 0 so the grounds are symmetric
    shift = (top + GAP + bottom - GAP) / 2
    for it in items + areas:
        it["z"] = round(it["z"] - shift, 2)
    half_d = max(abs(it["z"]) + it["d"] / 2 for it in items + areas)
    half_w = max(abs(it["x"]) + it["w"] / 2 for it in items + areas)
    return {"buildings": items, "areas": areas, "grounds": {"w": round(2 * half_w + 2 * MARGIN, 2), "d": round(2 * half_d + 2 * MARGIN, 2)}}


def campus(org: dict, plan: dict | None, check: dict | None, settings: dict, source: str | None) -> dict:
    rs = rooms(org, plan)
    les, legend = lessons(org, plan)
    live = source == "live"
    return {"source": source, "rooms": rs, **place(*buildings(rs)), "time": time_of(org, settings),
            "lessons": les, "legend": legend, "people": people(org, plan),
            "clashes": clashes(check, org) if live else [], "checked_ok": bool(live and check and check.get("ok")),
            "hint": None if any(r["block"] for r in rs) else HINT}


def make_router(db) -> APIRouter:
    r = APIRouter()

    @r.get("/api/campus")
    def get_campus(sid: str = Depends(auth.require_session)):
        live = db.get_org("live")
        org = live or db.get_org("draft")
        source = "live" if live else "draft" if org else None
        return campus(org or {}, db.get_value("plan"), db.get_value("last_check") if live else None, db.get_settings(), source)

    return r
