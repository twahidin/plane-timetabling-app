"""The change log: a snapshot before every applied change, so the last twenty can be undone.

Each snapshot (a copy of the live organisation and last check) can be large — the live
organisation alone can be several hundred KB for a big school — so it is never inlined into the
log itself. The log ("changes") is a small per-timetable index of {when, kind, description, snap};
each entry's snapshot lives in its own scoped kv row under f"change_snap:{snap}", written once and
never rewritten. `record` therefore writes one small index row plus one new snapshot row, instead
of rewriting up to twenty snapshots on every call.

Bookings are not snapshotted. They live on the base timetable, shared by the base and its period
timetables, while each of those keeps its own log; restoring a whole bookings list from one log
would wipe a booking made since through another. So a booking change records the one booking it
adds or removes, and undoing it reverses exactly that booking by id; undoing any other change never
touches bookings.
"""
from __future__ import annotations

import time

from .db import CHANGE_SNAP_PREFIX

KEEP = 20
BOOKING_ADDED = ("booking",)                 # `proposals.apply` records a booking card under its kind
BOOKING_REMOVED = ("booking_removed",)       # DELETE /api/bookings/{bid}


def _entries(db, tid=None) -> list[dict]:
    return list(db.get_value("changes", tid=tid) or [])


def _snap_key(n: int) -> str:
    return f"{CHANGE_SNAP_PREFIX}{n}"


def _next_seq(db) -> int:
    n = (db.get_value("change_seq") or 0) + 1
    db.set_value("change_seq", n)
    return n


def _base(db) -> str:
    from . import periods                               # bookings live on the base; periods imports bookings
    return periods.base_tid(db)


def record(db, kind: str, description: str, booking: dict | None = None) -> None:
    """Snapshot the selected timetable's live organisation and last check. For a booking kind,
    `booking` is the booking the change adds (BOOKING_ADDED) or removes (BOOKING_REMOVED)."""
    if (kind in BOOKING_ADDED or kind in BOOKING_REMOVED) and booking is None:
        raise ValueError(f"a {kind!r} change needs the booking it adds or removes")
    n = _next_seq(db)
    snapshot = {"live": db.get_org("live"), "last_check": db.get_value("last_check")}
    if booking is not None:
        snapshot["booking"] = dict(booking)
    db.set_value(_snap_key(n), snapshot)
    entries = [{"when": time.time(), "kind": kind, "description": description, "snap": n}] + _entries(db)
    kept, dropped = entries[:KEEP], entries[KEEP:]
    for e in dropped:
        db.set_value(_snap_key(e["snap"]), None)     # drop the snapshot row for anything falling out of the kept 20
    db.set_value("changes", kept)


def list_all(db, tid=None) -> list[dict]:
    """The log of the selected timetable (or of `tid`), newest first, without the snapshot keys."""
    return [{k: v for k, v in e.items() if k not in ("before", "snap")} for e in _entries(db, tid)]


def _undo_booking(db, kind: str, snapshot: dict) -> None:
    from . import bookings                              # bookings imports periods, which imports bookings
    b = snapshot.get("booking")
    if b is not None:
        if kind in BOOKING_ADDED:
            bookings.remove(db, b["id"])
        elif kind in BOOKING_REMOVED:
            bookings.restore(db, b)
    elif "bookings" in snapshot and (kind in BOOKING_ADDED or kind in BOOKING_REMOVED):
        db.set_value("bookings", snapshot["bookings"], tid=_base(db))   # an entry logged before per-booking undo


def undo(db) -> str | None:
    entries = _entries(db)
    if not entries:
        return None
    top, rest = entries[0], entries[1:]
    snapshot = db.get_value(_snap_key(top["snap"]))
    db.set_org("live", snapshot["live"])
    _undo_booking(db, top["kind"], snapshot)
    db.set_value("last_check", snapshot["last_check"])
    db.set_value(_snap_key(top["snap"]), None)
    db.set_value("changes", rest)
    return top["description"]
