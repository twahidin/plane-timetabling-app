"""The only way the app reaches the engine."""
from __future__ import annotations

import httpx


class EngineError(Exception):
    def __init__(self, status: int, detail: str):
        super().__init__(f"engine {status}: {detail}")
        self.status, self.detail = status, detail


class EngineClient:
    def __init__(self, url: str, key: str, transport: httpx.BaseTransport | None = None, client: httpx.Client | None = None):
        self._key = key
        if client is not None:
            self._client = client
        else:
            self._client = httpx.Client(base_url=url.rstrip("/"), transport=transport, timeout=30.0)

    def _call(self, method: str, path: str, json: dict | None = None) -> dict:
        try:
            r = self._client.request(method, path, json=json, headers={"Authorization": f"Bearer {self._key}"})
        except httpx.HTTPError as e:
            raise EngineError(0, f"engine unreachable: {e}")
        if r.status_code >= 400:
            try:
                body = r.json()
            except ValueError:
                body = None
            detail = body.get("detail", r.text) if isinstance(body, dict) else r.text
            raise EngineError(r.status_code, str(detail))
        return r.json()

    def build(self, org: dict, max_repair: int = 50) -> dict:
        return self._call("POST", "/v1/build", {"organisation": org, "max_repair": max_repair})

    def check(self, org: dict) -> dict:
        return self._call("POST", "/v1/check", {"organisation": org})

    def query(self, org: dict, kind: str, args: dict) -> dict:
        return self._call("POST", "/v1/query", {"organisation": org, "kind": kind, "args": args})

    def load(self, org: dict, person: str) -> dict:
        return self._call("POST", "/v1/load", {"organisation": org, "person": person})

    def loads(self, org: dict) -> dict:
        """Every person's load report in one metered call: {"loads": {pid: report}}."""
        return self._call("POST", "/v1/loads", {"organisation": org})

    def usage(self) -> dict:
        return self._call("GET", "/v1/usage")
