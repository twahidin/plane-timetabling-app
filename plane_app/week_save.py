"""Saving a week, the one way (spec 2026-10-15-week-shape-design.md §1): `PUT /api/settings` and an aSc
reading applied with "Adopt the file's week" both come through here, so an adopted week can never skip
the checks, a refusal, or the remap of the stored timetable."""
from . import periods, week as W, week_apply


class WeekRefused(Exception):
    """A week that cannot be saved now: `code` is the HTTP status, `message` plain words."""

    def __init__(self, code: int, message: str):
        super().__init__(message)
        self.code, self.message = code, message


def refuse_if_busy(db) -> None:
    """A week change moves bookings the base shares with its period timetables, so it waits for them to go;
    and a running build would write a grid that is no longer the week. The app sets `db.solve_probe` (is a
    build running on this timetable?); a db without one is never busy."""
    here = db.working_timetable()
    base, rec = periods.record_of(db, here)
    if rec is not None:
        name = next((t["name"] for t in db.timetables() if t["id"] == base), base)
        raise WeekRefused(409, f"This is a period timetable of {name}; change the week on {name} after removing its period timetables.")
    names = [p["name"] for p in periods.list_for(db, here)]
    if names:
        raise WeekRefused(409, f"This timetable has period timetables ({', '.join(names)}). Their weeks and room bookings follow this one, "
                               "so the week can't change while they exist. Remove them first, then change the week.")
    probe = getattr(db, "solve_probe", None)
    if probe is not None and probe():
        raise WeekRefused(409, "A build is running; wait for it to finish or cancel it, then change the week.")


def prepared(db, given: dict) -> tuple[dict, dict]:
    """(current settings, the week `given` makes on top of the current time, checked and derived)."""
    current = db.get_settings()
    time = {**current.get("time", {}), **given}
    problem = W.check(time)
    if problem:
        raise WeekRefused(400, problem)
    return current, W.derive(time)


def save(db, given: dict, body: dict | None = None) -> tuple[dict, dict]:
    """Check, derive and follow a week `given` (days, cycle, ...): refuses with WeekRefused, else remaps
    the stored timetable and returns (the settings body to store, what the new week moved or could not
    keep). The caller stores the body with `db.store_settings`."""
    current, time = prepared(db, given)
    if time != current.get("time"):
        refuse_if_busy(db)
    body = {**(body or {}), "time": time, "rules": {**((body or {}).get("rules") or {}), "mandatory_rest": []}}
    return body, week_apply.follow(db, current["time"], current["rules"], time)


def changes_text(moved: dict | None) -> str:
    """What a saved week moved or could not keep (`week_apply.follow`'s answer), in words; '' when nothing."""
    out = []
    for key, one, many in (("unplaced", "lesson could not stay on its day and is unplaced", "lessons could not stay on their day and are unplaced"),
                           ("bookings", "room booking no longer fits and was removed", "room bookings no longer fit and were removed"),
                           ("pins", "pin could not be kept", "pins could not be kept"),
                           ("avail", "person's availability no longer fits the week; check it",
                            "people's availability no longer fits the week; check it")):
        items = [str(x) for x in (moved or {}).get(key) or []]
        if items:
            out.append(f"{len(items)} {one if len(items) == 1 else many}: {', '.join(items[:5])}{' and more' if len(items) > 5 else ''}.")
    return " ".join(out)
