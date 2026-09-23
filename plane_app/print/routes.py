"""Print pages and downloads. Reads the live organisation, settings and bookings; never the engine.
A dated week view reads the timetable in force on that date (a period timetable, else the base)."""
from __future__ import annotations

import secrets
from datetime import date as _date

from fastapi import APIRouter, Depends, HTTPException, Response
from fastapi.responses import HTMLResponse

from .. import auth, bookings, periods
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


def _in_force(db, date: str) -> tuple[str, str, dict, str | None]:
    """(base tid, tid, organisation, period name or None) for the timetable in force on `date`: 404 when
    that timetable has no live organisation. The injected bookings are stripped again — the week
    grid paints the base's bookings itself, as booking cells on their date."""
    try:
        tid, org = periods.org_for(db, date)
    except periods.PeriodError as e:
        raise HTTPException(400, str(e))
    if org is None:
        raise HTTPException(404, periods.no_live_message(db, tid, "no live timetable for that date"))
    base = periods.base_tid(db)
    hit = next((p for p in periods.in_force(db, date, base) if p["timetable"] == tid), None)
    return base, tid, bookings.strip(org), (hit["name"] if hit else None)


def _weeks(db, base: str, org: dict, grids: list[G.Grid], date: str, period: str | None) -> list[G.Grid]:
    """Each grid widened to its calendar week on the base's calendar and times, with the base's
    bookings; the subtitle names the period when one is in force."""
    s = db.get_settings(tid=base)
    items = bookings.list_all(db)
    try:
        out = [G.week_grid(org, g, s["calendar"], s["time"], date, items) for g in grids]
    except ValueError as e:
        raise HTTPException(400, str(e))
    if period:
        for w in out:
            w.subtitle = f"{w.subtitle} · {period}"
    return out


def view_in_force(db, kind: str, id: str, view: str, date: str | None) -> tuple[str, G.Grid]:
    """(tid the grid was read from, grid) for one teacher/group/class/room: the cycle grid of the
    selected live timetable, or for `view == "week"` the week of `date` (default today) in the
    timetable in force on that date."""
    if view != "week":
        return db.current_timetable(), resolve_grid(live_org(db), kind, id)
    date = date or _date.today().isoformat()
    base, tid, org, period = _in_force(db, date)
    [week] = _weeks(db, base, org, [resolve_grid(org, kind, id)], date, period)
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
    base, tid, org, period = _in_force(db, date)
    return tid, _weeks(db, base, org, G.all_grids(org, kind), date, period)


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
