"""A term calendar maps dates to cycle days. The engine stays calendar-free; the app does the mapping."""
from __future__ import annotations

from datetime import date as _date, timedelta

from . import week as W


def cycle_days(time: dict) -> int:
    return max(1, W.shape(time)["n_days"])


def _parse(s: str) -> _date | None:
    try:
        return _date.fromisoformat(s)
    except (TypeError, ValueError):
        return None


def cycle_day(cal: dict, time: dict, date: str) -> int | None:
    start, d = _parse(cal.get("term_start") or ""), _parse(date)
    off = set(cal.get("non_teaching_dates") or [])
    if start is None or d is None or d < start or d.weekday() > 4 or date in off:
        return None
    n = cycle_days(time)
    if (time or {}).get("cycle") == "rolling":
        count, k = 0, start
        while k < d:                               # school days before this one: weekdays that are not holidays
            if k.weekday() <= 4 and k.isoformat() not in off:
                count += 1
            k += timedelta(days=1)
        first = min(max(int(time.get("rolling_start_day") or 1), 1), n) - 1
        return (first + count) % n
    weeks = (d - start).days // 7
    if cal.get("first_week", "odd") == "odd":
        offset = 0
    elif not W.shape(time)["legacy"]:
        # "even" = the term starts in week 2 of a 2-4 week cycle; a one-week or one-day cycle has no week 2
        # (the select is hidden there, and a stale stored "even" must not shift every date)
        offset = 5 if n >= 10 else 0
    else:
        offset = 5 if n == 10 else n // 2
    return (weeks * 5 + d.weekday() + offset) % n


def slot_range(cal: dict, time: dict, date: str) -> tuple[int, int] | None:
    k = cycle_day(cal, time, date)
    if k is None:
        return None
    s = W.shape(time)                              # a time given only as days has no stored labels
    spd = s["spd"] or 1
    return k * spd, min((k + 1) * spd, len(s["labels"]))


def slot_word(time: dict, t: int) -> str:
    """Absolute position `t` in its day's own words: "P3" (or its clock time, or a break's name) on a
    saved week, "slot 3" (one-based within the day) on an old one. Never a zero-based absolute number."""
    s = W.shape(time)
    i = t % (s["spd"] or 1)
    if not s["legacy"] and 0 <= t < len(s["positions"]):
        p = s["positions"][t]
        if p["kind"] == "period":
            return p["start"] if (time or {}).get("label_style") == "clock" else f"P{p['period']}"
        if p["kind"] == "break":
            return str(p["name"])
    return f"slot {i + 1}"


def slot_words(time: dict, a: int, b: int) -> str:
    """Positions `a` to `b` (inclusive) in their day's own words: "P4–P5", "slots 5–6", "P2"."""
    first, last = slot_word(time, a), slot_word(time, b)
    if first == last:
        return first
    if first.startswith("slot ") and last.startswith("slot "):
        return f"slots {first[5:]}–{last[5:]}"
    return f"{first}–{last}"


def _day_words(time: dict, k: int) -> str:
    """Day `k`'s periods in its own words: "P1–P9" (or clock times) on a saved week, "slots 1–8" on an old one."""
    s = W.shape(time)
    if s["legacy"]:
        return f"slots 1–{s['spd']}"
    periods = [p for p in s["positions"] if p["day"] == k and p["kind"] == "period"]
    if not periods:
        return "no periods"
    if (time or {}).get("label_style") == "clock":
        return f"{periods[0]['start']}–{periods[-1]['end']}"
    a, b = periods[0]["period"], periods[-1]["period"]
    return f"P{a}" if a == b else f"P{a}–P{b}"


def describe(cal: dict, time: dict, date: str) -> str:
    d = _parse(date)
    label = d.strftime("%a %-d %b") if d else date
    if d is None:
        return f"{date}: not a date"
    if date in (cal.get("non_teaching_dates") or []):
        return f"{label}: no lessons (non-teaching day)"
    rng = slot_range(cal, time, date)
    if rng is None:
        return f"{label}: no lessons"
    k = cycle_day(cal, time, date)
    n = cycle_days(time)
    words = _day_words(time, k)
    if (time or {}).get("cycle") == "rolling":
        return f"{label}: Day {k + 1} ({words})"
    s = W.shape(time)
    if not s["legacy"]:
        return f"{label}: {s['days'][k]}, day {k + 1} ({words})"
    week = "Odd" if (k < n / 2) else "Even"
    return f"{label}: {week} week, day {k + 1} ({words})"


def clean(cal, current: dict) -> dict:
    if not isinstance(cal, dict):
        return dict(current)
    out = dict(current)
    ts = cal.get("term_start", out.get("term_start", ""))
    if ts == "" or (isinstance(ts, str) and (p := _parse(ts)) is not None and p.weekday() == 0):
        out["term_start"] = ts
    fw = cal.get("first_week", out.get("first_week", "odd"))
    if fw in ("odd", "even"):
        out["first_week"] = fw
    nt = cal.get("non_teaching_dates", out.get("non_teaching_dates", []))
    if isinstance(nt, list):
        out["non_teaching_dates"] = sorted({s for s in nt if isinstance(s, str) and _parse(s) is not None})
    return out
