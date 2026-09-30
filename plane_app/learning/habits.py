"""Suggestions from habits, learned rules and the edits-after-a-timetable measure (spec
docs/superpowers/specs/2026-09-30-learning-design.md §1.2).

Detectors are pure functions over decision rows (`log.rows`) plus the current plan, settings and relief
pool. Nothing here changes anything until `accept` is called; every accepted suggestion is kept as a
learned rule with the exact change it made (`{kind, before, after}`) so that it can be switched off again.
Stored per timetable in the `learning` kv: `{hidden: {id: until | None}, learned: [...]}`."""
from __future__ import annotations

import copy
import threading
import time
from collections import Counter

from .. import periods, relief
from ..plan import model as plan_model
from . import log

WINDOW_DAYS = 120                # decisions older than this are not read
HIDE_DAYS = 30                   # "Not now"
EDGE_MIN_COUNT = 3               # moves out of the ends of the day ...
EDGE_MIN_SHARE = 0.75            # ... and their share of the subject's moves
STABILITY_MIN_PUT_BACKS = 3      # lessons put back where a Best timetable had them ...
STABILITY_MIN_SOLVES = 2         # ... across this many Best timetables
RELIEF_MIN_OVERRIDES = 3         # times one person was picked over the first suggestion
MOVE_KINDS = ("move", "swap")
_PERIOD = "period"             # the plan's vocabulary names a person, group, requirement and venue, not a slot
_KEY = "learning"
_lock = threading.RLock()       # the `learning` kv is read, changed and written back by hide, accept and switch


class LearningError(Exception):
    pass


# ---- the stored document ---------------------------------------------------------------------------------

def _doc(db) -> dict:
    v = db.get_value(_KEY)
    v = v if isinstance(v, dict) else {}
    hidden = v.get("hidden")
    learned = v.get("learned")
    return {"hidden": dict(hidden) if isinstance(hidden, dict) else {},
            "learned": list(learned) if isinstance(learned, list) else []}


def _save(db, doc: dict) -> None:
    db.set_value(_KEY, doc)


# ---- moves of a decision row -----------------------------------------------------------------------------

def _sides(row: dict) -> list[dict]:
    """The moves a `move` or `swap` row made, as [{event, subject, from, to, spd}]. A swap is two moves: the
    first event from its `from` to its `to`, and the `with` event from `with_from` into the first event's
    `from`. The row names only the first event's subject; the `with` side's subject is left None for the
    caller to resolve (from the plan)."""
    d = row["data"]
    out = [{"event": d.get("event"), "subject": d.get("subject"), "from": d.get("from"), "to": d.get("to"),
            "spd": d.get("slots_per_day")}]
    if row["kind"] == "swap" and d.get("with"):
        out.append({"event": d["with"], "subject": None, "from": d.get("with_from"), "to": d.get("from"),
                    "spd": d.get("slots_per_day")})
    return out


def _t0(placement) -> int | None:
    try:
        return int(placement[0])
    except (TypeError, ValueError, IndexError, KeyError):
        return None


# ---- detectors (pure) ------------------------------------------------------------------------------------

def _edge(rows: list[dict], plan: dict | None, spd: int, events: dict | None = None) -> list[dict]:
    """Subjects moved out of the first or last slot of the day (and not already edge subjects)."""
    edge_subjects = set(((plan or {}).get("rules") or {}).get("edge_subjects") or [])
    total: Counter = Counter()
    out_of_ends: Counter = Counter()
    for row in rows:
        if row["kind"] not in MOVE_KINDS:
            continue
        for side in _sides(row):
            subject = side["subject"]
            if subject is None:                                    # a swap's `with` side: the plan names its subject
                subject = log.subject_of(plan, (events or {}).get(side["event"]) or {"id": side["event"]})[0]
            if not subject:
                continue
            n = int(side["spd"] or spd)
            frm, to = _t0(side["from"]), _t0(side["to"])
            if frm is None or to is None or n < 1:
                continue
            total[subject] += 1
            ends = (0, n - 1)
            if log.slot_of(frm, n)[1] in ends and log.slot_of(to, n)[1] not in ends:
                out_of_ends[subject] += 1
    found = []
    for subject, count in sorted(out_of_ends.items()):
        if count >= EDGE_MIN_COUNT and count / total[subject] >= EDGE_MIN_SHARE and subject not in edge_subjects:
            found.append({
                "id": f"edge_subject:{subject}", "kind": "edge_subject",
                "text": (f"Keep {subject} away from the first and last {_PERIOD}? "
                         f"You moved it out of the ends of the day in {count} of {total[subject]} changes."),
                "evidence": f"{count} of {total[subject]} changes to {subject} moved it out of the first or last {_PERIOD} of a day",
                "rule": f"Keeps {subject} away from the first and last {_PERIOD}.",
                "change": {"kind": "edge_subject", "before": _edge_subjects(plan), "after": _edge_subjects(plan) + [subject]}})
    return found


