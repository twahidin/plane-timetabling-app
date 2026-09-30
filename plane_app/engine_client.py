"""The only way the app reaches the engine."""
from __future__ import annotations

import hashlib
from urllib.parse import quote, urlencode

import httpx


# The shared library is a nicety beside the timetable: its calls give up quickly, so a slow or hung engine never
# holds the wizard, the Project tab or a Generate draft for long (the solver's calls keep the client's 30 s).
LIBRARY_TIMEOUT = 4.0


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

    @property
    def identity(self) -> str:
        """The engine's URL and a digest of the key: what a per-process cache of this engine's answers is keyed by
        (two schools on one engine never share an entry; the key itself is not kept)."""
        return f"{self._client.base_url}#{hashlib.sha256(self._key.encode()).hexdigest()[:16]}"

    def _call(self, method: str, path: str, json: dict | None = None, timeout: float | None = None) -> dict:
        headers = {"Authorization": f"Bearer {self._key}"}
        try:
            if timeout is None:
                r = self._client.request(method, path, json=json, headers=headers)
            else:                    # built then sent: the per-request timeout travels in the request's extensions
                r = self._client.send(self._client.build_request(method, path, json=json, headers=headers, timeout=timeout))
        except httpx.HTTPError as e:
            raise EngineError(0, f"engine unreachable: {e}")
        if r.status_code >= 400:
            try:
                body = r.json()
            except ValueError:
                body = None
            detail = body.get("detail", r.text) if isinstance(body, dict) else r.text
            raise EngineError(r.status_code, str(detail))
        try:
            return r.json()
        except ValueError:
            raise EngineError(502, "the engine sent an answer that is not JSON")

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

    # -- the shared library (learning spec §2) ------------------------------------------------------------------

    def library_publish(self, kind: str, body: dict) -> dict:
        """Send an item for the operator's review: {"id", "status": "pending"}."""
        return self._call("POST", "/v1/library", {"kind": kind, "body": body}, timeout=LIBRARY_TIMEOUT)

    def library_list(self, kind: str, clash: str | None = None) -> dict:
        """{"items": approved items from every school, "own": this key's items with status and note}."""
        query = {"kind": kind} if clash is None else {"kind": kind, "clash": clash}
        return self._call("GET", f"/v1/library?{urlencode(query)}", timeout=LIBRARY_TIMEOUT)

    def library_use(self, item_id: str, kept: bool) -> dict:
        return self._call("POST", f"/v1/library/{quote(str(item_id), safe='')}/use", {"kept": bool(kept)},
                          timeout=LIBRARY_TIMEOUT)

    def library_withdraw(self, item_id: str) -> dict:
        return self._call("DELETE", f"/v1/library/{quote(str(item_id), safe='')}", timeout=LIBRARY_TIMEOUT)
