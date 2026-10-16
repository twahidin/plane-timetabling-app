"""The only way the app reaches the engine."""
from __future__ import annotations

import hashlib
import time
from urllib.parse import quote, urlencode

import httpx


# The shared library is a nicety beside the timetable: its calls give up quickly, so a slow or hung engine never
# holds the wizard, the Project tab or a Generate draft for long (the solver's calls keep the client's 30 s).
LIBRARY_TIMEOUT = 4.0

WEEK_REFUSED = "The engine needs an update for this week's shape (closed periods and per-day breaks). Ask whoever runs the engine to update it."
# The request keys under which an organisation travels to the engine; each gets the week merged in.
ORG_KEYS = ("organisation", "previous")
_features: dict[str, tuple[float, frozenset]] = {}         # engine identity -> (when asked, features)


class EngineError(Exception):
    def __init__(self, status: int, detail: str):
        super().__init__(f"engine {status}: {detail}")
        self.status, self.detail = status, detail


def with_week(org: dict, extra: dict) -> dict:
    """The organisation with the week's rules merged in, when it is on the week's grid: as many positions as
    the week (`n_slots`) and, when the week's `labels` are carried, the same labels. Otherwise as it was
    (another timetable's organisation, a two-week file kept on a one-week timetable, or one not yet
    remapped is never sent indices that do not fit it). `n_slots` and `labels` are never sent."""
    if not extra or not isinstance(org, dict):
        return org
    labels = list(org.get("time_labels") or [])
    n_slots = extra.get("n_slots")
    if not isinstance(n_slots, int) or len(labels) != n_slots:
        return org
    if extra.get("labels") is not None and [str(x) for x in labels] != [str(x) for x in extra["labels"]]:
        return org
    send = {k: v for k, v in extra.items() if k not in ("n_slots", "labels")}
    return {**org, "rules": {**(org.get("rules") or {}), **send}}


PATTERN_ROLE = "Pattern"
PATTERN_NAME = "Students (choice pattern)"      # anonymous: the engine's clash messages name it as they name a person


def _ties(org: dict) -> dict[str, list[tuple]]:
    """Member id -> what ties its lessons to the clock, as (event id, sync id, locked start, locked end): a
    band's options start together (`sync`), and a locked, placed lesson stays where it is."""
    out: dict[str, list[tuple]] = {}
    week = max(1, len(org.get("time_labels") or []))
    for e in org.get("events") or []:
        members = e.get("members") if isinstance(e, dict) else None
        if not isinstance(members, list):
            continue
        sync = e.get("sync") if isinstance(e.get("sync"), str) else None
        t0 = e.get("t0") if e.get("fixed") and isinstance(e.get("t0"), int) else None
        dur = min(e["dur"], week) if isinstance(e.get("dur"), int) and e["dur"] > 0 else 1
        if sync is None and t0 is None:
            continue
        tie = (e.get("id"), sync, t0, None if t0 is None else t0 + dur)
        for m in members:
            if isinstance(m, str):
                out.setdefault(m, []).append(tie)
    return out


def _apart(gids: list[str], ties: dict[str, list[tuple]]) -> list[str]:
    """The groups a pattern can keep apart, in order: a group whose lessons are synced with a kept group's (two
    options of one band, from a list read against older bands) or whose locked lessons already overlap a kept
    group's locked lessons cannot be, so it is left out and the first stays. One lesson holding two of the
    groups is no obstacle (the pattern sits in it once). The app-side student check still reports the student."""
    kept: list[str] = []
    by_sync: dict[str, set] = {}                # held sync id -> the events holding it
    by_slot: dict[int, set] = {}                # held locked position -> the events covering it

    def clashes(eid, sync, t0, t1) -> bool:
        if sync is not None and by_sync.get(sync, set()) - {eid}:
            return True
        return t0 is not None and any(by_slot.get(t, set()) - {eid} for t in range(t0, t1))

    for g in gids:
        mine = ties.get(g, [])
        if any(clashes(*tie) for tie in mine):
            continue
        kept.append(g)
        for eid, sync, t0, t1 in mine:
            if sync is not None:
                by_sync.setdefault(sync, set()).add(eid)
            for t in range(t0, t1) if t0 is not None else ():
                by_slot.setdefault(t, set()).add(eid)
    return kept


