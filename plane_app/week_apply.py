"""Applying a saved week to the stored timetable (spec 2026-10-15-week-shape-design.md §1, R4 of the plan):
every absolute slot reference moves with its day and period number; a lesson whose period is gone stays
on its day (closed, so the check flags it) or, with no room on the day at all, is unplaced and listed."""
import copy

from . import bookings, changes, proposals, week as W


def _windows(slots: set[int]) -> list[list[int]]:
    out: list[list[int]] = []
    for t in sorted(slots):
        if out and out[-1][1] == t:
            out[-1][1] = t + 1
        else:
            out.append([t, t + 1])
    return out


def map_avail(raw, pmap, old: dict, new: dict) -> tuple[list[list[int]], bool]:
    """Availability windows on the old grid, on the new one, and whether their meaning changed.
    A day a person had whole follows a longer or shorter day; a person free every day is free on a day
    that did not exist. Otherwise each available position keeps its day and period number; one whose period
    is gone keeps its day and index when the new grid has that position as a break or closed (the engine
    never places there, so their lessons show unplaced), else is dropped. If nothing is left, the same
    (day, index) positions clamped to the new grid: never [] and never the whole cycle for a person who
    had something less. `changed` is True when any available period did not keep its day and number."""
    raw = raw or []
    wins = [raw] if len(raw) == 2 and all(isinstance(x, int) for x in raw) else raw
    had = {t for a, b in wins for t in range(max(a, 0), min(b, len(old["positions"])))}
    if not had:
        return [list(w) for w in wins if isinstance(w, (list, tuple))], False
    whole, with_periods = [], []                  # per old day: had every period (breaks do not count)
    for k in range(old["n_days"]):
        periods = {q["t"] for q in old["positions"] if q["day"] == k and q["kind"] == "period"}
        whole.append(bool(periods) and periods <= had)
        if periods:
            with_periods.append(k)
    everyday = bool(with_periods) and all(whole[k] for k in with_periods)
    keep: set[int] = set()
    changed = False
    for k in range(new["n_days"]):
        opens = {q["t"] for q in new["positions"] if q["day"] == k and q["kind"] != "closed"}
        if k < old["n_days"] and whole[k]:
            keep |= opens                       # a whole day follows a longer or shorter day
        elif k >= old["n_days"] and everyday:
            keep |= opens                       # a day that did not exist: a person free every day is free on it
    for t in sorted(had):
        q = old["positions"][t]
        if q["kind"] == "closed" or (q["day"] < len(whole) and whole[q["day"]]):
            continue
        m = pmap[t] if t < len(pmap) else None
        if m is not None:
            keep.add(m)
            changed |= q["kind"] == "period" and new["positions"][m]["kind"] != "period"
            continue
        changed |= q["kind"] == "period"
        if q["day"] < new["n_days"] and q["index"] < new["spd"]:
            same = q["day"] * new["spd"] + q["index"]
            if new["positions"][same]["kind"] != "period":
                keep.add(same)                  # closed or a break now: kept, never placed on
    if not keep:
        changed = True
        day_max, idx_max = max(new["n_days"] - 1, 0), max(new["spd"] - 1, 0)
        keep = {min(old["positions"][t]["day"], day_max) * new["spd"] + min(old["positions"][t]["index"], idx_max)
                for t in had}
    return _windows(keep), changed


def _place(t0: int, dur: int, pmap, old: dict, new: dict) -> int | None:
    """Where `dur` slots from old position `t0` go: the same day and period number when they stay
    consecutive and clear of a break, else the same index on the same day when it fits (it may sit
    on a closed or break position; the check flags it), else None."""
    mapped = [pmap[t] if 0 <= t < len(pmap) else None for t in range(t0, t0 + dur)]
    if (mapped[0] is not None and all(m == mapped[0] + i for i, m in enumerate(mapped))
            and not any(m in new["rests"] for m in mapped)):
        return mapped[0]
    day, index = divmod(t0, old["spd"]) if old["spd"] else (0, t0)
    if 0 <= t0 and day < new["n_days"] and index + dur <= new["spd"]:
        return day * new["spd"] + index
    return None


def _move(e: dict, pmap, old: dict, new: dict) -> int | None:
    return _place(e["t0"], int(e.get("dur") or 1), pmap, old, new)


def _touches(t: int, dur: int, new: dict) -> bool:
    return any(x in new["closed"] or x in new["rests"] for x in range(t, t + dur))


def remap_org(org: dict, pmap, old: dict, new: dict, avail_changed: list | None = None) -> tuple[dict, list[str]]:
    """`org` on the new grid, and the ids of lessons that could not stay on their day. The names of people
    whose availability changed meaning are appended to `avail_changed` when given."""
    out = copy.deepcopy(org)
    out["time_labels"] = list(new["labels"])
    rules = out.setdefault("rules", {})
    rules["slots_per_day"] = new["spd"]
    if not new["legacy"]:
        rules["mandatory_rest"] = []
    for k in ("closed", "rest_slots"):
        rules.pop(k, None)                      # added again at send time (engine_client.with_week)
    gone, events = [], []
    for e in out.get("events") or []:
        if str(e.get("id", "")).startswith("rest-"):
            continue
        if e.get("t0") is not None:
            t = _move(e, pmap, old, new)
            if t is None:
                gone.append(e["id"]); e["t0"] = None; e.pop("loc", None)
            else:
                e["t0"] = t
        events.append(e)
    out["events"] = events
    for p in out.get("persons") or []:
        p["avail"], changed = map_avail(p.get("avail"), pmap, old, new)
        if changed and avail_changed is not None:
            avail_changed.append(str(p.get("name") or p.get("id") or ""))
    return out, gone


