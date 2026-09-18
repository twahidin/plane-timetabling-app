"""The change log: a snapshot before every applied change, so the last twenty can be undone.

Each snapshot (a copy of the live organisation, bookings and last check) can be large — the
live organisation alone can be several hundred KB for a big school — so it is never inlined
into the log itself. The log ("changes") is a small per-timetable index of
{when, kind, description, snap}; each entry's snapshot lives in its own scoped kv row under
f"change_snap:{snap}", written once and never rewritten. `record` therefore writes one small
index row plus one new snapshot row, instead of rewriting up to twenty snapshots on every call.
"""
from __future__ import annotations

import time

from .db import CHANGE_SNAP_PREFIX

KEEP = 20


def _entries(db) -> list[dict]:
    return list(db.get_value("changes") or [])


def _snap_key(n: int) -> str:
    return f"{CHANGE_SNAP_PREFIX}{n}"


def _next_seq(db) -> int:
    n = (db.get_value("change_seq") or 0) + 1
    db.set_value("change_seq", n)
    return n


def record(db, kind: str, description: str) -> None:
    n = _next_seq(db)
    snapshot = {"live": db.get_org("live"), "bookings": list(db.get_value("bookings") or []),
                "last_check": db.get_value("last_check")}
    db.set_value(_snap_key(n), snapshot)
    entries = [{"when": time.time(), "kind": kind, "description": description, "snap": n}] + _entries(db)
    kept, dropped = entries[:KEEP], entries[KEEP:]
    for e in dropped:
        db.set_value(_snap_key(e["snap"]), None)     # drop the snapshot row for anything falling out of the kept 20
    db.set_value("changes", kept)


def list_all(db) -> list[dict]:
    return [{k: v for k, v in e.items() if k not in ("before", "snap")} for e in _entries(db)]


def undo(db) -> str | None:
    entries = _entries(db)
    if not entries:
        return None
    top, rest = entries[0], entries[1:]
    snapshot = db.get_value(_snap_key(top["snap"]))
    db.set_org("live", snapshot["live"])
    db.set_value("bookings", snapshot["bookings"])
    db.set_value("last_check", snapshot["last_check"])
    db.set_value(_snap_key(top["snap"]), None)
    db.set_value("changes", rest)
    return top["description"]
