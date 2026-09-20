"""Proposals the user confirms. The pending list is the only thing `apply` accepts.

Nothing in the app writes to the live timetable straight from a chat message. The assistant asks the
engine for options (`propose_for`), prepares a booking (`book`) or offers to revert (`propose_undo`);
all three only store a card in the pending list of the timetable's one chat thread (`db.THREAD`). A
change happens only when the user consents, and there are exactly three ways to consent: a
confirmation word in the next message, a click on the apply route, or the model calling the `apply`
tool in a *later* message than the one that produced the card (see `chat.CONFIRM_WORDS` and the run
token in `chat.run_chat`).

`apply` then re-reads the card, refuses it if the timetable has moved under it, snapshots the
timetable through the change log, makes the change and re-checks it. Anything that goes wrong after
the snapshot — a clash, an unreachable engine, a vanished event — is rolled back through
`changes.undo`, so a half-applied proposal cannot survive this call.
"""
from __future__ import annotations

import secrets

from . import bookings, calendar as cal, changes
from .bookings import BookingError

STALE = "the timetable changed since this was proposed"
LOG_MOVED = "the change log moved since this was proposed"


# ---- the pending list: one card set per chat thread ---------------------------------------------
# Stored under the per-timetable "pending" key as {"cards": {db.THREAD: {"run", "items"}}, "runs": {db.THREAD: run}}.
# "runs" holds the run token of the most recent user message that saw the card, which is how
# `expire` lets a card live for exactly one message after the one that created it.

def _store(db) -> dict:
    v = db.get_value("pending")
    return v if isinstance(v, dict) else {}


def _write(db, cards: dict, runs: dict) -> None:
    db.set_value("pending", {"cards": cards, "runs": runs} if cards else None)


def pending(db, session_id: str = "") -> list[dict]:
    rec = (_store(db).get("cards") or {}).get(session_id)
    return list(rec["items"]) if rec else []


def set_pending(db, items: list[dict], session_id: str = "", run: str = "") -> list[dict]:
    """Stamp the cards with the run that made them and store them for the timetable's thread."""
    store = _store(db)
    cards, runs = dict(store.get("cards") or {}), dict(store.get("runs") or {})
    items = [{**i, "run": run} for i in items]
    cards[session_id] = {"run": run, "items": items}
    runs[session_id] = run
    _write(db, cards, runs)
    return items


def clear_pending(db, session_id: str = "") -> None:
    store = _store(db)
    cards, runs = dict(store.get("cards") or {}), dict(store.get("runs") or {})
    cards.pop(session_id, None)
    runs.pop(session_id, None)
    _write(db, cards, runs)


def expire(db, session_id: str, run: str) -> None:
    """Called once per user message. A card survives the message after the one that created it and
    no longer: by the message after that, the user has moved on and the offer is gone."""
    store = _store(db)
    cards, runs = dict(store.get("cards") or {}), dict(store.get("runs") or {})
    rec = cards.get(session_id)
    if rec is None:
        return
    if rec.get("run") != runs.get(session_id):
        cards.pop(session_id, None)
        runs.pop(session_id, None)
    else:
        runs[session_id] = run
    _write(db, cards, runs)


# ---- building cards ----------------------------------------------------------------------------

def _names(org: dict) -> tuple[dict, dict, list]:
    return ({e["id"]: e for e in org["events"]}, {l["id"]: l["name"] for l in org["locations"]}, org["time_labels"])


def _member_names(org: dict, e: dict) -> str:
    """The people in the lesson, by name, without the groups (a group plane stands for a whole class
    and says nothing about who is affected)."""
    group_ids = {g["id"] for g in org.get("groups") or []}
    persons = {p["id"]: p for p in org.get("persons", [])}
    out = []
    for m in e.get("members", []):
        p = persons.get(m)
        if m in group_ids or str((p or {}).get("role", "")).startswith("Group"):
            continue
        out.append((p or {}).get("name", m))
    return ", ".join(out)[:60]