def _int(v) -> int | None:
    return v if isinstance(v, int) and not isinstance(v, bool) else None


def _pin_at(pin: dict, old: dict) -> int | None:
    day, slot = _int(pin.get("day")), _int(pin.get("slot"))
    if day is None or slot is None or not (0 <= day < old["n_days"] and 0 <= slot < old["spd"]):
        return None
    return day * old["spd"] + slot


def _on_grid(org: dict, s: dict) -> bool:
    """The organisation is laid on the week `s`: the same labels, not only as many (a two-week file kept on a
    one-week timetable, or another grid of the same size, is left alone)."""
    return [str(x) for x in org.get("time_labels") or []] == list(s["labels"])


def _same_grid(old: dict, new: dict) -> bool:
    return old["labels"] == new["labels"] and [p["kind"] for p in old["positions"]] == [p["kind"] for p in new["positions"]]


def follow(db, old_time: dict, old_rules: dict, new_time: dict) -> dict:
    """Rewrite the current timetable's organisations, the plan's pins and the bookings for the new week.
    Returns what could not be kept: {"unplaced": [event ids], "bookings": [ids dropped], "pins": [names dropped],
    "avail": [names of people whose availability changed meaning]}."""
    out: dict = {"unplaced": [], "bookings": [], "pins": [], "avail": []}
    old, new = W.shape(old_time, old_rules), W.shape(new_time)
    if _same_grid(old, new):
        return out
    pmap = W.position_map(old, new)
    for kind in ("draft", "live"):
        org = db.get_org(kind)
        if org and _on_grid(org, old):
            moved, lost = remap_org(org, pmap, old, new, out["avail"])
            db.set_org(kind, moved); out["unplaced"] += lost
    plan = db.get_value("plan")
    if plan:
        plan, touched = copy.deepcopy(plan), False
        if (plan.get("rules") or {}).get("pinned"):
            kept = []
            for pin in plan["rules"]["pinned"]:
                at = _pin_at(pin, old)
                t = _place(at, int(pin.get("dur") or 1), pmap, old, new) if at is not None else None
                if t is None:
                    out["pins"].append(str(pin.get("name") or "")); continue
                pin["day"], pin["slot"] = divmod(t, new["spd"])
                kept.append(pin)
            plan["rules"]["pinned"], touched = kept, True
        for st in plan.get("staff") or []:     # absolute windows from the importer (flexi-adjunct hours, "Mon-Wed")
            if isinstance(st, dict) and st.get("avail") is not None:
                st["avail"], changed = map_avail(st["avail"], pmap, old, new)
                touched = True
                if changed:
                    out["avail"].append(str(st.get("name") or st.get("id") or ""))
        if touched:
            db.set_value("plan", plan)
    items = bookings.list_all(db)
    if items:
        kept = []
        for b in items:
            start = _int(b.get("start"))
            t = _place(start, int(b.get("dur") or 1), pmap, old, new) if start is not None else None
            if t is None:
                out["bookings"].append(b.get("id")); continue
            if t != start:
                b["start"] = t
                b["id"] = bookings._id(b)         # the id hashes the start
            kept.append(b)
        db.set_value("bookings", kept, tid=bookings._base(db))
    changes.clear(db)                              # undo would restore snapshots of the old grid
    db.plan_history_clear()
    proposals.clear_all(db)                        # a pending move or booking card names old-grid slots
    out["avail"] = list(dict.fromkeys(out["avail"]))   # the same teacher in the draft, the live and the plan
    return out


def impact(db, new_time: dict) -> dict:
    s = db.get_settings()
    old, new = W.shape(s["time"], s["rules"]), W.shape(new_time)
    org = db.get_org("live") or db.get_org("draft") or {}
    pmap, where = W.position_map(old, new), []
    if _on_grid(org, old):
        for e in org.get("events") or []:
            if e.get("t0") is None or str(e.get("id", "")).startswith("rest-"):
                continue
            t = _move(e, pmap, old, new)
            if t is None or _touches(t, int(e.get("dur") or 1), new):
                where.append(old["labels"][e["t0"]] if 0 <= e["t0"] < len(old["labels"]) else str(e["t0"]))
    nb = np = 0
    for b in bookings.list_all(db):
        start = _int(b.get("start"))
        if start is None:
            continue
        t = _place(start, int(b.get("dur") or 1), pmap, old, new)
        nb += t is None or _touches(t, int(b.get("dur") or 1), new)
    plan = db.get_value("plan")
    for pin in ((plan or {}).get("rules") or {}).get("pinned") or []:
        at = _pin_at(pin, old)
        t = _place(at, int(pin.get("dur") or 1), pmap, old, new) if at is not None else None
        np += t is None or _touches(t, int(pin.get("dur") or 1), new)
    return {"lessons": len(where), "where": where[:5], "bookings": nb, "pins": np}
