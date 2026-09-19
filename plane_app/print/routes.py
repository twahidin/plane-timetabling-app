"""Print pages and downloads. Reads the live organisation, settings and bookings; never the engine."""
from __future__ import annotations

import secrets
from datetime import date as _date

from fastapi import APIRouter, Depends, HTTPException, Response
from fastapi.responses import HTMLResponse

from .. import auth, bookings
from . import grid as G, html as H, pdf as P

KINDS = {"teacher": G.teacher_grid, "group": G.group_grid, "class": G.class_grid, "room": G.room_grid}
ALL = ("teachers", "classes", "rooms")


def make_router(db, templates) -> APIRouter:
    r = APIRouter()

    def live() -> dict:
        org = db.get_org("live")
        if org is None:
            raise HTTPException(404, "no live timetable")
        return org

    def meta() -> tuple[str, str]:
        name = next((t["name"] for t in db.timetables() if t["id"] == db.current_timetable()), "")
        return _date.today().strftime("%-d %b %Y"), name

    def grids_for(kind: str, id: str) -> list[G.Grid]:
        org = live()
        try:
            if kind == "custom":
                spec = db.get_value(f"print_custom:{id}")
                if spec is None:
                    raise HTTPException(404, "no such custom timetable")
                return [G.custom_grid(org, spec["title"], spec.get("persons", ()), spec.get("events", ()))]
            if kind not in KINDS:
                raise HTTPException(404, f"unknown kind {kind}")
            return [KINDS[kind](org, id)]
        except KeyError as e:
            raise HTTPException(404, f"unknown {kind} {e.args[0]!r}")

    def apply_view(grids: list[G.Grid], view: str, date: str | None) -> list[G.Grid]:
        if view != "week":
            return grids
        s = db.get_settings()
        try:
            return [G.week_grid(live(), g, s["calendar"], s["time"], date or _date.today().isoformat(), bookings.list_all(db)) for g in grids]
        except ValueError as e:
            raise HTTPException(400, str(e))

    def respond(grids, as_pdf: bool, filename: str):
        generated, name = meta()
        if as_pdf:
            return Response(P.render(grids, generated, name), media_type="application/pdf",
                            headers={"Content-Disposition": f'attachment; filename="{filename}.pdf"'})
        return HTMLResponse(H.render(templates, grids, generated, name))

    def _all(kind: str):
        if kind not in ALL:
            raise HTTPException(404, f"unknown kind {kind}")
        return G.all_grids(live(), kind)

    @r.get("/print/all/{kind}.pdf")
    def all_pdf(kind: str, view: str = "cycle", date: str | None = None, sid: str = Depends(auth.require_session)):
        return respond(apply_view(_all(kind), view, date), True, f"all-{kind}")

    @r.get("/print/all/{kind}")
    def all_html(kind: str, view: str = "cycle", date: str | None = None, sid: str = Depends(auth.require_session)):
        return respond(apply_view(_all(kind), view, date), False, f"all-{kind}")

    @r.get("/print/{kind}/{id}.pdf")
    def one_pdf(kind: str, id: str, view: str = "cycle", date: str | None = None, sid: str = Depends(auth.require_session)):
        return respond(apply_view(grids_for(kind, id), view, date), True, f"{kind}-{id}")

    @r.get("/print/{kind}/{id}")
    def one_html(kind: str, id: str, view: str = "cycle", date: str | None = None, sid: str = Depends(auth.require_session)):
        return respond(apply_view(grids_for(kind, id), view, date), False, f"{kind}-{id}")

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
