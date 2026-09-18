"""A term calendar maps dates to cycle days. The engine stays calendar-free; the app does the mapping."""
from __future__ import annotations

from datetime import date as _date


def cycle_days(time: dict) -> int:
    spd = int(time.get("slots_per_day") or len(time.get("labels") or [1]))
    return max(1, len(time.get("labels") or []) // spd)


def _parse(s: str) -> _date | None:
    try:
        return _date.fromisoformat(s)
    except (TypeError, ValueError):
        return None


def cycle_day(cal: dict, time: dict, date: str) -> int | None:
    start, d = _parse(cal.get("term_start") or ""), _parse(date)
    if start is None or d is None or d < start or d.weekday() > 4 or date in (cal.get("non_teaching_dates") or []):
        return None
    n = cycle_days(time)
    weeks = (d - start).days // 7
    offset = 0 if cal.get("first_week", "odd") == "odd" else (5 if n == 10 else n // 2)
    return (weeks * 5 + d.weekday() + offset) % n


def slot_range(cal: dict, time: dict, date: str) -> tuple[int, int] | None:
    k = cycle_day(cal, time, date)
    if k is None:
        return None
    spd = int(time.get("slots_per_day") or len(time.get("labels") or [1]))
    return k * spd, min((k + 1) * spd, len(time.get("labels") or []))


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
    week = "Odd" if (k < n / 2) else "Even"
    return f"{label}: {week} week, day {k + 1} (slots {rng[0]}–{rng[1] - 1})"


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
