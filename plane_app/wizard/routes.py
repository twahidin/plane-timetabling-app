"""HTTP routes for the start wizard (spec docs/superpowers/specs/2026-09-20-start-wizard-design.md
§4, §7). The model (via `chat.TOOLS`) drives the conversation; these routes render what it chooses:
the template library, a preview of a candidate's facts, and — once the user is happy — the settings,
the plan's vocabulary and the three downloads (workbook, sample PDF, guide). No LLM is involved here.

`resolve` and `instantiate` are also called directly by the `wizard_preview`/`wizard_instantiate`
chat tools (`chat.py`), so the route handlers and the tools write the same thing the same way."""
from __future__ import annotations

import time as _time

from fastapi import APIRouter, Depends, HTTPException, Response
from fastapi.responses import HTMLResponse

from .. import auth
from ..engine_client import EngineError
from ..plan import model as M
from . import instantiate as I
from . import library as L

# The three links the wizard hands out once a template is chosen; the same shape a route response
# and a `wizard_instantiate` tool event both carry.
DOWNLOADS = {"workbook": "/api/wizard/workbook.xlsx", "sample": "/api/wizard/sample.pdf", "guide": "/api/wizard/guide"}


def resolve(template_id, knobs) -> tuple[dict, dict]:
    """The template and its checked knobs for an id and knob values a caller gave. Raises `KeyError`
    for an unknown template id, `library.WizardError` for a knob out of range — each caller (an HTTP
    route or a chat tool) turns that into its own error shape."""
    template = L.get(str(template_id or ""))
    return template, L.check_knobs(template, knobs if isinstance(knobs, dict) else {})


def instantiate(db, template: dict, knobs: dict) -> dict:
    """Write the timetable's settings (through the same cleaning `PUT /api/settings` uses), the
    wizard's own record of the choice, and the plan's vocabulary (creating an empty plan when there
    is none yet); return the facts and the three download links."""
    settings = I.settings_for(template, knobs, db.get_settings())
    db.store_settings({"time": settings["time"], "rules": settings["rules"]})
    db.set_value("wizard", {"template": template["id"], "knobs": knobs, "chosen_at": _time.time()})
    plan = db.get_value("plan") or M.empty_plan()
    db.set_value("plan", M.normalise({**plan, "vocabulary": dict(template["vocabulary"])}))
    return {"facts": I.facts(template, knobs), "downloads": dict(DOWNLOADS)}


def make_router(db, templates, engine) -> APIRouter:
    r = APIRouter()

    def _template(template_id) -> dict:
        try:
            return L.get(str(template_id or ""))
        except KeyError:
            raise HTTPException(404, f"no such template {template_id!r}")

    def _resolved(body: dict) -> tuple[dict, dict]:
        body = body if isinstance(body, dict) else {}
        try:
            return resolve(body.get("template"), body.get("knobs"))
        except KeyError:
            raise HTTPException(404, f"no such template {body.get('template')!r}")
        except L.WizardError as e:
            raise HTTPException(422, str(e))

    def _chosen() -> tuple[dict, dict]:
        """The template and knobs of the wizard's record on this timetable, or a 404 before one
        has been instantiated."""
        record = db.get_value("wizard")
        if record is None:
            raise HTTPException(404, "the wizard has not been used on this timetable yet")
        return _template(record["template"]), record["knobs"]

    @r.get("/api/wizard/library")
    def library(sid: str = Depends(auth.require_session)):
        return L.domains()

    @r.post("/api/wizard/preview")
    def preview(body: dict, sid: str = Depends(auth.require_session)):
        template, knobs = _resolved(body)
        return I.facts(template, knobs)

    @r.post("/api/wizard/instantiate")
    def instantiate_route(body: dict, sid: str = Depends(auth.require_session)):
        template, knobs = _resolved(body)
        result = instantiate(db, template, knobs)
        return {"ok": True, **result}

    @r.get("/api/wizard")
    def get_wizard(sid: str = Depends(auth.require_session)):
        return db.get_value("wizard") or {}

    @r.get("/api/wizard/workbook.xlsx")
    def workbook(sid: str = Depends(auth.require_session)):
        template, knobs = _chosen()
        return Response(I.workbook_bytes(template, knobs),
                        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                        headers={"Content-Disposition": 'attachment; filename="template.xlsx"'})

    @r.get("/api/wizard/sample.pdf")
    def sample_pdf(sid: str = Depends(auth.require_session)):
        template, knobs = _chosen()
        try:
            data = I.sample_pdf(engine(), template, knobs, db.get_settings(), templates)
        except EngineError as e:
            raise HTTPException(502 if e.status in (0, 500) else e.status, e.detail)
        except L.WizardError as e:
            raise HTTPException(502, str(e))
        return Response(data, media_type="application/pdf",
                        headers={"Content-Disposition": 'attachment; filename="sample.pdf"'})

    @r.get("/api/wizard/guide", response_class=HTMLResponse)
    def guide(sid: str = Depends(auth.require_session)):
        template, knobs = _chosen()
        return HTMLResponse(I.guide_html(templates, template, knobs))

    return r
