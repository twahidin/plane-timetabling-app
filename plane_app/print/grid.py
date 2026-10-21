"""The grid model behind every printout: days down, slots across, one cell per slot."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date as _date, timedelta
from os.path import commonprefix

from .. import calendar as cal_mod, students as S, week as week_mod
from ..plan.generate import class_group_id


@dataclass
class Cell:
    kind: str                       # lesson | booking | rest | break | free | continued
    text: str = ""
    sub: str = ""
    span: int = 1
    events: list[str] = field(default_factory=list)   # ids of the events in a lesson cell (stacked overlaps too)
    bad: list[str] = field(default_factory=list)      # problems said on the cell itself (a lesson sitting in a break band)


@dataclass
class DayRow:
    label: str
    cells: list[Cell]
    off: bool = False
    timetable: str | None = None    # a dated week's row: the timetable in force that day (None: the grid's own)
    slots: list[str] = field(default_factory=list)   # this day's own header, a label a cell (empty: the grid's; always empty for a legacy week)
    ts: list[int] = field(default_factory=list)      # the absolute slot of each cell (empty: counted from the day's start)


@dataclass
class Grid:
    title: str
    subtitle: str
    slots: list[str]
    days: list[DayRow]
    kind: str
    id: str
    note: str = ""      # a line under the subtitle, e.g. how many students a pile left out

    @property
    def columns(self) -> int:
        """The most cells any day has: the width of the table."""
        return max([len(d.cells) for d in self.days] + [len(self.slots)])

    @property
    def uniform(self) -> bool:
        """Every day's header is the grid's: one shared header row serves (days of an old grid that
        carry no header of their own are the grid's)."""
        return all(not d.slots or d.slots == self.slots for d in self.days)


def _spd(org: dict) -> int:
    labels = org["time_labels"]
    return int(org.get("rules", {}).get("slots_per_day") or len(labels) or 1)


def day_columns(org: dict, time: dict | None = None, rules: dict | None = None):
    """Per day: its name, its header (period word or break name, no day prefix), the absolute slot
    `t` of each column, each column's kind ("period" | "break") and, for the per-day path, each
    column's band text (a break's "Recess 09:30–09:50", None for a period). With `time` whose
    non-legacy week has the same labels as the organisation, a day has only its open positions
    (closed ones are not columns); otherwise the stored labels are chunked a day at a time, as
    before, and the band list is None."""
    if time:
        s = week_mod.shape(time, rules)
        if not s["legacy"] and s["labels"] == list(org.get("time_labels") or []):
            names, heads, ts, kinds, bands = [], [], [], [], []
            for k, name in enumerate(s["days"]):
                ps = [p for p in s["positions"] if p["day"] == k and p["kind"] != "closed"]
                names.append(name)
                heads.append([p["name"] if p["kind"] == "break" else (p["start"] if time.get("label_style") == "clock" else f"P{p['period']}") for p in ps])
                ts.append([p["t"] for p in ps])
                kinds.append([p["kind"] for p in ps])
                bands.append([f"{p['name']} {p['start']}–{p['end']}" if p["kind"] == "break" else None for p in ps])
            return names, heads, ts, kinds, bands
    labels, spd = org["time_labels"], _spd(org)
    days, slots = _chunked(labels, spd)
    return days, slots, [list(range(k * spd, k * spd + len(sl))) for k, sl in enumerate(slots)], [["period"] * len(sl) for sl in slots], None


def _chunked(labels: list[str], spd: int) -> tuple[list[str], list[list[str]]]:
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


def day_slot_labels(org: dict, time: dict | None = None, rules: dict | None = None) -> tuple[list[str], list[list[str]]]:
    """(day names, each day's header). See `day_columns`."""
    days, slots = day_columns(org, time, rules)[:2]
    return days, slots


NAMES_SHOWN = 3          # names a cell lists before "+N more"


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
    # A whole-school event lists everyone: show the first few and count the rest, so the cell stays short.
    more = len(names) - NAMES_SHOWN
    shown = " · ".join(parts + names[:NAMES_SHOWN])[:80]
    return f"{shown} · +{more} more" if more > 0 else shown


def _empty(cols) -> list[DayRow]:
    days, slots, ts, kinds, bands = cols
    per_day = bands is not None                          # a legacy week's rows carry no header of their own
    return [DayRow(d, [Cell("break", bands[i][j]) if kinds[i][j] == "break" else Cell("free") for j in range(len(slots[i]))],
                   slots=list(slots[i]) if per_day else [], ts=list(ts[i]))
            for i, d in enumerate(days)]


def _place(org: dict, index: tuple[dict, dict, dict], rows: list[DayRow], events: list[dict],
           exclude: set[str], with_room: bool, cols=None) -> None:
    """Place each event in its day/slot cell. Any event that starts in an already-occupied slot — a
    `lesson` cell, or a `continued` cell belonging to an earlier lesson's span — stacks onto that lesson
    rather than vanishing: text and sub joined by " / ", span extended to cover the later end. Nothing on
    a teacher, group, class or room grid is ever silently dropped; a genuine double-booking still shows,
    stacked, rather than disappearing from the printout. A lesson's span is clamped to the slots remaining
    in its day so it never overflows past the last slot, and stops at a break band. A lesson that starts
    in a break band stays on the band (its id in the cell's events, the problem in its `bad`). A lesson in
    a closed position (one no column stands for) has no cell to go in."""
    locs = index[1]
    where = {t: (d, c) for d, row in enumerate(rows) for c, t in enumerate(row.ts)}
    for e in sorted(events, key=lambda x: (x["t0"], x["id"])):
        if e["t0"] not in where:
            continue
        d, s = where[e["t0"]]
        cells, ts = rows[d].cells, rows[d].ts
        text, sub = e["name"], _sub(index, e, exclude, with_room)
        rest = locs.get(e.get("loc"), {}).get("rest", False)
        if rest:
            if cells[s].kind == "free":
                cells[s] = Cell("rest", "Rest")
            continue
        if cells[s].kind == "break":                      # a lesson in a break: the band stays, marked with it
            cells[s].events.append(e["id"])
            cells[s].bad.append(f"{text} sits in {rows[d].slots[s]}")
            continue
        span = 1
        while s + span < len(cells) and ts[s + span] < e["t0"] + e["dur"] and cells[s + span].kind != "break":
            span += 1
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


def _grid(org: dict, kind: str, id: str, title: str, events: list[dict], exclude: set[str], with_room: bool,
          time: dict | None = None, rules: dict | None = None) -> Grid:
    cols = day_columns(org, time, rules)
    rows = _empty(cols)
    _place(org, _names(org), rows, events, exclude, with_room, cols)   # one name index for the whole grid
    return Grid(title, org.get("name", ""), cols[1][0] if cols[1] else [], rows, kind, id)


def teacher_grid(org: dict, pid: str, time: dict | None = None, rules: dict | None = None) -> Grid:
    p = next(p for p in org["persons"] if p["id"] == pid) if any(p["id"] == pid for p in org["persons"]) else None
    if p is None:
        raise KeyError(pid)
    return _grid(org, "teacher", pid, p["name"], _placed(org, lambda e: pid in e["members"]), {pid}, True, time, rules)


def group_grid(org: dict, gid: str, time: dict | None = None, rules: dict | None = None) -> Grid:
    g = next((g for g in org.get("groups", []) if g["id"] == gid), None)
    if g is None:
        raise KeyError(gid)
    return _grid(org, "group", gid, g["name"], _placed(org, lambda e: gid in e["members"]), {gid}, True, time, rules)


def class_grid(org: dict, cls: str, time: dict | None = None, rules: dict | None = None) -> Grid:
    ids = {g["id"] for g in org.get("groups", []) if cls.lower() in (c.lower() for c in g["classes"])}
    if not ids:
        raise KeyError(cls)
    return _grid(org, "class", cls, cls, _placed(org, lambda e: bool(ids & set(e["members"]))), ids, True, time, rules)


def room_grid(org: dict, lid: str, time: dict | None = None, rules: dict | None = None) -> Grid:
    l = next((l for l in org["locations"] if l["id"] == lid), None)
    if l is None:
        raise KeyError(lid)
    return _grid(org, "room", lid, l["name"], _placed(org, lambda e: e["loc"] == lid), set(), False, time, rules)


def custom_grid(org: dict, title: str, persons=(), events=(), time: dict | None = None, rules: dict | None = None) -> Grid:
    ps, es = set(persons), set(events)
    return _grid(org, "custom", "custom", title, _placed(org, lambda e: e["id"] in es or bool(ps & set(e["members"]))), ps, True, time, rules)


def student_grid(org: dict, plan: dict, student: dict, time: dict | None = None, rules: dict | None = None) -> Grid:
    """One student's own week: their class's whole-class lessons and their own option groups only, so a
    banded slot shows their option alone and nothing is stacked. Titled with the name, subtitled
    "ID · class". `plan` is accepted for the callers that hold it; the lessons come from the organisation."""
    mine = set(S.student_events(student, org))
    own = {S.group_id(t) for t in student["takes"]} | {class_group_id(student["class"])}
    g = _grid(org, "student", student["id"], student["name"] or student["id"],
              _placed(org, lambda e: e["id"] in mine), own, True, time, rules)
    g.subtitle = f"{student['id']} · {student['class']}"
    return g


def all_grids(org: dict, kind: str, time: dict | None = None, rules: dict | None = None) -> list[Grid]:
    if kind == "teachers":
        ids = sorted((p for p in org["persons"] if str(p.get("role", "")).startswith("Teacher")), key=lambda p: p["name"])
        return [teacher_grid(org, p["id"], time, rules) for p in ids]
    if kind == "classes":
        classes = sorted({c for g in org.get("groups", []) if g.get("band") is None for c in g["classes"]})
        return [class_grid(org, c, time, rules) for c in classes]
    if kind == "rooms":
        ls = sorted((l for l in org["locations"] if not l.get("rest")), key=lambda l: l["name"])
        return [room_grid(org, l["id"], time, rules) for l in ls]
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
            own = grid.days[k] if k is not None and k < len(grid.days) else None     # an off row has its cycle day's columns
            heads = list(own.slots) if own and own.slots else list(grid.slots)
            rows.append(DayRow(f"{label} · {why}" if why else label, [Cell("free") for _ in heads], off=True, timetable=tid,
                               slots=list(own.slots) if own else [], ts=list(own.ts) if own else []))
            continue
        day = src.days[k]
        cells = [Cell(c.kind, c.text, c.sub, c.span, list(c.events), list(c.bad)) for c in day.cells]
        ts = list(day.ts) or list(range(k * _spd(org), k * _spd(org) + len(cells)))     # absolute slot a column
        if grid.kind == "room":
            for b in bookings:
                if b["venue"] == grid.id and b["date"] == iso and b["start"] in ts:
                    s = ts.index(b["start"])                      # a booking in a closed position is in no column: skipped
                    span = 1
                    while s + span < len(ts) and ts[s + span] < b["start"] + b["dur"]:
                        span += 1
                    cells[s] = Cell("booking", b["title"], b.get("booked_by", ""), span)
                    for t in range(s + 1, s + span):
                        cells[t] = Cell("continued")
        rows.append(DayRow(label, cells, timetable=tid, slots=list(day.slots), ts=ts))
    return Grid(grid.title, f"Week of {monday.strftime('%-d %B %Y')}", grid.slots, rows, grid.kind, grid.id)
