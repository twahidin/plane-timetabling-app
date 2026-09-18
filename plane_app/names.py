"""Resolve what the user typed to ids. The assistant asks when this returns more than one match."""
from __future__ import annotations


def _entries(org: dict, kinds: tuple) -> list[dict]:
    out = []
    if "person" in kinds or "group" in kinds:
        for p in org.get("persons", []):
            kind = "group" if str(p.get("role", "")).startswith("Group") else "person"
            if kind in kinds:
                out.append({"id": p["id"], "name": p["name"], "kind": kind, "role": p.get("role", "")})
    if "location" in kinds:
        out += [{"id": l["id"], "name": l["name"], "kind": "location", "role": l.get("kind", "") or ""}
                for l in org.get("locations", [])]
    return out


def find(org: dict, query: str, kinds: tuple = ("person", "location", "group")) -> list[dict]:
    """At most ten matches: an exact id first, then a whole-name match, then every token of the
    query contained in the name or the id. Nothing else — a wrong guess is worse than a question."""
    q = query.strip().lower()
    if not q:
        return []
    entries = _entries(org, kinds)
    exact = [e for e in entries if e["id"].lower() == q]
    whole = [e for e in entries if e["name"].lower() == q and e not in exact]
    tokens = q.split()
    partial = [e for e in entries if e not in exact and e not in whole
               and all(t in e["name"].lower() or t in e["id"].lower() for t in tokens)]
    return (exact + whole + partial)[:10]
