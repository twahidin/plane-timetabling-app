"""The chat loop: the model asks questions of the timetable and shapes the draft; tools do the work."""
from __future__ import annotations

import json
import re
import secrets
from dataclasses import dataclass, field
from urllib.parse import quote

from . import bookings
from . import calendar as cal_mod
from . import names
from . import proposals
from .db import Db
from .engine_client import EngineClient, EngineError
from .intake import IntakeError, apply_patch, empty_organisation, summarise
from .llm import Provider, ProviderError, ToolCall, ToolSpec
from .plan import generate as plan_generate_mod
from .plan.issues import ISSUE_LIMIT, Issue, capped, has_blocks, plan_issues
from .plan.model import apply_patch as apply_plan_patch, empty_plan
from .promote import promote_build
from .wizard import instantiate as wiz_instantiate
from .wizard import library as wiz_library
from .wizard.routes import instantiate as wizard_apply, resolve as wizard_resolve

MAX_ROUNDS = 8
# A bare yes applies the first pending proposal without troubling the model.
CONFIRM_WORDS = {"yes", "y", "ok", "okay", "apply", "go ahead", "do it", "confirm", "yes please"}
# And a bare no drops the card, again without troubling the model: the offer is gone, not deferred.
DECLINE_WORDS = {"no", "cancel", "dismiss", "never mind", "stop"}
# Every user message gets a run token, and every card is stamped with the run that made it. The
# `apply` tool refuses a card from the current run: the model may not propose and apply inside one
# message, because the user has not seen the options yet. Consent is a later message, a confirmation
# word, or a click on the apply route — never the model's own say-so.
NOT_YET = "show the options and wait for the user's yes"

SYSTEM_PROMPT = """You are the timetable assistant for one organisation. The timetable is a solid: every person is a
plane, time runs along it, location runs up it, and a lesson or shift is a prism through the planes of its members.
There is a LIVE timetable (built and checked) and possibly a DRAFT organisation extracted from uploaded documents.
Use the tools: where, who, both_free and load answer questions about the live timetable; list_draft shows the draft;
update_draft changes it with a patch keyed by id; build_draft asks the engine to build it. When the user describes an
organisation from scratch (criteria, numbers of people and rooms, subjects, constraints) and there is no draft, call
new_draft first, then fill it with update_draft: rooms with capacities, people with avail windows
[[first, one past last], ...] (one window for full-time staff) and eligible rooms (always include "rest"), and
events with members and duration; then ask the user to check
the tables before building. Never claim a build
succeeded unless build_draft returned ok: true. Quote the tool's answer and keep replies short. Slots are numbered
from 0 and ids are short lowercase strings; if the user uses a name, look it up in the draft or ask.
Changing the live timetable is done by proposals: `propose` (for an event, or every clash of a person), `book` and
`undo` show an option and wait. Never call `apply` in the same reply that made an option — show the options, stop,
and call `apply` with the option's id only in a later message, after the user has said yes to it. Resolve names with
`find` before any tool that takes an id; when `find` returns several matches, ask which. `clashes` lists what the
Reviewer found; `free_venues` lists rooms free at a slot or on a date. `print_timetable` returns links to a
printable page and a PDF; give the user both.
There is also a curriculum PLAN, separate from the draft: requirements (a group's periods by lesson length,
its classes, teachers and venue need), divisions and bands for option groups that run together, staff and
rules. Use plan_summary and plan_list to see it, plan_update to import from a workbook description or fix
issues with a patch keyed by id (edits need no proposal gate, unlike the live timetable), and once no
blocking issues remain, plan_generate to turn it into the draft — then Build or Solve as usual.
When there is no live timetable and no plan yet (or the user wants to start over), guide them through the
start wizard instead of building from scratch. Ask one question at a time, in plain words a non-timetabler
understands: never mention template ids, "slots" or other engine terms, and never offer more than three
options at once. First ask what they are timetabling and call wizard_library to find the domain's
templates; describe at most three candidates by their name, summary and when_to_choose, and call
wizard_preview on each so the side panel shows its facts — up to three previews accumulate there in one
turn. Once they pick one, ask its own questions (from the template) one at a time, then confirm the knobs
in plain words, calling wizard_preview again as the knobs settle. When the user is happy, call
wizard_instantiate; it writes the settings, the plan's vocabulary and returns three links — a workbook to
fill in, a sample PDF of what the printed timetable will look like, and a one-page guide. Give the user all
three links and tell them to drop the filled workbook back into the chat when it is ready. The user may
still start from criteria instead ("start an empty draft"), which skips the wizard."""

