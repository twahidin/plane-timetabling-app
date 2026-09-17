"""One provider interface for every model the app can use."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol


class ProviderError(Exception):
    pass


@dataclass(frozen=True)
class ToolCall:
    id: str
    name: str
    args: dict


@dataclass(frozen=True)
class ToolSpec:
    name: str
    description: str
    schema: dict


@dataclass
class Turn:
    text: str
    tool_calls: list[ToolCall] = field(default_factory=list)


class Provider(Protocol):
    def complete(self, system: str, messages: list[dict], tools: list[ToolSpec]) -> Turn: ...
    def extract_json(self, system: str, user: str, schema: dict) -> dict: ...


DEFAULT_BASE_URLS = {
    "openai": "https://api.openai.com/v1",
    "openrouter": "https://openrouter.ai/api/v1",
    "tokenrouter": "https://api.tokenrouter.io/v1",
}


def make_provider(settings: dict) -> Provider:
    p = settings.get("provider", {})
    kind, key, model = p.get("kind", "anthropic"), p.get("api_key", ""), p.get("model", "")
    if not key:
        raise ProviderError("No API key set for the model provider. Add one in Settings.")
    if not model:
        raise ProviderError("No model id set. Add one in Settings.")
    if kind == "anthropic":
        from .anthropic_provider import AnthropicProvider
        return AnthropicProvider(key, model)
    if kind in DEFAULT_BASE_URLS:
        from .openai_provider import OpenAICompatProvider
        return OpenAICompatProvider(key, model, p.get("base_url") or DEFAULT_BASE_URLS[kind])
    raise ProviderError(f"Unknown provider kind {kind}")


class FakeProvider:
    """Scripted provider for tests."""
    def __init__(self, turns: list[Turn] | None = None, extraction: dict | None = None):
        self.turns = list(turns or [])
        self.extraction = extraction or {}
        self.calls: list[tuple] = []

    def complete(self, system, messages, tools) -> Turn:
        self.calls.append(("complete", system, messages, tools))
        return self.turns.pop(0) if self.turns else Turn("(no more scripted turns)")

    def extract_json(self, system, user, schema) -> dict:
        self.calls.append(("extract", system, user, schema))
        return self.extraction
