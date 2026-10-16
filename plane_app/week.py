"""The shape of the week (spec docs/superpowers/specs/2026-10-15-week-shape-design.md §2–3).

A timetable's settings `time` may carry `days`: one row a day of the cycle, each with its own start,
number of periods and breaks. Every day keeps the same number of positions (the longest day's); a
shorter day's extra positions are closed. Without `days` the week is "every day the same", read from
the old fields and the rules' mandatory rest offsets, and its stored labels are kept exactly.
Pure functions; static/week.js is the same reading in the browser."""
import re

MAX_DAYS = 20
MAX_POSITIONS = 30
WEEKDAYS = ("Mon", "Tue", "Wed", "Thu", "Fri")
WEEK_DAY_COUNTS = (1, 5, 10, 15, 20)            # the day counts a week-based cycle may have (R1: one day too)
_HM = re.compile(r"^(\d{1,2}):(\d{2})$")


def _int(v, default=None):
    return v if isinstance(v, int) and not isinstance(v, bool) else default


def minutes(hm) -> int | None:
    m = _HM.match(str(hm or ""))
    if not m:
        return None
    h, mi = int(m.group(1)), int(m.group(2))
    return h * 60 + mi if h < 24 and mi < 60 else None


def clock(total: int) -> str:
    total %= 24 * 60
    return f"{total // 60:02d}:{total % 60:02d}"


def day_names(cycle: str, n: int) -> list[str]:
    if n == 1:
        return ["Day"]
    if cycle == "rolling" or n % 5:
        return [f"Day {k + 1}" for k in range(n)]
    weeks = n // 5
    if weeks == 1:
        return list(WEEKDAYS)
    if weeks == 2:
        return [f"{w} {d}" for w in ("Odd", "Even") for d in WEEKDAYS]
    return [f"W{w + 1} {d}" for w in range(weeks) for d in WEEKDAYS]