_PATCH_DOC = ("JSON merge patch keyed by id, e.g. {\"persons\": {\"kumar\": {\"avail\": [0, 8]}}, "
              "\"locations\": {\"lab\": {\"cap\": 30}}}. avail may also be a list of windows, e.g. "
              "{\"persons\": {\"kumar\": {\"avail\": [[0, 3], [5, 8]]}}} for someone free only outside a midday gap. "
              "A null value removes the item; an unknown id appends it.")

_PLAN_PATCH_DOC = ("JSON merge patch keyed by id, e.g. {\"requirements\": {\"ma-1g3-class-1a2\": {\"periods\": 20}}}, "
                    "{\"staff\": {\"tan\": {\"avail\": [[0, 8]]}}}. A null value removes the item; an unknown id appends it. "
                    "A described requirement like \"Sec 3 Science: 6 periods, two doubles, in a lab, Mr Tan\" becomes one "
                    "requirement patch.")

TOOLS: list[ToolSpec] = [
    ToolSpec("where", "Where a person is at a slot in the live timetable.",
             {"type": "object", "properties": {"person": {"type": "string"}, "slot": {"type": "integer"}}, "required": ["person", "slot"]}),
    ToolSpec("who", "Who is in a location at a slot in the live timetable.",
             {"type": "object", "properties": {"location": {"type": "string"}, "slot": {"type": "integer"}}, "required": ["location", "slot"]}),
    ToolSpec("both_free", "Slots where two people are both free in the live timetable.",
             {"type": "object", "properties": {"a": {"type": "string"}, "b": {"type": "string"}}, "required": ["a", "b"]}),
    ToolSpec("load", "How loaded a person is in the live timetable.",
             {"type": "object", "properties": {"person": {"type": "string"}}, "required": ["person"]}),
    ToolSpec("list_draft", "One section of the draft organisation: persons, locations, events or rules.",
             {"type": "object", "properties": {"section": {"type": "string", "enum": ["persons", "locations", "events", "rules", "summary"]}}, "required": ["section"]}),
    ToolSpec("update_draft", "Change the draft organisation. " + _PATCH_DOC,
             {"type": "object", "properties": {"patch": {"type": "object"}}, "required": ["patch"]}),
    ToolSpec("new_draft", "Start an empty draft (only a rest location) so it can be filled from the user's criteria with update_draft. Replaces any existing draft.",
             {"type": "object", "properties": {"name": {"type": "string", "description": "A name for the organisation or timetable."}}}),
    ToolSpec("build_draft", "Send the draft to the engine. On success the draft becomes the live timetable.",
             {"type": "object", "properties": {}}),
    ToolSpec("find", "Resolve a name the user typed to ids of people, groups or rooms. Ask the user when several come back.",
             {"type": "object", "properties": {"query": {"type": "string"}}, "required": ["query"]}),
    ToolSpec("clashes", "Clashes the Reviewer finds in the live timetable, optionally only those touching one person or one venue.",
             {"type": "object", "properties": {"person": {"type": "string"}, "venue": {"type": "string"}}}),
    ToolSpec("propose", "Ranked options for moving a lesson, without changing anything. Give an event id, or a person "
                        "to propose for every event they clash in. Show the options and wait for the user's yes.",
             {"type": "object", "properties": {"event": {"type": "string"}, "person": {"type": "string"},
                                               "prefer": {"type": "object", "description": "{\"slots\": [int], \"venues\": [id]}"},
                                               "limit": {"type": "integer"}}}),
    ToolSpec("free_venues", "Rooms free at one slot: either an absolute slot, or a date plus the slot's offset within that day.",
             {"type": "object", "properties": {"slot": {"type": "integer"}, "date": {"type": "string", "description": "YYYY-MM-DD"},
                                               "offset": {"type": "integer", "description": "slot within that day, from 0"},
                                               "dur": {"type": "integer"}, "min_cap": {"type": "integer"}, "kind": {"type": "string"}}}),
    ToolSpec("book", "Propose a dated booking of a venue. Nothing is booked until the user says yes and apply is called.",
             {"type": "object", "properties": {"venue": {"type": "string"}, "date": {"type": "string", "description": "YYYY-MM-DD"},
                                               "start": {"type": "integer"}, "dur": {"type": "integer"}, "title": {"type": "string"},
                                               "booked_by": {"type": "string"}, "note": {"type": "string"}},
              "required": ["venue", "date", "start", "title"]}),
    ToolSpec("apply", "Apply one pending proposal by its id. Only after the user has said yes to that option.",
             {"type": "object", "properties": {"id": {"type": "string"}}, "required": ["id"]}),
    ToolSpec("undo", "Offer to undo the last applied change. Like every change, it waits for the user's yes.",
             {"type": "object", "properties": {}}),
    ToolSpec("print_timetable", "Links to a printable timetable (HTML page and PDF) for a teacher, class, group or "
                                "room in the live timetable; resolves the name (or id) first.",
             {"type": "object", "properties": {
                 "kind": {"type": "string", "enum": ["teacher", "class", "group", "room"]},
                 "name": {"type": "string", "description": "A name or id to resolve; asks when several match."},
                 "view": {"type": "string", "enum": ["cycle", "week"]},
                 "date": {"type": "string", "description": "YYYY-MM-DD, for view=week"},
             }, "required": ["kind", "name"]}),
    ToolSpec("plan_summary", "Counts and issue counts for the curriculum plan.",
             {"type": "object", "properties": {}}),
    ToolSpec("plan_list", "One section of the curriculum plan, optionally filtered by a substring on id/name/subject/text.",
             {"type": "object", "properties": {
                 "section": {"type": "string", "enum": ["staff", "classes", "divisions", "requirements", "bands", "rules", "issues"]},
                 "filter": {"type": "string"},
             }, "required": ["section"]}),
    ToolSpec("plan_update", "Change the curriculum plan. " + _PLAN_PATCH_DOC,
             {"type": "object", "properties": {"patch": {"type": "object"}}, "required": ["patch"]}),
    ToolSpec("plan_generate", "Generate a draft organisation from the curriculum plan. Refuses while any blocking issue exists.",
             {"type": "object", "properties": {}}),
    ToolSpec("wizard_library", "The start wizard's template library: domains and, in each, the templates' id, name, "
                               "summary, when_to_choose and knobs with defaults. Optionally filter to one domain.",
             {"type": "object", "properties": {"domain": {"type": "string", "enum": ["education", "health", "business", "sports"]}}}),
    ToolSpec("wizard_preview", "Compute the facts (cycle length, slots, sample counts, trade-offs) for one candidate "
                               "template and its knobs, without writing anything. Fills the side panel with the "
                               "candidate card; call it again with different knobs or another template to compare, "
                               "up to three at once.",
             {"type": "object", "properties": {"template": {"type": "string"}, "knobs": {"type": "object"}},
              "required": ["template"]}),
    ToolSpec("wizard_instantiate", "Write the timetable's settings, the plan's vocabulary and the wizard's record for "
                                   "the chosen template and knobs. Returns links to the workbook, sample PDF and guide.",
             {"type": "object", "properties": {"template": {"type": "string"}, "knobs": {"type": "object"}},
              "required": ["template"]}),
]