def _edge_subjects(plan: dict | None) -> list:
    """The plan's `rules.edge_subjects`, in the order it holds them."""
    return list(((plan or {}).get("rules") or {}).get("edge_subjects") or [])


def _stability(rows: list[dict], preset: str | None) -> dict | None:
    """Moves that put a lesson back where the last Best timetable had it, across enough Best timetables.
    "Such solves" are the Best timetables that had at least one put-back."""
    if preset == "close":
        return None
    per_solve: list[int] = []
    was: dict | None = None       # the current solve's `before`, when it is a Best one
    n = 0                         # put-backs since that solve
    for row in rows:
        if row["kind"] == "solve":
            if was is not None:
                per_solve.append(n)
            best = row["data"].get("how") == "best"
            was = {k: list(v) for k, v in (row["data"].get("before") or {}).items() if isinstance(v, (list, tuple))} if best else None
            n = 0
        elif row["kind"] in MOVE_KINDS and was is not None:
            n += sum(1 for side in _sides(row)
                     if side["event"] in was and side["to"] is not None and list(side["to"]) == was[side["event"]])
    if was is not None:
        per_solve.append(n)
    counts = [n for n in per_solve if n > 0]
    put_back, solves = sum(counts), len(counts)
    if put_back >= STABILITY_MIN_PUT_BACKS and solves >= STABILITY_MIN_SOLVES:
        return {"id": "stability", "kind": "stability",
                "text": (f"Keep new timetables closer to the last one? Across {solves} best timetables "
                         f"you put {put_back} lessons back where they were."),
                "evidence": f"{put_back} lessons put back where a best timetable had them, across {solves} best timetables",
                "rule": "Keeps new timetables close to the last one.",
                "change": {"kind": "solve_preset", "before": preset, "after": "close"}}
    return None


def _relief(rows: list[dict], pool: list[str], names: dict) -> list[dict]:
    """People picked for a cover over the assistant's first offer, often enough, who are not in the pool."""
    picked: Counter = Counter()
    for row in rows:
        if row["kind"] != "cover":
            continue
        offered, chosen = row["data"].get("offered") or [], row["data"].get("chosen")
        if chosen and offered and chosen != offered[0]:
            picked[chosen] += 1
    found = []
    for person, count in sorted(picked.items()):
        if count >= RELIEF_MIN_OVERRIDES and person not in pool and person in names:
            found.append({
                "id": f"relief_pool:{person}", "kind": "relief_pool",
                "text": f"Add {names[person]} to the relief pool? You picked them over the first suggestion {count} times.",
                "evidence": f"chosen over the first offer for {count} covers",
                "rule": f"Asks {names[person]} first for relief cover.",
                "change": {"kind": "relief_pool", "before": list(pool), "after": list(pool) + [person]}})
    return found


# ---- suggestions -----------------------------------------------------------------------------------------

def suggestions(db, now: float | None = None) -> list[dict]:
    """[{id, kind, text, evidence, rule, change}] from the last 120 days of decisions, minus the hidden ones and the
    ones already learned (active or switched off: switching a rule off never makes its suggestion come back)."""
    now = time.time() if now is None else now
    since = now - WINDOW_DAYS * 86400
    plan = db.get_value("plan") or None
    spd = int(db.get_settings()["time"]["slots_per_day"])
    base = periods.base_tid(db)
    org = db.get_org("live", tid=base) or {}
    events = {e.get("id"): e for e in org.get("events") or []}
    names = {p["id"]: p.get("name") or p["id"] for p in org.get("persons") or [] if p.get("id")}
    rows = log.rows(db, since=since, kinds=MOVE_KINDS + ("solve",))
    found = _edge(rows, plan, spd, events)
    if (s := _stability(rows, db.get_settings()["solve"]["preset"])) is not None:
        found.append(s)
    found += _relief(log.rows(db, since=since, kinds=("cover",), tid=base), relief.settings(db)["pool"], names)
    doc = _doc(db)
    known = {e.get("id") for e in doc["learned"]}
    return [s for s in found if s["id"] not in known and not _is_hidden(doc["hidden"], s["id"], now)]


