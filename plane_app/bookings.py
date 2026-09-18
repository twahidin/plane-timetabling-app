"""Dated venue bookings. Stored per timetable; injected as fixed, memberless events on their cycle slot."""
from __future__ import annotations

import copy
import hashlib
import json

from . import calendar as cal


class BookingError(ValueError):
    pass


def _id(b: dict) -> str:
    return "bk-" + hashlib.sha256(json.dumps([b["venue"], b["date"], b["start"], b["dur"], b["title"]]).encode()).hexdigest()[:10]


def validate(db, b: dict) -> dict:
    if not isinstance(b, dict) or not str(b.get("title") or "").strip():
        raise BookingError("a booking needs a title")
    live = db.get_org("live") or {"locations": []}
    venue = str(b.get("venue") or "")
    if venue not in {l["id"] for l in live["locations"]}:
        raise BookingError(f"unknown venue {venue!r}")
    s = db.get_settings()
    date = str(b.get("date") or "")
    if date in (s["calendar"].get("non_teaching_dates") or []):
        raise BookingError(f"{cal.describe(s['calendar'], s['time'], date)}")
    rng = cal.slot_range(s["calendar"], s["time"], date)
    if rng is None:
        raise BookingError(f"{cal.describe(s['calendar'], s['time'], date)}: set the term calendar in Settings if the term has started")
    try:
        start, dur = int(b.get("start")), int(b.get("dur") or 1)
    except (TypeError, ValueError):
        raise BookingError("start and dur must be integers")
    if dur < 1 or start < rng[0] or start + dur > rng[1]:
        raise BookingError(f"slots {start}–{start + dur - 1} are outside that day's range {rng[0]}–{rng[1] - 1}")
    out = {"venue": venue, "date": date, "start": start, "dur": dur, "title": str(b["title"]).strip(),
           "booked_by": str(b.get("booked_by") or ""), "note": str(b.get("note") or "")}
    out["id"] = _id(out)
    return out


def list_all(db) -> list[dict]:
    return list(db.get_value("bookings") or [])


def add(db, b: dict) -> dict:
    b = validate(db, b)
    items = [x for x in list_all(db) if x["id"] != b["id"]] + [b]
    db.set_value("bookings", sorted(items, key=lambda x: (x["date"], x["start"], x["venue"])))
    return b


def remove(db, bid: str, before_persist=None) -> dict | None:
    """Remove the booking with this id in a single pass over the list, returning it (or None if
    absent) so callers need no separate lookup for a 404 or a description. When a booking is
    found, `before_persist(removed)` — if given — runs before the removal is written to the db,
    so a caller can snapshot the pre-removal state (e.g. the change log) first."""
    items = list_all(db)
    removed = next((x for x in items if x["id"] == bid), None)
    if removed is not None:
        if before_persist is not None:
            before_persist(removed)
        db.set_value("bookings", [x for x in items if x["id"] != bid])
    return removed


def inject(org: dict, items: list[dict]) -> dict:
    out = copy.deepcopy(org)
    known = {l["id"] for l in out.get("locations", [])}
    for b in items:
        if b["venue"] in known:
            out["events"].append({"id": b["id"], "name": b["title"], "members": [], "dur": b["dur"], "loc": b["venue"],
                                  "t0": b["start"], "sync": None, "eligible_locs": [b["venue"]], "fixed": True})
    return out


def strip(org: dict) -> dict:
    out = copy.deepcopy(org)
    out["events"] = [e for e in out["events"] if not str(e.get("id", "")).startswith("bk-")]
    return out


def live_for_engine(db) -> dict | None:
    org = db.get_org("live")
    return None if org is None else inject(org, list_all(db))


def draft_for_engine(db) -> dict | None:
    org = db.get_org("draft")
    return None if org is None else inject(org, list_all(db))