# names.find's own "person"/"group" split only tells a group (a synthesised whole-class plane) apart
# from every other person; teacher and room still need a further filter of find's own matches.
_PRINT_FIND_KIND = {"teacher": "person", "group": "group", "room": "location"}


def _print_classes(org: dict) -> list[str]:
    return sorted({c for g in org.get("groups", []) if g.get("band") is None for c in g["classes"]})


def _find_class(org: dict, query: str) -> list[dict]:
    q = query.strip().lower()
    if not q:
        return []
    classes = _print_classes(org)
    exact = [c for c in classes if c.lower() == q]
    if exact:
        return [{"id": c, "name": c, "kind": "class", "role": ""} for c in exact]
    tokens = q.split()
    partial = [c for c in classes if all(t in c.lower() for t in tokens)]
    return [{"id": c, "name": c, "kind": "class", "role": ""} for c in partial[:10]]


def _find_print_target(org: dict, kind: str, query: str) -> list[dict]:
    if kind == "class":
        return _find_class(org, query)
    if kind == "teacher":
        # Filter to teachers BEFORE find's ten-match cap: filtering afterwards loses a teacher whose
        # name a dozen students share, because find would have returned ten students and stopped.
        org = {**org, "persons": [p for p in org.get("persons", []) if str(p.get("role", "")).startswith("Teacher")]}
    return names.find(org, query, kinds=(_PRINT_FIND_KIND[kind],))


