"""OpenAI-compatible chat completions (OpenAI, OpenRouter, TokenRouter) implementation."""
from __future__ import annotations

import json
import re

import openai

from . import ProviderError, ToolCall, ToolSpec, Turn


def to_openai_messages(system: str, messages: list[dict]) -> list[dict]:
    out = [{"role": "system", "content": system}]
    for m in messages:
        if m["role"] == "user":
            out.append({"role": "user", "content": m["text"]})
        elif m["role"] == "assistant":
            entry = {"role": "assistant", "content": m.get("text") or None}
            if m.get("tool_calls"):
                entry["tool_calls"] = [{"id": c.id, "type": "function", "function": {"name": c.name, "arguments": json.dumps(c.args)}}
                                       for c in m["tool_calls"]]
            elif not entry["content"]:
                entry["content"] = "(no reply)"
            out.append(entry)
        elif m["role"] == "tool":
            out.append({"role": "tool", "tool_call_id": m["call_id"], "content": m["result"]})
    return out


_FENCE = re.compile(r"```(?:json)?\s*(.*?)```", re.S)


def _parse_json(text: str) -> dict:
    m = _FENCE.search(text or "")
    candidate = m.group(1) if m else (text or "")
    return json.loads(candidate.strip())


class OpenAICompatProvider:
    def __init__(self, api_key: str, model: str, base_url: str):
        self.model, self.base_url = model, base_url
        self.client = openai.OpenAI(api_key=api_key, base_url=base_url)

    def complete(self, system: str, messages: list[dict], tools: list[ToolSpec]) -> Turn:
        kwargs = {"model": self.model, "messages": to_openai_messages(system, messages)}
        if tools:
            kwargs["tools"] = [{"type": "function", "function": {"name": t.name, "description": t.description, "parameters": t.schema}} for t in tools]
        try:
            resp = self.client.chat.completions.create(**kwargs)
        except openai.OpenAIError as e:
            raise ProviderError(f"{self.base_url}: {e}")
        msg = resp.choices[0].message
        calls = []
        for c in msg.tool_calls or []:
            try:
                args = json.loads(c.function.arguments or "{}")
            except json.JSONDecodeError:
                args = {}
            calls.append(ToolCall(c.id, c.function.name, args))
        return Turn((msg.content or "").strip(), calls)

    def extract_json(self, system: str, user: str, schema: dict) -> dict:
        prompt = f"{user}\n\nReply with a single JSON object matching this JSON schema and nothing else:\n{json.dumps(schema)}"
        messages = [{"role": "system", "content": system}, {"role": "user", "content": prompt}]
        last_error = None
        for attempt in range(2):
            try:
                resp = self.client.chat.completions.create(model=self.model, messages=messages)
            except openai.OpenAIError as e:
                raise ProviderError(f"{self.base_url}: {e}")
            text = resp.choices[0].message.content or ""
            try:
                return _parse_json(text)
            except json.JSONDecodeError as e:
                last_error = e
                messages.append({"role": "assistant", "content": text})
                messages.append({"role": "user", "content": "That was not valid JSON. Reply with only the JSON object."})
        raise ProviderError(f"model returned invalid JSON twice: {last_error}")
