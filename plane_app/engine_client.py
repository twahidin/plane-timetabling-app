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

    def propose(self, org: dict, event: str, prefer: dict | None = None, limit: int = 3) -> dict:
        """Ranked alternatives for one event: {"proposals": [{id, kind, event, to, with, delta, total, review}]}."""
        return self._call("POST", "/v1/propose", {"organisation": org, "event": event, "prefer": prefer, "limit": limit})

    def free_venues(self, org: dict, slot: int, dur: int = 1, min_cap: int = 0, kind: str | None = None) -> dict:
        return self._call("POST", "/v1/venues/free", {"organisation": org, "slot": slot, "dur": dur, "min_cap": min_cap, "kind": kind})

    def clashes(self, org: dict, person: str | None = None, venue: str | None = None) -> dict:
        return self._call("POST", "/v1/clashes", {"organisation": org, "person": person, "venue": venue})

    def usage(self) -> dict:
        return self._call("GET", "/v1/usage")

    # -- solve jobs (asynchronous: submit, poll, cancel) ------------------------

    def solve(self, org: dict, previous: dict | None = None, time_limit: int = 300, weights: dict | None = None,
              hint: str = "greedy") -> dict:
        """Queue a solve; returns {"job": id}. `hint` is "greedy" or "previous" (the placements in `previous`)."""
        return self._call("POST", "/v1/solve", {"organisation": org, "previous": previous, "time_limit": time_limit,
                                                "weights": weights, "hint": hint})

    def job(self, jid: str) -> dict:
        """{status, elapsed, best_objective, bound, result?, error?}; status is queued | running | done | failed | cancelled."""
        return self._call("GET", f"/v1/jobs/{jid}")

    def cancel_job(self, jid: str) -> dict:
        return self._call("DELETE", f"/v1/jobs/{jid}")

    def score(self, org: dict, previous: dict | None = None, weights: dict | None = None) -> dict:
        """The soft-rule score of a placed organisation: {total, breakdown, ideal, worst, scores}."""
        return self._call("POST", "/v1/score", {"organisation": org, "previous": previous, "weights": weights})