def _is_hidden(hidden: dict, sid: str, now: float) -> bool:
    if sid not in hidden:
        return False
    until = hidden[sid]
    return until is None or until > now


def hide(db, sid: str, days: int | None) -> None:
    """Hide a suggestion for `days` ("Not now": 30), or for good (`None`, "Never")."""
    with _lock:
        doc = _doc(db)
        doc["hidden"][str(sid)] = None if days is None else time.time() + int(days) * 86400
        _save(db, doc)


# ---- learned rules ---------------------------------------------------------------------------------------

def learned(db) -> list[dict]:
    """The learned rules; each has a `rule` (what it does), which an entry kept before rules were worded
    falls back from to its `text`."""
    out = copy.deepcopy(_doc(db)["learned"])
    for e in out:
        e.setdefault("rule", e.get("text"))
    return out


def _current(db, kind: str):
    if kind == "edge_subject":
        return _edge_subjects(db.get_value("plan"))
    if kind == "solve_preset":
        return db.get_settings()["solve"]["preset"]
    if kind == "relief_pool":
        return relief.settings(db)["pool"]
    raise LearningError(f"unknown change kind {kind!r}")


def _apply(db, kind: str, value) -> None:
    """Write `value` where a change of this kind lives, through the writer the app itself uses."""
    if kind == "edge_subject":
        with db.plan_lock():                                       # as PATCH /api/plan: normalised, under the plan lock
            plan = db.get_value("plan") or plan_model.empty_plan()
            try:
                new = plan_model.apply_patch(plan, {"rules": {"edge_subjects": list(value)}})
            except plan_model.PlanError as e:
                raise LearningError(str(e)) from e
            db.set_value("plan", new)
    elif kind == "solve_preset":
        db.store_settings({"solve": {"preset": value}})            # keeps the weights and the time limit
    elif kind == "relief_pool":
        try:
            relief.set_settings(db, {"pool": list(value)})
        except relief.ReliefError as e:
            raise LearningError(str(e)) from e
    else:
        raise LearningError(f"unknown change kind {kind!r}")


def accept(db, sid: str) -> dict:
    """Apply a suggestion's change and keep it as a learned rule; returns the learned entry."""
    with _lock:
        found = next((s for s in suggestions(db) if s["id"] == sid), None)
        if found is None:
            raise LearningError("no such suggestion")
        change = found["change"]
        _apply(db, change["kind"], change["after"])
        entry = {"id": found["id"], "text": found["text"], "rule": found["rule"], "change": copy.deepcopy(change),
                 "accepted_at": time.time(), "active": True}
        doc = _doc(db)
        doc["learned"] = [e for e in doc["learned"] if e.get("id") != entry["id"]] + [entry]     # accepting again replaces the old entry
        _save(db, doc)
    log.safe_record(db, "accepted", {"suggestion": entry["id"], "change": copy.deepcopy(change)})
    return copy.deepcopy(entry)


def switch(db, lid: str, on: bool) -> dict:
    """Switch a learned rule off (restore `before`) or on (reapply `after`), only while the current value
    is still the one the rule last set; otherwise refuse and change nothing."""
    with _lock:
        doc = _doc(db)
        entry = next((e for e in doc["learned"] if e.get("id") == lid), None)
        if entry is None:
            raise LearningError("no such learned rule")
        on = bool(on)
        if entry["active"] == on:
            return copy.deepcopy(entry)
        change = entry["change"]
        # off: the value must still be `after`, and goes back to `before`; on: it must still be `before`, and goes to `after`
        was, will_be = (change["before"], change["after"]) if on else (change["after"], change["before"])
        if _current(db, change["kind"]) != was:
            raise LearningError("changed by hand since")
        _apply(db, change["kind"], will_be)
        entry["active"] = on
        _save(db, doc)
    log.safe_record(db, "learned_on" if on else "learned_off", {"suggestion": lid, "change": copy.deepcopy(change)})
    return copy.deepcopy(entry)


# ---- the measure -----------------------------------------------------------------------------------------

def edits_after_solves(db, last: int = 10) -> dict:
    """For each of the last `last` timetables built (a `solve` row), how many moves and swaps followed it
    before the next one (the newest counts up to now). Oldest first, with their average."""
    counts: list[int] = []
    for row in log.rows(db, kinds=("solve",) + MOVE_KINDS, data=False):
        if row["kind"] == "solve":
            counts.append(0)
        elif counts:
            counts[-1] += 1
    counts = counts[-last:] if last > 0 else []
    return {"per_solve": counts, "average": (sum(counts) / len(counts)) if counts else None}
