"""The grid model behind every printout: days down, slots across, one cell per slot."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date as _date, timedelta
from os.path import commonprefix

from .. import calendar as cal_mod


@dataclass
class Cell:
    kind: str                       # lesson | booking | rest | free | continued
    text: str = ""
    sub: str = ""
    span: int = 1
    events: list[str] = field(default_factory=list)   # ids of the events in a lesson cell (stacked overlaps too)


@dataclass
class DayRow:
    label: str
    cells: list[Cell]
    off: bool = False
    timetable: str | None = None    # a dated week's row: the timetable in force that day (None: the grid's own)


@dataclass
class Grid:
    title: str
    subtitle: str
    slots: list[str]
    days: list[DayRow]
    kind: str
    id: str


def _spd(org: dict) -> int:
    labels = org["time_labels"]
    return int(org.get("rules", {}).get("slots_per_day") or len(labels) or 1)


def day_slot_labels(org: dict) -> tuple[list[str], list[list[str]]]:
    labels, spd = org["time_labels"], _spd(org)
    days, slots = [], []
    for k in range(0, len(labels), spd):
        chunk = labels[k:k + spd]
        prefix = commonprefix(chunk) if len(chunk) > 1 else ""
        if " " in prefix:
            prefix = prefix[:prefix.rindex(" ")]
        else:
            prefix = ""
        days.append(prefix or f"Day {k // spd + 1}")
        slots.append([c[len(prefix):].strip() if prefix else c for c in chunk])
    return days, slots


def _names(org: dict) -> tuple[dict, dict, dict]:
    persons = {p["id"]: p for p in org["persons"]}
    locs = {l["id"]: l for l in org["locations"]}
    groups = {g["id"]: g for g in org.get("groups", [])}
    return persons, locs, groups


def _sub(index: tuple[dict, dict, dict], e: dict, exclude: set[str], with_room: bool) -> str:
    persons, locs, groups = index
    parts = []
    if with_room and e.get("loc") in locs:
        parts.append(locs[e["loc"]]["name"])
    names = [groups[m]["name"] if m in groups else persons[m]["name"] for m in e["members"] if m not in exclude and (m in persons or m in groups)]
    return " · ".join(parts + names)[:80]


def _empty(org: dict) -> list[DayRow]:
    days, slots = day_slot_labels(org)
    return [DayRow(d, [Cell("free") for _ in slots[i]]) for i, d in enumerate(days)]


def _place(org: dict, index: tuple[dict, dict, dict], rows: list[DayRow], events: list[dict],
           exclude: set[str], with_room: bool) -> None:
    """Place each event in its day/slot cell. Any event that starts in an already-occupied slot — a
    `lesson` cell, or a `continued` cell belonging to an earlier lesson's span — stacks onto that lesson
    rather than vanishing: text and sub joined by " / ", span extended to cover the later end. Nothing on
    a teacher, group, class or room grid is ever silently dropped; a genuine double-booking still shows,
    stacked, rather than disappearing from the printout. A lesson's span is clamped to the slots remaining
    in its day so it never overflows past the last slot."""
    spd, locs = _spd(org), index[1]
    for e in sorted(events, key=lambda x: (x["t0"], x["id"])):
        d, s = divmod(e["t0"], spd)
        if d >= len(rows) or s >= len(rows[d].cells):
            continue
        cells = rows[d].cells
        rest = locs.get(e.get("loc"), {}).get("rest", False)
        if rest:
            if cells[s].kind == "free":
                cells[s] = Cell("rest", "Rest")
            continue
        text, sub = e["name"], _sub(index, e, exclude, with_room)
        span = min(e["dur"], len(cells) - s)
        owner = s
        while owner > 0 and cells[owner].kind == "continued":
            owner -= 1
        if cells[owner].kind == "lesson":
            oc = cells[owner]                             # an overlap with an earlier lesson: stack onto it
            oc.text += " / " + text
            oc.sub += " / " + sub
            oc.events.append(e["id"])
            end = max(owner + oc.span, s + span)
            oc.span = end - owner
        elif cells[s].kind == "free":
            cells[s] = Cell("lesson", text, sub, span, [e["id"]])
            owner, end = s, s + span
        else:
            continue                                      # e.g. a rest tile already occupies this slot
        for t in range(owner + 1, min(end, len(cells))):
            if cells[t].kind == "free":
                cells[t] = Cell("continued")


def _placed(org: dict, pred) -> list[dict]:
    return [e for e in org["events"] if e.get("t0") is not None and e.get("loc") is not None and pred(e)]


def _grid(org: dict, kind: str, id: str, title: str, events: list[dict], exclude: set[str], with_room: bool) -> Grid:
    rows = _empty(org)
    _place(org, _names(org), rows, events, exclude, with_room)   # one name index for the whole grid
    _, slots = day_slot_labels(org)
    return Grid(title, org.get("name", ""), slots[0] if slots else [], rows, kind, id)


def teacher_grid(org: dict, pid: str) -> Grid:
    p = next(p for p in org["persons"] if p["id"] == pid) if any(p["id"] == pid for p in org["persons"]) else None
    if p is None:
        raise KeyError(pid)
    return _grid(org, "teacher", pid, p["name"], _placed(org, lambda e: pid in e["members"]), {pid}, True)


def group_grid(org: dict, gid: str) -> Grid:
    g = next((g for g in org.get("groups", []) if g["id"] == gid), None)
    if g is None:
        raise KeyError(gid)
    return _grid(org, "group", gid, g["name"], _placed(org, lambda e: gid in e["members"]), {gid}, True)


def class_grid(org: dict, cls: str) -> Grid:
    ids = {g["id"] for g in org.get("groups", []) if cls.lower() in (c.lower() for c in g["classes"])}
    if not ids:
        raise KeyError(cls)
    return _grid(org, "class", cls, cls, _placed(org, lambda e: bool(ids & set(e["members"]))), ids, True)


def room_grid(org: dict, lid: str) -> Grid:
    l = next((l for l in org["locations"] if l["id"] == lid), None)
    if l is None:
        raise KeyError(lid)
    return _grid(org, "room", lid, l["name"], _placed(org, lambda e: e["loc"] == lid), set(), False)


def custom_grid(org: dict, title: str, persons=(), events=()) -> Grid:
    ps, es = set(persons), set(events)
    return _grid(org, "custom", "custom", title, _placed(org, lambda e: e["id"] in es or bool(ps & set(e["members"]))), ps, True)


def all_grids(org: dict, kind: str) -> list[Grid]:
    if kind == "teachers":
        ids = sorted((p for p in org["persons"] if str(p.get("role", "")).startswith("Teacher")), key=lambda p: p["name"])
        return [teacher_grid(org, p["id"]) for p in ids]
    if kind == "classes":
        classes = sorted({c for g in org.get("groups", []) if g.get("band") is None for c in g["classes"]})
        return [class_grid(org, c) for c in classes]
    if kind == "rooms":
        ls = sorted((l for l in org["locations"] if not l.get("rest")), key=lambda l: l["name"])
        return [room_grid(org, l["id"]) for l in ls]
    raise KeyError(kind)


def week_grid(org: dict, grid: Grid, cal: dict, time: dict, date: str, bookings=(), day_grids: dict[str, Grid] | None = None,
              day_tids: dict[str, str] | None = None, day_notes: dict[str, str] | None = None) -> Grid:
    """The Monday-to-Friday week of `date`, each weekday's cells taken from its cycle day. With
    `day_grids` (ISO date -> this grid built from that date's organisation: the timetable in force that
    day, with that day's covers), each weekday reads its own grid, and a weekday missing from it is an
    off row; without, every weekday reads `grid`. `day_tids` names each row's timetable
    (`DayRow.timetable`); `day_notes` says why a school day has no grid ("no timetable built"), added
    to that off row's label so it is not taken for a holiday."""
    if not cal.get("term_start"):
        raise ValueError("set the term calendar in Settings to print a dated week")
    d = _date.fromisoformat(date)
    monday = d - timedelta(days=d.weekday())
    rows = []
    for i in range(5):
        day = monday + timedelta(days=i)
        iso, label = day.isoformat(), day.strftime("%a %-d %b")
        k = cal_mod.cycle_day(cal, time, iso)
        src = grid if day_grids is None else day_grids.get(iso)
        tid = (day_tids or {}).get(iso)
        if k is None or src is None or k >= len(src.days):
            why = (day_notes or {}).get(iso) if k is not None and src is None else None
            rows.append(DayRow(f"{label} · {why}" if why else label, [Cell("free") for _ in grid.slots], off=True, timetable=tid))
            continue
        cells = [Cell(c.kind, c.text, c.sub, c.span, list(c.events)) for c in src.days[k].cells]
        if grid.kind == "room":
            spd = _spd(org)
            for b in bookings:
                if b["venue"] == grid.id and b["date"] == iso:
                    s = b["start"] - k * spd
                    if 0 <= s < len(cells):
                        cells[s] = Cell("booking", b["title"], b.get("booked_by", ""), b["dur"])
                        for t in range(s + 1, min(s + b["dur"], len(cells))):
                            cells[t] = Cell("continued")
        rows.append(DayRow(label, cells, timetable=tid))
    return Grid(grid.title, f"Week of {monday.strftime('%-d %B %Y')}", grid.slots, rows, grid.kind, grid.id)