@dataclass
class ChatResult:
    text: str
    events: list[dict] = field(default_factory=list)


def _applied_events(kind: str, res: dict) -> list[dict]:
    out: list[dict] = [{"kind": "applied", **res}]
    if res["ok"]:
        if kind == "undo":
            out.append({"kind": "undone", "description": res["description"]})
        out.append({"kind": "bookings_updated"})
    return out


def _plan_issue_dict(i: Issue) -> dict:
    return {"level": i.level, "where": i.where, "text": i.text}


def _plan_issues_for(db: Db, plan: dict) -> list[Issue]:
    return plan_issues(plan, db.get_org("live"), db.get_settings())


def _plan_summary_dict(db: Db, plan: dict, issues: list[Issue] | None = None) -> dict:
    issues = _plan_issues_for(db, plan) if issues is None else issues
    return {"requirements": len(plan["requirements"]), "staff": len(plan["staff"]),
            "divisions": len(plan["divisions"]), "bands": len(plan["bands"]), "classes": len(plan["classes"]),
            "blocks": sum(1 for i in issues if i.level == "block"),
            "warns": sum(1 for i in issues if i.level == "warn"), "source": plan.get("source")}


def _run_tool(call: ToolCall, db: Db, engine: EngineClient, events: list[dict],
              session_id: str = "", run: str = "") -> str:
    try:
        if call.name in ("where", "who", "both_free", "load"):
            live = bookings.live_for_engine(db)
            if live is None:
                return json.dumps({"error": "There is no live timetable yet. Upload documents and build a draft first."})
            return json.dumps(engine.query(live, call.name, call.args))
        if call.name == "find":
            live = db.get_org("live") or db.get_org("draft") or {"persons": [], "locations": []}
            return json.dumps({"matches": names.find(live, str(call.args.get("query", "")))})
        if call.name == "print_timetable":
            live = db.get_org("live")
            if live is None:
                return json.dumps({"error": "There is no live timetable yet. Upload documents and build a draft first."})
            kind = str(call.args.get("kind", ""))
            if kind not in ("teacher", "class", "group", "room"):
                return json.dumps({"error": f"unknown kind {kind}"})
            query = str(call.args.get("name", ""))
            matches = _find_print_target(live, kind, query)
            if not matches:
                return json.dumps({"error": f"no {kind} matching {query!r}"})
            if len(matches) > 1:
                return json.dumps({"matches": matches})
            m = matches[0]
            view = call.args.get("view") or "cycle"
            qs = ""
            if view == "week":
                date = call.args.get("date")
                qs = "?view=week" + (f"&date={quote(str(date), safe='')}" if date else "")
            ident = quote(str(m["id"]), safe="")        # ids are free text: a space or a # would break the link
            return json.dumps({"title": m["name"], "html": f"/print/{kind}/{ident}{qs}", "pdf": f"/print/{kind}/{ident}.pdf{qs}"})
        if call.name == "clashes":
            live = bookings.live_for_engine(db)
            if live is None:
                return json.dumps({"error": "There is no live timetable yet."})
            cl = engine.clashes(live, call.args.get("person"), call.args.get("venue"))["clashes"]
            return json.dumps({"count": len(cl), "clashes": [{"type": c["type"], "message": c["message"], "event": c["event"]} for c in cl]})
        if call.name == "propose":
            if call.args.get("event"):
                items = proposals.propose_for(db, engine, str(call.args["event"]), call.args.get("prefer"),
                                              int(call.args.get("limit") or 3), session_id, run)
            elif call.args.get("person"):
                live = bookings.live_for_engine(db)
                if live is None:
                    return json.dumps({"error": "There is no live timetable yet."})
                cl = engine.clashes(live, person=call.args.get("person"))["clashes"]
                evs = list(dict.fromkeys(c["event"] for c in cl if c["event"]))[:3]
                items = []
                for ev in evs:
                    items += proposals.propose_for(db, engine, ev, None, 2, session_id, run)
                items = proposals.set_pending(db, items, session_id, run)   # each call replaced the list; keep them all
            else:
                return json.dumps({"error": "say which event or person"})
            events.append({"kind": "proposals", "items": items})
            return json.dumps({"proposals": [{"id": i["id"], "text": i["text"], "delta": i.get("delta"),
                                              "clashes": [c["message"] for c in i.get("review") or []]} for i in items],
                               "note": "Show these to the user and wait for a yes before calling apply. "
                                       "Saying yes applies the first option; name another by its id."})
        if call.name == "free_venues":
            live = bookings.live_for_engine(db)
            if live is None:
                return json.dumps({"error": "There is no live timetable yet."})
            slot = call.args.get("slot")
            if slot is None:
                s = db.get_settings()
                date = str(call.args.get("date") or "")
                rng = cal_mod.slot_range(s["calendar"], s["time"], date)
                if rng is None:
                    return json.dumps({"error": cal_mod.describe(s["calendar"], s["time"], date)})
                slot = rng[0] + int(call.args.get("offset") or 0)
            venues = engine.free_venues(live, int(slot), int(call.args.get("dur") or 1),
                                        int(call.args.get("min_cap") or 0), call.args.get("kind"))["venues"]
            return json.dumps({"slot": int(slot), "venues": venues})
        if call.name == "book":
            item = proposals.book(db, call.args, session_id, run)
            events.append({"kind": "proposals", "items": [item]})
            return json.dumps({"proposal": {"id": item["id"], "text": item["text"]},
                               "note": "Wait for a yes before calling apply."})
        if call.name == "apply":
            pid = str(call.args.get("id", ""))
            item = next((p for p in proposals.pending(db, session_id) if p["id"] == pid), None)
            if item is not None and item.get("run") == run:
                return json.dumps({"ok": False, "description": NOT_YET, "clashes": []})
            res = proposals.apply(db, engine, pid, session_id)
            events.extend(_applied_events((item or {}).get("kind", ""), res))
            return json.dumps(res)
        if call.name == "undo":
            item = proposals.propose_undo(db, session_id, run)
            if item is None:
                return json.dumps({"ok": False, "error": "nothing to undo"})
            events.append({"kind": "proposals", "items": [item]})
            return json.dumps({"proposal": {"id": item["id"], "text": item["text"]},
                               "note": "Wait for a yes before calling apply."})
        draft = db.get_org("draft")
        if call.name == "new_draft":
            new = empty_organisation(db.get_settings(), str(call.args.get("name") or "New timetable"))
            db.set_org("draft", new)
            events.append({"kind": "draft_updated", "summary": summarise(new)})
            return json.dumps({"ok": True, "summary": summarise(new), "time_labels": new["time_labels"],
                               "rest_location": next(l["id"] for l in new["locations"] if l.get("rest"))})
        if call.name == "list_draft":
            if draft is None:
                return json.dumps({"error": "There is no draft. Upload documents first."})
            section = call.args.get("section", "summary")
            if section == "summary":
                return json.dumps(summarise(draft))
            if section not in ("persons", "locations", "events", "rules"):
                return json.dumps({"error": f"unknown section {section}"})
            return json.dumps(draft.get(section))
        if call.name == "update_draft":
            if draft is None:
                return json.dumps({"error": "There is no draft to update."})
            new = apply_patch(draft, call.args.get("patch") or {})
            db.set_org("draft", new)
            events.append({"kind": "draft_updated", "summary": summarise(new)})
            return json.dumps({"ok": True, "summary": summarise(new)})
        if call.name == "build_draft":
            if draft is None:
                return json.dumps({"error": "There is no draft to build."})
            result = engine.build(bookings.draft_for_engine(db))
            result["organisation"] = bookings.strip(result["organisation"])
            ok = promote_build(db, result)
            events.append({"kind": "build", "ok": ok, "placed": result["placed"], "unplaced": result["unplaced"],
                           "clashes": result["clashes"]})
            return json.dumps({"ok": ok, "placed": len(result["placed"]), "unplaced": result["unplaced"],
                               "clashes": [c["message"] for c in result["clashes"]], "log": result["log"][-10:]})
        plan = db.get_value("plan")
        if call.name == "plan_summary":
            return json.dumps(_plan_summary_dict(db, plan or empty_plan()))
        if call.name == "plan_list":
            p = plan or empty_plan()
            section = call.args.get("section", "")
            if section == "rules":
                return json.dumps(p["rules"])
            if section == "issues":
                items = [_plan_issue_dict(i) for i in _plan_issues_for(db, p)]
            elif section in ("staff", "classes", "divisions", "requirements", "bands"):
                items = list(p.get(section, []))
            else:
                return json.dumps({"error": f"unknown section {section}"})
            filt = str(call.args.get("filter") or "").strip().lower()
            if filt:
                keys = ("id", "code", "name", "subject", "text", "where")
                items = [it for it in items if any(filt in str(it.get(k, "")).lower() for k in keys)]
            if len(items) > 40:
                return json.dumps({"items": items[:40], "more": len(items) - 40})
            return json.dumps({"items": items})
        if call.name == "plan_update":
            p = plan or empty_plan()
            new = apply_plan_patch(p, call.args.get("patch") or {})
            db.set_value("plan", new)
            issues = _plan_issues_for(db, new)
            events.append({"kind": "plan_updated"})
            return json.dumps({"ok": True, "summary": _plan_summary_dict(db, new, issues),
                               "issues": [_plan_issue_dict(i) for i in issues[:ISSUE_LIMIT]]})
        if call.name == "plan_generate":
            p = plan or empty_plan()
            settings = db.get_settings()
            base_org = db.get_org("live")
            issues = plan_issues(p, base_org, settings)
            if has_blocks(issues):
                blocks = [i.text for i in issues if i.level == "block"]
                return json.dumps({"error": f"{len(blocks)} blocking issues: " + "; ".join(capped(blocks))})
            org, summary = plan_generate_mod.generate(p, base_org, settings)
            db.set_org("draft", org)
            events.append({"kind": "draft_updated"})
            result = {"ok": True, "summary": summary, "issues": [_plan_issue_dict(i) for i in issues[:ISSUE_LIMIT]]}
            if len(issues) > ISSUE_LIMIT:
                result["more"] = len(issues) - ISSUE_LIMIT
            return json.dumps(result)
        if call.name == "wizard_library":
            domains = wiz_library.domains()
            domain = call.args.get("domain")
            if domain:
                domains = [d for d in domains if d["id"] == domain]
            return json.dumps({"domains": domains})
        if call.name in ("wizard_preview", "wizard_instantiate"):
            try:
                template, knobs = wizard_resolve(call.args.get("template"), call.args.get("knobs"))
            except KeyError:
                return json.dumps({"error": f"no such template {call.args.get('template')!r}"})
            except wiz_library.WizardError as e:
                return json.dumps({"error": str(e)})
            if call.name == "wizard_preview":
                facts = wiz_instantiate.facts(template, knobs)
                candidate = {"template": template["id"], "name": template["name"], "summary": template["summary"],
                             "when_to_choose": template["when_to_choose"], "tradeoffs": facts["tradeoffs"],
                             "facts": facts, "knobs": knobs}
                panel = next((e for e in events if e.get("kind") == "wizard" and e.get("stage") == "preview"), None)
                if panel is None:
                    panel = {"kind": "wizard", "stage": "preview", "candidates": []}
                    events.append(panel)
                if len(panel["candidates"]) < 3:      # the side panel shows at most three candidates at once
                    panel["candidates"].append(candidate)
                return json.dumps(facts)
            result = wizard_apply(db, template, knobs)
            events.append({"kind": "wizard", "stage": "done", "downloads": result["downloads"]})
            events.append({"kind": "settings_updated"})
            return json.dumps({"ok": True, **result})
        return json.dumps({"error": f"unknown tool {call.name}"})
    except (EngineError, IntakeError, KeyError, ValueError) as e:
        return json.dumps({"error": str(e)})
    except Exception as e:      # noqa: BLE001 - a tool failure must become a result the model can read, not a lost turn
        return json.dumps({"error": f"{type(e).__name__}: {e}"})