def from_legacy(time: dict, rules: dict | None = None) -> dict:
    """The week of a timetable saved before `days`: every day the same, the rest offsets as breaks
    (a break after the periods before it; consecutive offsets are consecutive breaks)."""
    labels = [str(x) for x in time.get("labels") or []]
    spd = _int(time.get("slots_per_day")) or len(labels) or 1
    n = max(1, len(labels) // spd) if labels else 1
    rest = sorted({o for o in ((rules or {}).get("mandatory_rest") or []) if _int(o) is not None and 0 <= o < spd})
    mins = _int(time.get("slot_minutes"), 40) or 40
    breaks = [{"after": o - i, "name": "Break", "minutes": mins} for i, o in enumerate(rest)]
    names = []
    for k in range(n):
        first = labels[k * spd] if k * spd < len(labels) else ""
        names.append(first.rsplit(" ", 1)[0] if " " in first else (f"Day {k + 1}" if n > 1 else "Day"))
    start = str(time.get("start") or "07:30")
    cycle = time.get("cycle") or "weeks"
    if cycle == "weeks" and n not in WEEK_DAY_COUNTS:
        cycle = "rolling"                       # a 6- or 8-day cycle is not whole weeks: read it as Day 1…N
    return {**{k: v for k, v in time.items() if k not in ("days",)}, "cycle": cycle,
            "days": [{"name": names[k], "start": start, "periods": spd - len(rest),
                      "breaks": [dict(b) for b in breaks]} for k in range(n)]}


def _items(d: dict) -> list[dict]:
    """A day's positions in order: periods, with each break after its period (after == 0 first)."""
    periods = _int(d.get("periods"), 0) or 0
    breaks = sorted((b for b in d.get("breaks") or [] if isinstance(b, dict)), key=lambda b: _int(b.get("after"), 0))
    out, bi = [], 0
    for p in range(periods + 1):
        while bi < len(breaks) and _int(breaks[bi].get("after"), 0) == p:
            out.append({"kind": "break", "name": str(breaks[bi].get("name") or "Break"),
                        "minutes": _int(breaks[bi].get("minutes"), 0) or 0})
            bi += 1
        if p < periods:
            out.append({"kind": "period", "period": p + 1})
    return out


def shape(time: dict | None, rules: dict | None = None) -> dict:
    time = dict(time or {})
    legacy = not (isinstance(time.get("days"), list) and any(isinstance(d, dict) for d in time["days"]))
    week = from_legacy(time, rules) if legacy else time
    mins = _int(time.get("slot_minutes"), 40) or 40
    days = [d for d in week["days"] if isinstance(d, dict)]
    rows = [_items(d) for d in days]
    spd = max((len(r) for r in rows), default=0)
    stored = [str(x) for x in time.get("labels") or []]
    style = time.get("label_style") or "period"
    names = [str(d.get("name") or f"Day {k + 1}") for k, d in enumerate(days)]
    one = len(days) == 1
    positions, closed, rests, open_per_day = [], [], [], []
    for k, (d, row) in enumerate(zip(days, rows)):
        cur = minutes(d.get("start")) if minutes(d.get("start")) is not None else (minutes(time.get("start")) or 450)
        for i in range(spd):
            t = k * spd + i
            it = row[i] if i < len(row) else {"kind": "closed"}
            start = end = None
            if it["kind"] == "period":
                start, end = clock(cur), clock(cur + mins); cur += mins
                word = start if style == "clock" else f"P{it['period']}"
            elif it["kind"] == "break":
                start, end = clock(cur), clock(cur + it["minutes"]); cur += it["minutes"]
                word = it["name"]; rests.append(t)
            else:
                word = "—"; closed.append(t)
            label = stored[t] if legacy and t < len(stored) else (word if one else f"{names[k]} {word}")
            positions.append({"t": t, "day": k, "index": i, "kind": it["kind"], "period": it.get("period"),
                              "name": it.get("name"), "label": label, "start": start, "end": end})
        open_per_day.append(sum(1 for it in row if it["kind"] == "period"))
    return {"legacy": legacy, "cycle": week.get("cycle") or "weeks", "days": names, "n_days": len(days), "spd": spd,
            "positions": positions, "closed": closed, "rests": rests, "open_per_day": open_per_day,
            "labels": [p["label"] for p in positions]}


def check(time: dict) -> str | None:
    """Why this week cannot be saved, or None."""
    mins = _int(time.get("slot_minutes"))
    if mins is None or not 5 <= mins <= 240:
        return "Give a period length between 5 and 240 minutes."
    cycle = time.get("cycle") or "weeks"
    if cycle not in ("weeks", "rolling"):
        return "The cycle is week-based or rolling."
    days = time.get("days")
    if not isinstance(days, list) or not days:
        return "Give at least one day."
    if len(days) > MAX_DAYS:
        return f"A cycle has at most {MAX_DAYS} days; this one has {len(days)}."
    if cycle == "rolling" and not 2 <= len(days) <= MAX_DAYS:
        return f"A rolling cycle has 2 to {MAX_DAYS} days."
    if cycle == "weeks" and len(days) not in WEEK_DAY_COUNTS:
        return "A week-based cycle has 1, 5, 10, 15 or 20 days (one to four weeks)."
    for d in days:
        if not isinstance(d, dict):
            return "Each day is a row with a name, a start and periods."
        name = str(d.get("name") or "").strip() or "A day"
        periods = _int(d.get("periods"))
        if periods is None or periods < 1:
            return f"{name}: give at least one period."
        start = minutes(d.get("start"))
        if start is None:
            return f"{name}: the start time must be HH:MM."
        breaks = d.get("breaks") or []
        if not isinstance(breaks, list):
            return f"{name}: breaks are a list."
        if periods + len(breaks) > MAX_POSITIONS:
            return f"{name}: {MAX_POSITIONS} positions a day at most (periods and breaks); this day has {periods + len(breaks)}."
        total = periods * mins
        for b in breaks:
            if not isinstance(b, dict):
                return f"{name}: each break needs a position, a name and minutes."
            after = _int(b.get("after"))
            if after is None or not 1 <= after <= periods - 1:
                return f"{name}: a break must sit between two periods (after P1 to after P{periods - 1})."
            bname = str(b.get("name") or "").strip()
            bm = _int(b.get("minutes"))
            if not bname or bm is None or not 1 <= bm <= 240:
                return f"{name}: {bname or 'a break'} needs a name and 1 to 240 minutes."
            total += bm
        if start + total >= 24 * 60:
            return f"{name}: the day runs past midnight."
    return None


def derive(time: dict) -> dict:
    """A checked week made ready to store: breaks in order, labels and positions a day written out."""
    out = {k: v for k, v in time.items() if k != "legacy"}
    out["cycle"] = out.get("cycle") or "weeks"
    out["days"] = [{"name": str(d.get("name") or "").strip(), "start": d["start"], "periods": int(d["periods"]),
                    "breaks": sorted(({"after": int(b["after"]), "name": str(b["name"]).strip(), "minutes": int(b["minutes"])}
                                      for b in d.get("breaks") or []), key=lambda b: b["after"])}
                   for d in out["days"]]
    s = shape(out)
    out["labels"], out["slots_per_day"] = s["labels"], s["spd"]
    if out["cycle"] == "rolling":
        out["rolling_start_day"] = min(max(_int(out.get("rolling_start_day"), 1) or 1, 1), s["n_days"])
    else:
        out.pop("rolling_start_day", None)
    return out


def engine_rules(time: dict, rules: dict | None) -> dict:
    """What the engine's rules gain for this week: nothing for a legacy week (it solves as before).
    `n_slots` and `labels` say which grid the lists are for; they are never sent (engine_client.with_week)."""
    s = shape(time, rules)
    if s["legacy"]:
        return {}
    return {"slots_per_day": s["spd"], "mandatory_rest": [], "closed": s["closed"], "rest_slots": s["rests"],
            "n_slots": len(s["labels"]), "labels": list(s["labels"])}


def _key(p: dict, ordinal: int):
    if p["kind"] == "period":
        return (p["day"], "period", p["period"])
    if p["kind"] == "break":
        return (p["day"], "break", ordinal)
    return None


def _keys(s: dict) -> list:
    out, seen = [], {}
    for p in s["positions"]:
        n = seen.get((p["day"], p["kind"]), 0)
        seen[(p["day"], p["kind"])] = n + 1
        out.append(_key(p, n))
    return out


def position_map(old: dict, new: dict) -> list[int | None]:
    """For each old position, the new position with the same day and period number (or break ordinal)."""
    where = {k: t for t, k in enumerate(_keys(new)) if k is not None}
    return [where.get(k) if k is not None else None for k in _keys(old)]


def describe(s: dict) -> list[str]:
    out = []
    for k, name in enumerate(s["days"]):
        ps = [p for p in s["positions"] if p["day"] == k and p["kind"] != "closed"]
        if not ps:
            out.append(f"{name} · no periods"); continue
        periods = sum(1 for p in ps if p["kind"] == "period")
        line = f"{name} · {ps[0]['start']}–{ps[-1]['end']} · {periods} period{'s' if periods != 1 else ''}"
        last = 0
        for p in ps:
            if p["kind"] == "period":
                last = p["period"]
            else:
                line += f" · {p['name']} after P{last}"
        out.append(line)
    return out
