"""The chat loop: the model asks questions of the timetable and shapes the draft; tools do the work."""
from __future__ import annotations

import json
from dataclasses import dataclass, field

from .db import Db
from .engine_client import EngineClient, EngineError
from .intake import IntakeError, apply_patch, empty_organisation, summarise
from .llm import Provider, ProviderError, ToolCall, ToolSpec
from .promote import promote_build

MAX_ROUNDS = 8

SYSTEM_PROMPT = """You are the timetable assistant for one organisation. The timetable is a solid: every person is a
plane, time runs along it, location runs up it, and a lesson or shift is a prism through the planes of its members.
There is a LIVE timetable (built and checked) and possibly a DRAFT organisation extracted from uploaded documents.
Use the tools: where, who, both_free and load answer questions about the live timetable; list_draft shows the draft;
update_draft changes it with a patch keyed by id; build_draft asks the engine to build it. When the user describes an
organisation from scratch (criteria, numbers of people and rooms, subjects, constraints) and there is no draft, call
new_draft first, then fill it with update_draft: rooms with capacities, people with avail [first slot, one past the
last] and eligible rooms (always include "rest"), and events with members and duration; then ask the user to check
the tables before building. Never claim a build
succeeded unless build_draft returned ok: true. Quote the tool's answer and keep replies short. Slots are numbered
from 0 and ids are short lowercase strings; if the user uses a name, look it up in the draft or ask."""

_PATCH_DOC = ("JSON merge patch keyed by id, e.g. {\"persons\": {\"kumar\": {\"avail\": [0, 8]}}, "
              "\"locations\": {\"lab\": {\"cap\": 30}}}. A null value removes the item; an unknown id appends it.")

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
]


@dataclass
class ChatResult:
    text: str
    events: list[dict] = field(default_factory=list)


def _run_tool(call: ToolCall, db: Db, engine: EngineClient, events: list[dict]) -> str:
    try:
        if call.name in ("where", "who", "both_free", "load"):
            live = db.get_org("live")
            if live is None:
                return json.dumps({"error": "There is no live timetable yet. Upload documents and build a draft first."})
            return json.dumps(engine.query(live, call.name, call.args))
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
            result = engine.build(draft)
            ok = promote_build(db, result)
            events.append({"kind": "build", "ok": ok, "placed": result["placed"], "unplaced": result["unplaced"],
                           "clashes": result["clashes"]})
            return json.dumps({"ok": ok, "placed": len(result["placed"]), "unplaced": result["unplaced"],
                               "clashes": [c["message"] for c in result["clashes"]], "log": result["log"][-10:]})
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
            result = _run_tool(call, db, engine, events)
            db.add_message(session_id, "tool", {"call_id": call.id, "name": call.name, "result": result})
    text = "I stopped after too many tool calls in a row. Ask again with a narrower question."
    db.add_message(session_id, "assistant", {"text": text, "tool_calls": []})
    return ChatResult(text, events)
