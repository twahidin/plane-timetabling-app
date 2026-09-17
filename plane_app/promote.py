"""The one place a build result becomes the live timetable."""
from __future__ import annotations

from .db import Db


def promote_build(db: Db, result: dict) -> bool:
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
        db.promote(result["organisation"], {"ok": not clashes, "clashes": clashes})
        return True
    return False
