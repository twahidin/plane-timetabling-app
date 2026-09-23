"""Print pages and downloads. Reads the live organisation, settings and bookings; never the engine.
A dated week view reads each weekday from the timetable in force that day (a period timetable, else
the base), with that day's relief covers."""
from __future__ import annotations

import secrets
from datetime import date as _date, timedelta

from fastapi import APIRouter, Depends, HTTPException, Response
from fastapi.responses import HTMLResponse

from .. import auth, bookings, calendar as cal_mod, periods, relief
from . import grid as G, html as H, pdf as P

KINDS = {"teacher": G.teacher_grid, "group": G.group_grid, "class": G.class_grid, "room": G.room_grid}
ALL = ("teachers", "classes", "rooms")


def live_org(db) -> dict:
    """The live organisation, or a 404 — shared by the print routes and `/api/grid`."""
    org = db.get_org("live")
    if org is None:
        raise HTTPException(404, "no live timetable")
    return org


def resolve_grid(org: dict, kind: str, id: str) -> G.Grid:
    """A teacher/group/class/room grid by kind and id, or a 404 — shared by the print routes and `/api/grid`."""
    if kind not in KINDS:
        raise HTTPException(404, f"unknown kind {kind}")
    try:
        return KINDS[kind](org, id)
    except KeyError as e:
        raise HTTPException(404, f"unknown {kind} {e.args[0]!r}")


def apply_week(db, org: dict, grids: list[G.Grid], view: str, date: str | None) -> list[G.Grid]:
    """Grids unchanged, unless `view == "week"`: then each is widened to its calendar week, with a
    400 if the calendar (e.g. `term_start`) isn't set up. Shared by the print routes and `/api/grid`."""
    if view != "week":
        return grids
    s = db.get_settings()
    try:
        return [G.week_grid(org, g, s["calendar"], s["time"], date or _date.today().isoformat(), bookings.list_all(db)) for g in grids]
    except ValueError as e:
        raise HTTPException(400, str(e))


def _in_force(db, date: str) -> tuple[str, str, dict]:
    """(base tid, tid, organisation) for the timetable in force on `date`: 404 when
    that timetable has no live organisation. The injected bookings are stripped again — the week
    grid paints the base's bookings itself, as booking cells on their date."""
    try:
        tid, org = periods.org_for(db, date)
    except periods.PeriodError as e:
        raise HTTPException(400, str(e))
    if org is None:
        raise HTTPException(404, periods.no_live_message(db, tid, "no live timetable for that date"))
    return periods.base_tid(db), tid, bookings.strip(org)


_WEEKDAYS = ("Mon", "Tue", "Wed", "Thu", "Fri")


def _weekdays(date: str) -> list[str]:
    monday = _date.fromisoformat(date) - timedelta(days=_date.fromisoformat(date).weekday())
    return [(monday + timedelta(days=i)).isoformat() for i in range(5)]


def _week_days(db, date: str, make) -> dict[str, tuple[str | None, dict[tuple[str, str], G.Grid] | None]]:
    """For each weekday of the week of `date`: (the timetable in force that day, (kind, id) -> the grid
    `make(org)` builds from that day's organisation, with that day's covers), the map None when that
    timetable has nothing built."""
    out: dict[str, tuple[str | None, dict[tuple[str, str], G.Grid] | None]] = {}
    for iso in _weekdays(date):
        try:
            tid, org = periods.org_for(db, iso)
        except periods.PeriodError:
            out[iso] = (None, None)
            continue
        out[iso] = (tid, None if org is None else {(g.kind, g.id): g for g in make(bookings.strip(org))})
    return out


def _runs(idx: list[int]) -> str:
    """Weekday indexes as "Thu–Fri", "Mon, Wed–Fri"."""
    runs: list[list[int]] = []
    for i in idx:
        if runs and runs[-1][-1] == i - 1:
            runs[-1].append(i)
        else:
            runs.append([i])
    return ", ".join(_WEEKDAYS[r[0]] if len(r) == 1 else f"{_WEEKDAYS[r[0]]}–{_WEEKDAYS[r[-1]]}" for r in runs)