def with_patterns(org: dict, patterns: dict) -> dict:
    """The organisation with one anonymous `Pattern` member per choice pattern (students spec §3): a member
    of every event of its groups, so the engine never places two of them at once. A pattern keeps apart the
    groups that survive in `org` (as a group or a person id): a group gone from this organisation is left out,
    and a pattern left with fewer than two is skipped. Two of a pattern's groups whose lessons are synced (one
    band's options, which start together) or whose locked lessons already overlap cannot be kept apart: only the
    first stays in the pattern. A pattern id the organisation already has is left alone.
    Nothing to add: the very same organisation comes back. The pattern is available everywhere and in every
    room, so it never narrows a lesson beyond its overlaps."""
    if not patterns or not isinstance(org, dict):
        return org
    present = {x.get("id") for key in ("groups", "persons") for x in org.get(key) or [] if isinstance(x, dict)}
    ties = _ties(org)
    by_group: dict[str, list[str]] = {}             # surviving group id -> the patterns it is in, in pattern order
    added: list[str] = []
    for pid, gids in patterns.items():
        surviving = _apart(sorted({g for g in gids if isinstance(g, str) and g in present}), ties)
        if pid in present or len(surviving) < 2:
            continue
        added.append(pid)
        for g in surviving:
            by_group.setdefault(g, []).append(pid)
    if not added:
        return org
    n_slots = len(org.get("time_labels") or [])
    rooms = [l["id"] for l in org.get("locations") or [] if isinstance(l, dict) and "id" in l]
    persons = list(org.get("persons") or []) + [
        {"id": pid, "name": PATTERN_NAME, "role": PATTERN_ROLE, "avail": [[0, n_slots]], "eligible": list(rooms)}
        for pid in added]
    events = []
    for e in org.get("events") or []:
        members = e.get("members") if isinstance(e, dict) else None
        extra: dict[str, None] = {}                     # ordered and without repeats
        for m in members if isinstance(members, list) else []:
            for pid in by_group.get(m, ()) if isinstance(m, str) else ():
                extra[pid] = None
        events.append({**e, "members": members + list(extra)} if extra else e)
    return {**org, "persons": persons, "events": events}


def _pattern_ids(obj, found: set) -> None:
    if isinstance(obj, dict):
        persons = obj.get("persons")
        for p in persons if isinstance(persons, list) else []:
            if isinstance(p, dict) and p.get("role") == PATTERN_ROLE and isinstance(p.get("id"), str):
                found.add(p["id"])
        for v in obj.values():
            _pattern_ids(v, found)
    elif isinstance(obj, list):
        for v in obj:
            _pattern_ids(v, found)


def _pattern_tile(t, ids: set) -> bool:
    return isinstance(t, str) and t.rpartition("@")[2] in ids


def _pattern_only(v, ids: set) -> bool:
    """A clash whose every tile is a pattern's: the app-side student check reports it, so the answer drops it."""
    tiles = v.get("tiles") if isinstance(v, dict) else None
    return isinstance(tiles, list) and bool(tiles) and all(_pattern_tile(t, ids) for t in tiles)


def _strip(obj, ids: set):
    if isinstance(obj, list):
        return [_strip(v, ids) for v in obj if not _pattern_only(v, ids)]
    if not isinstance(obj, dict):
        return obj
    out = {k: _strip(v, ids) for k, v in obj.items() if k not in ids}      # e.g. a load report keyed by a pattern
    if isinstance(out.get("persons"), list) and isinstance(out.get("events"), list):
        out["persons"] = [p for p in out["persons"] if not (isinstance(p, dict) and (p.get("role") == PATTERN_ROLE or p.get("id") in ids))]
        out["events"] = [{**e, "members": [m for m in e["members"] if not (isinstance(m, str) and m in ids)]}
                         if isinstance(e, dict) and isinstance(e.get("members"), list) else e for e in out["events"]]
    if isinstance(out.get("tiles"), list):                    # a clash keeps its message, never a pattern's tile
        out["tiles"] = [t for t in out["tiles"] if not _pattern_tile(t, ids)]
    return out