def describe_move(org: dict, p: dict) -> str:
    """One line a user can check at a glance: what moves, from where, to where."""
    events, locs, labels = _names(org)
    e = events[p["event"]]
    frm = f"{locs.get(e.get('loc'), e.get('loc'))} {labels[e['t0']]}" if e.get("t0") is not None else "unplaced"
    to = f"{locs.get(p['to']['loc'], p['to']['loc'])} {labels[p['to']['t0']]}"
    if p["kind"] == "swap":
        f = events[p["with"]]
        return (f"Swap {e['name']} {labels[e['t0']]} with {f['name']} {labels[f['t0']]}"
                + (f" (both {locs.get(e['loc'], e['loc'])})" if e.get("loc") == f.get("loc") else ""))
    return f"Move {e['name']} ({_member_names(org, e)}) from {frm} to {to}"


def _placement(e: dict | None) -> dict | None:
    return None if e is None else {"loc": e.get("loc"), "t0": e.get("t0")}


def propose_for(db, engine, event: str, prefer: dict | None = None, limit: int = 3,
                session_id: str = "", run: str = "") -> list[dict]:
    org = bookings.live_for_engine(db)
    if org is None:
        raise ValueError("There is no live timetable yet.")
    by_id = {e["id"]: e for e in org["events"]}
    e = by_id.get(event)
    if e is not None and e.get("fixed"):
        raise ValueError(f"{e['name']} is pinned; unpin it before moving it")
    props = engine.propose(org, event, prefer, limit)["proposals"]
    items = [{**p, "text": describe_move(org, p), "from": _placement(by_id.get(p["event"])),
              "with_from": _placement(by_id.get(p["with"])) if p.get("with") else None} for p in props]
    return set_pending(db, items, session_id, run)


def _overlapping_event(org: dict, b: dict) -> dict | None:
    span = range(int(b["start"]), int(b["start"]) + int(b["dur"]))
    for e in org["events"]:
        if e.get("loc") == b["venue"] and e.get("t0") is not None and e.get("id") != b["id"] \
           and any(e["t0"] <= t < e["t0"] + e["dur"] for t in span):
            return e
    return None


def book(db, b: dict, session_id: str = "", run: str = "") -> dict:
    b = bookings.validate(db, b)
    # Against the organisation the engine would see, so an existing booking is found too: a booking
    # holds its cycle slot on every cycle, which is not obvious from a date and deserves saying.
    clash = _overlapping_event(bookings.live_for_engine(db) or {"events": []}, b)
    if clash is not None:
        held = next((x for x in bookings.list_all(db) if x["id"] == clash["id"]), None)
        if held is not None:
            raise BookingError(f"{b['venue']} is already booked on {held['date']} "
                               f"(bookings block the same slot in every cycle; see Settings › calendar)")
        raise BookingError(f"{b['venue']} is used by {clash['name']} at that time")
    s = db.get_settings()
    labels = s["time"]["labels"]
    venue_name = next((l["name"] for l in db.get_org("live")["locations"] if l["id"] == b["venue"]), b["venue"])
    day = cal.describe(s["calendar"], s["time"], b["date"]).split(":")[0]
    span = labels[b["start"]] + (f"–{labels[b['start'] + b['dur'] - 1]}" if b["dur"] > 1 else "")
    item = {"id": b["id"], "kind": "booking", "booking": b, "review": [],
            "text": f"Book {venue_name} on {day} {span} for {b['title']!r}"}
    return set_pending(db, [item], session_id, run)[0]


def propose_undo(db, session_id: str = "", run: str = "") -> dict | None:
    """Undoing is a change like any other, so from the chat it is offered, not done."""
    log = changes.list_all(db)
    if not log:
        return None
    # The card offers to undo *that* change; if another lands on top of the log meanwhile, the card
    # no longer means what the user was shown, so remember which entry was the head.
    item = {"id": secrets.token_hex(5), "kind": "undo", "review": [], "head": log[0]["when"],
            "text": f"Undo: {log[0]['description']}"}
    return set_pending(db, [item], session_id, run)[0]