def _period_words(db, base: str, date: str, cal: dict, time: dict) -> str:
    """The periods in force in the week of `date`, for its subtitle: "Camp" when one covers every
    school day of the week, "Camp (Thu–Fri)" when it covers some; "" when none."""
    school = [(i, iso) for i, iso in enumerate(_weekdays(date)) if cal_mod.cycle_day(cal, time, iso) is not None]
    by: dict[str, list[int]] = {}
    for i, iso in school:
        hits = periods.in_force(db, iso, base)
        if hits:                                       # the one `timetable_for` picks
            by.setdefault(hits[0]["name"], []).append(i)
    return " · ".join(name if len(idx) == len(school) else f"{name} ({_runs(idx)})" for name, idx in by.items())


def _weeks(db, base: str, org: dict, grids: list[G.Grid], date: str, make) -> list[G.Grid]:
    """Each grid widened to its calendar week on the base's calendar and times, with the base's
    bookings, each weekday read from that day's own organisation (`make` builds the grids from one)
    and its row marked with that day's timetable; a school day with nothing to show says why. The
    subtitle names the periods in force that week, with their weekdays when they cover only some."""
    s = db.get_settings(tid=base)
    items = bookings.list_all(db)
    days = _week_days(db, date, make)
    tids = {iso: tid for iso, (tid, _) in days.items() if tid is not None}
    out = []
    try:
        for g in grids:
            key, per, notes = (g.kind, g.id), {}, {}
            for iso, (_, m) in days.items():
                if m is None:
                    notes[iso] = "no timetable built"
                elif key in m:
                    per[iso] = m[key]
                else:
                    notes[iso] = "not in that day's timetable"
            out.append(G.week_grid(org, g, s["calendar"], s["time"], date, items, day_grids=per, day_tids=tids, day_notes=notes))
    except ValueError as e:
        raise HTTPException(400, str(e))
    words = _period_words(db, base, date, s["calendar"], s["time"])
    if words:
        for w in out:
            w.subtitle = f"{w.subtitle} · {words}"
    return out


def _built(build, org: dict, id: str) -> list[G.Grid]:
    """[the grid `build(org, id)` makes], or [] when that organisation has no such teacher/class/room."""
    try:
        return [build(org, id)]
    except KeyError:
        return []


def view_in_force(db, kind: str, id: str, view: str, date: str | None) -> tuple[str, G.Grid]:
    """(tid the grid was read from, grid) for one teacher/group/class/room: the cycle grid of the
    selected live timetable, or for `view == "week"` the week of `date` (default today) in the
    timetable in force on that date."""
    if view != "week":
        return db.current_timetable(), resolve_grid(live_org(db), kind, id)
    date = date or _date.today().isoformat()
    base, tid, org = _in_force(db, date)
    [week] = _weeks(db, base, org, [resolve_grid(org, kind, id)], date, lambda o: _built(KINDS[kind], o, id))
    return tid, week


def grids_for_view(db, kind: str, id: str, view: str, date: str | None) -> tuple[str, list[G.Grid]]:
    """`view_in_force`'s tid and its grid as a list — for the print routes."""
    tid, grid = view_in_force(db, kind, id, view, date)
    return tid, [grid]


def all_for_view(db, kind: str, view: str, date: str | None) -> tuple[str, list[G.Grid]]:
    """(tid read from, every grid of one kind: teachers, classes, rooms), by the same rules as `grids_for_view`."""
    if kind not in ALL:
        raise HTTPException(404, f"unknown kind {kind}")
    if view != "week":
        return db.current_timetable(), G.all_grids(live_org(db), kind)
    date = date or _date.today().isoformat()
    base, tid, org = _in_force(db, date)
    return tid, _weeks(db, base, org, G.all_grids(org, kind), date, lambda o: G.all_grids(o, kind))