def strip_patterns(obj, ids=()):
    """An engine answer with every choice pattern taken out, wherever an organisation or a clash sits in it:
    `Pattern` persons and their ids among event members, clash tiles naming a pattern (the message stays), a
    clash on patterns alone (the app-side student check reports it) and any entry keyed by a pattern id.
    `ids` adds pattern ids the answer itself may not list (a check answer carries clashes but no
    organisation). An answer with no pattern comes back as it was."""
    found = {i for i in ids if isinstance(i, str)}
    _pattern_ids(obj, found)
    return _strip(obj, found) if found else obj


class EngineClient:
    def __init__(self, url: str, key: str, transport: httpx.BaseTransport | None = None, client: httpx.Client | None = None):
        self._key = key
        if client is not None:
            self._client = client
        else:
            self._client = httpx.Client(base_url=url.rstrip("/"), transport=transport, timeout=30.0)
        self.week_rules: dict = {}          # set by main.make_engine: the timetable's week, merged into every organisation sent
        self.patterns: dict = {}            # set by main.make_engine: pattern id -> option group ids, added to every organisation sent

    @property
    def identity(self) -> str:
        """The engine's URL and a digest of the key: what a per-process cache of this engine's answers is keyed by
        (two schools on one engine never share an entry; the key itself is not kept)."""
        return f"{self._client.base_url}#{hashlib.sha256(self._key.encode()).hexdigest()[:16]}"

    def _needs_week(self) -> None:
        """Ask the engine once in five minutes whether it reads closed positions; refuse if not."""
        if "week" not in self._engine_features():
            raise EngineError(409, WEEK_REFUSED)

    def _engine_features(self) -> frozenset:
        """The features the engine's /healthz lists, asked once in five minutes per engine; only an answer is
        remembered (a failing /healthz is the usual unreachable error)."""
        key = self.identity
        hit = _features.get(key)
        if hit is None or time.monotonic() - hit[0] > 300:
            try:
                r = self._client.request("GET", "/healthz")
                body = r.json() if r.status_code < 400 else None
            except (httpx.HTTPError, ValueError) as e:
                raise EngineError(0, f"engine unreachable: {e}")
            if r.status_code >= 400:         # a proxy's 502 while the engine redeploys is not an answer: never remembered
                raise EngineError(0, f"engine unreachable: healthz answered {r.status_code}")
            if not isinstance(body, dict):
                raise EngineError(0, "engine unreachable: healthz did not answer with a JSON object")
            listed = body.get("features")
            feats = frozenset(f for f in listed if isinstance(f, str)) if isinstance(listed, list) else frozenset()
            hit = _features[key] = (time.monotonic(), feats)
        return hit[1]

    def _call(self, method: str, path: str, json: dict | None = None, timeout: float | None = None) -> dict:
        if self.week_rules and isinstance(json, dict):
            touched = {k: with_week(v, self.week_rules) for k, v in json.items() if k in ORG_KEYS and isinstance(v, dict)}
            if any(touched[k] is not json[k] for k in touched):
                self._needs_week()
                json = {**json, **touched}
        if self.patterns and isinstance(json, dict):
            # after the week; only an engine that lists `patterns` gets them, any other the organisation as it
            # was (the app-side student clash check still reports). Nothing to add: no probe, the same bytes.
            touched = {k: with_patterns(v, self.patterns) for k, v in json.items() if k in ORG_KEYS and isinstance(v, dict)}
            if any(touched[k] is not json[k] for k in touched) and "patterns" in self._engine_features():
                json = {**json, **touched}
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
            answer = r.json()
        except ValueError:
            raise EngineError(502, "the engine sent an answer that is not JSON")
        return strip_patterns(answer, self.patterns)     # no stored or shown organisation ever holds a pattern

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
