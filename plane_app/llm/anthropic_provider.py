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


# The models Settings lists. Each can decline a request under its safeguards; for these the API's
# server-side fallback re-runs a declined request on a suitable model inside the same call.
FALLBACK_MODELS = ("claude-opus-5-5", "claude-sonnet-5-5", "claude-fable-5-1")


class AnthropicProvider:
    def __init__(self, api_key: str, model: str):
        self.model = model
        self.client = anthropic.Anthropic(api_key=api_key)

    def _create(self, **kw):
        if self.model in FALLBACK_MODELS:
            return self.client.beta.messages.create(betas=["server-side-fallback-2026-07-01"], fallbacks="default", **kw)
        return self.client.messages.create(**kw)      # a model typed in Settings: no fallback, which it may not accept

    @staticmethod
    def _check(resp) -> None:
        """A declined request comes back as HTTP 200 with stop_reason "refusal" and no answer: say so."""
        if getattr(resp, "stop_reason", None) == "refusal":
            category = getattr(getattr(resp, "stop_details", None), "category", None)
            raise ProviderError("The model declined this request" + (f" ({category})." if category else "."))

    def complete(self, system: str, messages: list[dict], tools: list[ToolSpec]) -> Turn:
        try:
            resp = self._create(
                model=self.model, max_tokens=8000, system=system,
                output_config={"effort": "high"},   # Opus 5.5 defaults to medium; keep the depth Opus 5 ran at
                messages=to_anthropic_messages(messages),
                tools=[{"name": t.name, "description": t.description, "input_schema": t.schema} for t in tools],
            )
        except anthropic.APIError as e:
            raise ProviderError(f"Anthropic: {e}")
        self._check(resp)
        text, calls = [], []
        for b in resp.content:
            if b.type == "text":
                text.append(b.text)
            elif b.type == "tool_use":
                calls.append(ToolCall(b.id, b.name, dict(b.input)))
        return Turn("\n".join(text).strip(), calls)

    def extract_json(self, system: str, user: str, schema: dict) -> dict:
        try:
            resp = self._create(
                model=self.model, max_tokens=16000, system=system,
                messages=[{"role": "user", "content": user}],
                output_config={"effort": "high", "format": {"type": "json_schema", "schema": anthropic.transform_schema(schema)}},
            )
        except anthropic.APIError as e:
            raise ProviderError(f"Anthropic: {e}")
        self._check(resp)
        text = next((b.text for b in resp.content if b.type == "text"), "")
        try:
            return json.loads(text)
        except json.JSONDecodeError as e:
            raise ProviderError(f"model returned invalid JSON: {e}")
