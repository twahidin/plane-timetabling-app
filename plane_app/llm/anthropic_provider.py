"""Anthropic SDK implementation of the Provider interface."""
from __future__ import annotations

import json

import anthropic

from . import ProviderError, ToolCall, ToolSpec, Turn


def to_anthropic_messages(messages: list[dict]) -> list[dict]:
    out = []
    for m in messages:
        if m["role"] == "user":
            out.append({"role": "user", "content": [{"type": "text", "text": m["text"]}]})
        elif m["role"] == "assistant":
            content = []
            if m.get("text"):
                content.append({"type": "text", "text": m["text"]})
            for c in m.get("tool_calls", []):
                content.append({"type": "tool_use", "id": c.id, "name": c.name, "input": c.args})
            if not content:
                content.append({"type": "text", "text": "(no reply)"})
            out.append({"role": "assistant", "content": content})
        elif m["role"] == "tool":
            block = {"type": "tool_result", "tool_use_id": m["call_id"], "content": m["result"]}
            if out and out[-1]["role"] == "user" and out[-1]["content"] and out[-1]["content"][0].get("type") == "tool_result":
                out[-1]["content"].append(block)      # several results for one assistant turn go in one user message
            else:
                out.append({"role": "user", "content": [block]})
    return out


class AnthropicProvider:
    def __init__(self, api_key: str, model: str):
        self.model = model
        self.client = anthropic.Anthropic(api_key=api_key)

    def complete(self, system: str, messages: list[dict], tools: list[ToolSpec]) -> Turn:
        try:
            resp = self.client.messages.create(
                model=self.model, max_tokens=8000, system=system,
                messages=to_anthropic_messages(messages),
                tools=[{"name": t.name, "description": t.description, "input_schema": t.schema} for t in tools],
            )
        except anthropic.APIError as e:
            raise ProviderError(f"Anthropic: {e}")
        text, calls = [], []
        for b in resp.content:
            if b.type == "text":
                text.append(b.text)
            elif b.type == "tool_use":
                calls.append(ToolCall(b.id, b.name, dict(b.input)))
        return Turn("\n".join(text).strip(), calls)

    def extract_json(self, system: str, user: str, schema: dict) -> dict:
        try:
            resp = self.client.messages.create(
                model=self.model, max_tokens=16000, system=system,
                messages=[{"role": "user", "content": user}],
                output_config={"format": {"type": "json_schema", "schema": anthropic.transform_schema(schema)}},
            )
        except anthropic.APIError as e:
            raise ProviderError(f"Anthropic: {e}")
        text = next((b.text for b in resp.content if b.type == "text"), "")
        try:
            return json.loads(text)
        except json.JSONDecodeError as e:
            raise ProviderError(f"model returned invalid JSON: {e}")
