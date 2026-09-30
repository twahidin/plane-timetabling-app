"""The one place a build result becomes the live timetable."""
from __future__ import annotations

from .db import Db
from .learning import log as decisions


def promote_build(db: Db, result: dict, how: str = "quick", preset: str | None = None) -> bool:
    """Promote the engine's build result when it settled: nothing unplaced and no clashes.

    On success the built organisation becomes live, the draft is cleared and last_check records a
    clean check, all in one transaction. Returns whether the build was promoted."""
    clashes = result.get("clashes") or []
    settled = not result["unplaced"] and not clashes
    # An import that keeps an existing placement has nothing for the engine to place: it is the
    # organisation's real timetable, clashes included. Promote it so it can be inspected, and let
    # the Reviewer panel show the clashes.
    all_fixed = not result.get("placed") and not result["unplaced"] and all(
        e.get("fixed") for e in result["organisation"].get("events", []) if e.get("loc") is not None)
    if settled or all_fixed:
        before = _placements(db.get_org("live"))            # read before the live timetable is replaced
        db.promote(result["organisation"], {"ok": not clashes, "clashes": clashes})
        _log_solve(db, how, preset, before)
        return True
    return False


def _placements(org: dict | None) -> dict:
    """{event id: [t0, loc]} of every event of `org` (empty when there is none)."""
    try:
        return {e["id"]: [e.get("t0"), e.get("loc")] for e in (org or {}).get("events") or []}
    except (KeyError, TypeError, AttributeError):        # a malformed org must not stop a promotion
        return {}


def _log_solve(db: Db, how: str, preset: str | None, before: dict) -> None:
    """The decision log's `solve` row. `how` is "quick" (a one-shot build) or "best" (a solve job, which
    also names its `preset`; the settings' preset stands in when the caller has none). Only a Best timetable keeps
    its `before` (the stability detector reads it); a quick build's row stays small. Never raises."""
    try:
        if how == "best" and preset is None:
            preset = db.get_settings()["solve"]["preset"]
        data = {"how": how, "preset": preset, "before": before} if how == "best" else {"how": how, "preset": None}
    except Exception:  # noqa: BLE001
        data = {"how": how, "preset": None, **({"before": before} if how == "best" else {})}
    decisions.safe_record(db, "solve", data)