def make_router(db, templates) -> APIRouter:
    r = APIRouter()

    def live() -> dict:
        return live_org(db)

    def meta(tid: str) -> tuple[str, str]:
        name = next((t["name"] for t in db.timetables() if t["id"] == tid), "")
        return _date.today().strftime("%-d %b %Y"), name

    def grids_for(kind: str, id: str, view: str, date: str | None) -> tuple[str, list[G.Grid]]:
        if kind == "custom":                            # picks ids of the selected live: stays on it
            org = live()
            spec = db.get_value(f"print_custom:{id}")
            if spec is None:
                raise HTTPException(404, "no such custom timetable")
            grids = [G.custom_grid(org, spec["title"], spec.get("persons", ()), spec.get("events", ()))]
            return db.current_timetable(), apply_week(db, org, grids, view, date)
        return grids_for_view(db, kind, id, view, date)

    def respond(tid_grids: tuple[str, list[G.Grid]], as_pdf: bool, filename: str):
        tid, grids = tid_grids                          # named after the timetable the grids were read from
        generated, name = meta(tid)
        if name:                                        # a period's week subtitle already ends with its name: once is enough
            for g in grids:
                if g.subtitle.endswith(f" · {name}"):
                    g.subtitle = g.subtitle[: -len(f" · {name}")]
        if as_pdf:
            return Response(P.render(grids, generated, name), media_type="application/pdf",
                            headers={"Content-Disposition": f'attachment; filename="{filename}.pdf"'})
        return HTMLResponse(H.render(templates, grids, generated, name))

    @r.get("/print/relief")
    def relief_ledger(since: str | None = None, sid: str = Depends(auth.require_session)):
        """The relief ledger as a printable page: each teacher's covers since `since` (default: the
        relief term start), most first — for claims and moderation."""
        try:
            rows = relief.ledger(db, since)
        except relief.ReliefError as e:
            raise HTTPException(400, str(e))
        start = relief.term_start(db) if since is None else str(since).strip()     # as `ledger` read it
        generated, name = meta(periods.base_tid(db))
        for row in rows:
            row["when"] = ", ".join(_date.fromisoformat(d).strftime("%a %-d %b") for d in row["dates"])
        since_words = _date.fromisoformat(start).strftime("%-d %b %Y") if start else ""
        return HTMLResponse(templates.env.get_template("print/relief.html").render(
            rows=rows, since=since_words, generated=generated, timetable=name))

    @r.get("/print/all/{kind}.pdf")
    def all_pdf(kind: str, view: str = "cycle", date: str | None = None, sid: str = Depends(auth.require_session)):
        return respond(all_for_view(db, kind, view, date), True, f"all-{kind}")

    @r.get("/print/all/{kind}")
    def all_html(kind: str, view: str = "cycle", date: str | None = None, sid: str = Depends(auth.require_session)):
        return respond(all_for_view(db, kind, view, date), False, f"all-{kind}")

    @r.get("/print/{kind}/{id}.pdf")
    def one_pdf(kind: str, id: str, view: str = "cycle", date: str | None = None, sid: str = Depends(auth.require_session)):
        return respond(grids_for(kind, id, view, date), True, f"{kind}-{id}")

    @r.get("/print/{kind}/{id}")
    def one_html(kind: str, id: str, view: str = "cycle", date: str | None = None, sid: str = Depends(auth.require_session)):
        return respond(grids_for(kind, id, view, date), False, f"{kind}-{id}")

    @r.get("/api/print/targets")
    def targets(sid: str = Depends(auth.require_session)):
        org = live()
        groups = org.get("groups", [])
        return {"teachers": [{"id": p["id"], "name": p["name"]} for p in sorted(org["persons"], key=lambda p: p["name"]) if str(p.get("role", "")).startswith("Teacher")],
                "groups": [{"id": g["id"], "name": g["name"]} for g in sorted(groups, key=lambda g: g["name"])],
                "classes": sorted({c for g in groups if g.get("band") is None for c in g["classes"]}),
                "rooms": [{"id": l["id"], "name": l["name"]} for l in sorted(org["locations"], key=lambda l: l["name"]) if not l.get("rest")]}

    @r.post("/api/print/custom")
    def custom(body: dict, sid: str = Depends(auth.require_session)):
        org = live()
        persons, events = list(body.get("persons") or []), list(body.get("events") or [])
        known_p, known_e = {p["id"] for p in org["persons"]}, {e["id"] for e in org["events"]}
        bad = [x for x in persons if x not in known_p] + [x for x in events if x not in known_e]
        if bad or not (persons or events):
            raise HTTPException(422, f"unknown ids {bad}" if bad else "choose at least one person or event")
        token = secrets.token_hex(4)
        db.set_value(f"print_custom:{token}", {"title": str(body.get("title") or "Custom timetable")[:80], "persons": persons, "events": events})
        return {"token": token}

    return r