def history_for_provider(db: Db, session_id: str) -> list[dict]:
    out = []
    for m in db.messages(session_id):
        c = m["content"]
        if m["role"] == "assistant":
            out.append({"role": "assistant", "text": c.get("text", ""),
                        "tool_calls": [ToolCall(t["id"], t["name"], t["args"]) for t in c.get("tool_calls", [])]})
        elif m["role"] == "tool":
            out.append({"role": "tool", "call_id": c["call_id"], "name": c["name"], "result": c["result"]})
        else:
            out.append({"role": "user", "text": c.get("text", "")})
    return out


def run_chat(db: Db, session_id: str, provider: Provider, engine: EngineClient, user_text: str) -> ChatResult:
    db.add_message(session_id, "user", {"text": user_text})
    run = secrets.token_hex(4)
    # Expiring before the confirm word is what makes a yes mean this card and not an older one: a
    # card outlives the message that made it by exactly one message, so a question in between drops
    # it and the yes that follows applies nothing.
    proposals.expire(db, session_id, run)
    norm = re.sub(r"[^a-z ]", "", user_text.lower()).strip()
    pend = proposals.pending(db, session_id)
    if pend and norm in CONFIRM_WORDS:
        # A yes is the confirmation the gate waits for: apply the first pending option here, so no
        # model turn stands between the user's word and the change.
        res = proposals.apply(db, engine, pend[0]["id"], session_id)
        text = (("Applied: " if res["ok"] else "Not applied: ") + res["description"]
                + (" — " + "; ".join(c["message"] for c in res["clashes"]) if not res["ok"] and res["clashes"] else ""))
        db.add_message(session_id, "assistant", {"text": text, "tool_calls": []})
        return ChatResult(text, _applied_events(pend[0]["kind"], res))
    if pend and norm in DECLINE_WORDS:
        # The mirror of the yes: the card goes, every run of it, and the model never sees the turn.
        proposals.clear_pending(db, session_id)
        db.add_message(session_id, "assistant", {"text": "Dismissed.", "tool_calls": []})
        return ChatResult("Dismissed.", [{"kind": "dismissed"}])
    events: list[dict] = []
    for _ in range(MAX_ROUNDS):
        try:
            turn = provider.complete(SYSTEM_PROMPT, history_for_provider(db, session_id), TOOLS)
        except ProviderError as e:
            db.add_message(session_id, "assistant", {"text": f"The model provider failed: {e}", "tool_calls": []})
            raise
        db.add_message(session_id, "assistant", {"text": turn.text,
                       "tool_calls": [{"id": c.id, "name": c.name, "args": c.args} for c in turn.tool_calls]})
        if not turn.tool_calls:
            return ChatResult(turn.text, events)
        for call in turn.tool_calls:
            result = _run_tool(call, db, engine, events, session_id, run)
            db.add_message(session_id, "tool", {"call_id": call.id, "name": call.name, "result": result})
    text = "I stopped after too many tool calls in a row. Ask again with a narrower question."
    db.add_message(session_id, "assistant", {"text": text, "tool_calls": []})
    return ChatResult(text, events)