# ---- applying ----------------------------------------------------------------------------------

def _key(c: dict) -> tuple:
    """What makes one clash that clash. Not its words: `check_person` names the person, the rooms
    and the slot but not the lessons, so a clash this change caused can read exactly like one that
    was already there (a swap that moves another lesson into the same person, room and slot, or two
    people with the same display name). The tiles ("<event>@<person>") and the event name the
    lessons involved, and they do not change when an unrelated lesson moves."""
    return (c.get("type"), c.get("event"), tuple(sorted(c.get("tiles") or ())))


def _new_clashes(check: dict, review: list[dict] | None, base: list[dict]) -> list[dict]:
    """The clashes this change is answerable for: those in the post-change review that the proposal
    did not already show the user and the timetable did not already have."""
    known = {_key(c) for c in (review or [])} | {_key(c) for c in base}
    return [c for c in check["clashes"] if _key(c) not in known]


def _stale(db, item: dict) -> str | None:
    """A card describes the timetable as it was when it was made. If either event has moved, gone or
    been pinned since, the card no longer means what the user was shown."""
    by_id = {e["id"]: e for e in (db.get_org("live") or {"events": []})["events"]}
    for eid, was in ((item.get("event"), item.get("from")), (item.get("with"), item.get("with_from"))):
        if eid is None:
            continue
        e = by_id.get(eid)
        if e is None or e.get("fixed") or (was is not None and _placement(e) != was):
            return STALE
    return None


def apply(db, engine, pid: str, session_id: str = "") -> dict:
    """Make one pending proposal real, or say why not."""
    item = next((p for p in pending(db, session_id) if p["id"] == pid), None)
    if item is None:
        return {"ok": False, "description": "nothing pending with that id", "clashes": []}
    if item["kind"] == "undo":
        log = changes.list_all(db)
        if item.get("head") is not None and (log[0]["when"] if log else None) != item["head"]:
            return {"ok": False, "description": LOG_MOVED, "clashes": []}
        undone = changes.undo(db)
        clear_pending(db, session_id)
        if undone is None:
            return {"ok": False, "description": "nothing to undo", "clashes": []}
        return {"ok": True, "description": f"Undone: {undone}", "clashes": []}
    if item["kind"] != "booking" and (why := _stale(db, item)) is not None:
        return {"ok": False, "description": why, "clashes": []}
    # What the timetable already clashes on before this change. A school's live timetable is rarely
    # clean (a keep-mode import promotes with its clashes), and a proposal's review is scoped to the
    # event it moves, so without this baseline every apply would roll back citing a clash elsewhere.
    live = bookings.live_for_engine(db)
    base = engine.check(live)["clashes"] if live is not None else []
    changes.record(db, item["kind"], item["text"])      # snapshot first, so everything below can be undone
    try:
        if item["kind"] == "booking":
            bookings.add(db, item["booking"])
        else:
            org = db.get_org("live")
            by_id = {e["id"]: e for e in org["events"]}
            e = by_id[item["event"]]
            if item["kind"] == "swap":
                f = by_id[item["with"]]
                e["loc"], e["t0"], f["loc"], f["t0"] = f["loc"], f["t0"], e["loc"], e["t0"]
            else:
                e["loc"], e["t0"] = item["to"]["loc"], item["to"]["t0"]
            db.set_org("live", org)
        check = engine.check(bookings.live_for_engine(db))
    except Exception:                  # noqa: BLE001 - an engine that failed mid-apply must leave nothing behind
        changes.undo(db)
        raise
    new = _new_clashes(check, item.get("review"), base)
    if new:
        changes.undo(db)
        return {"ok": False, "description": f"not applied: {item['text']} would clash", "clashes": new}
    db.set_value("last_check", check)
    clear_pending(db, session_id)
    return {"ok": True, "description": item["text"], "clashes": check["clashes"]}
