"""The per-timetable `levels` store: what is known about each level before any class exists.

Applying requirements.csv writes it, beside the plan rather than inside it. It is keyed by
`plan.specialisation.level_key`, and each entry keeps the school's own spelling in "name":

    {level_key: {"name": "Sec 3", "students_expected": 120 | None, "class_size": 40, "naming": "",
                 "subjects": [{"subject", "periods", "taught_as", "combine", "room_kind", "dept"}]}}

A subject row may also carry three optional keys from the deployment file, each present only when given
non-empty (so an older row keeps exactly the six keys above):

    "lessons":   {"2": 2, "1": 1}     the lesson pattern, {length: count}; lengths 1..LESSON_LENGTH_MAX,
                                      counts at least 1, SPLIT_LESSONS_MAX lessons at most
    "runs_with": "Mother Tongue"      the subject (or group) it runs at the same time as, trimmed
    "deploy":    [{"teacher": str | None, "classes": int | None, "groups": int | None}]
                                      who teaches it and how many classes / groups each; an entry
                                      needs a teacher or a count, counts are whole numbers of 1 or more

The Classes step reads it, and option blocks reads the periods and room kind of Banded subjects from it.
No student data lives here, only counts and subject rows.

`normalise_levels` raises ValueError carrying a short code (never a value from the data), so a message
built from it cannot leak a name."""
from __future__ import annotations

import logging

from .plan.model import PlanError, normalise_lessons
from .plan.specialisation import level_key

log = logging.getLogger("plane_app")

KEY = "levels"
DEFAULT_CLASS_SIZE = 40
TAUGHT_AS = ("class", "combined", "banded")


def _whole(value, floor: int, code: str, default=None) -> int:
    """`value` as a whole number of at least `floor`; ValueError(code) otherwise (a bool is not a number)."""
    if value is None and default is not None:
        return default
    if isinstance(value, bool):
        raise ValueError(code)
    if isinstance(value, str):
        try:
            value = int(value.strip())          # "²" is a digit to str.isdigit() but not to int(); never let int()'s message out
        except ValueError:
            raise ValueError(code) from None
    elif isinstance(value, float) and value.is_integer():
        value = int(value)
    if not isinstance(value, int) or value < floor:
        raise ValueError(code)
    return value


def _text(value) -> str:
    return value.strip() if isinstance(value, str) else ""


def _lessons(raw) -> dict:
    """A stored lesson pattern `{length: count}` as `{str: int}`, zero-free; ValueError("bad_lessons").
    The ranges are plan.model.normalise_lessons's, so a pattern kept here is one the plan accepts."""
    if not isinstance(raw, dict):
        raise ValueError("bad_lessons")
    try:
        lessons = {str(k): _whole(v, 1, "bad_lessons") for k, v in raw.items()}
        normalise_lessons(lessons)
    except PlanError:
        raise ValueError("bad_lessons") from None
    return lessons


def _deploy(raw) -> list:
    """The deployment entries as `{"teacher", "classes", "groups"}`; ValueError("bad_deploy")."""
    if not isinstance(raw, list):
        raise ValueError("bad_deploy")
    out = []
    for item in raw:
        if not isinstance(item, dict):
            raise ValueError("bad_deploy")
        entry = {"teacher": _text(item.get("teacher")) or None}
        for count in ("classes", "groups"):
            value = item.get(count)
            entry[count] = None if value is None else _whole(value, 1, "bad_deploy")
        if entry["teacher"] is None and entry["classes"] is None and entry["groups"] is None:
            raise ValueError("bad_deploy")
        out.append(entry)
    return out


def _subject(raw) -> dict:
    if not isinstance(raw, dict):
        raise ValueError("bad_subject")
    name = _text(raw.get("subject"))
    if not name:
        raise ValueError("subject_missing")
    periods = _whole(raw.get("periods"), 1, "periods_below_1")
    taught_as = raw.get("taught_as", "class")
    if taught_as not in TAUGHT_AS:
        raise ValueError("bad_taught_as")
    # combine is how many classes share a lesson: required (2 or more) only for "combined", 1 otherwise
    combine = _whole(raw.get("combine"), 2, "combine_below_2") if taught_as == "combined" else 1
    subject = {"subject": name, "periods": periods, "taught_as": taught_as, "combine": combine,
               "room_kind": _text(raw.get("room_kind")) or None, "dept": _text(raw.get("dept"))}
    # the deployment fields are kept only when given, so a row from an older file stays as it was
    if raw.get("lessons"):
        subject["lessons"] = _lessons(raw["lessons"])
    if _text(raw.get("runs_with")):
        subject["runs_with"] = _text(raw["runs_with"])
    if raw.get("deploy"):
        subject["deploy"] = _deploy(raw["deploy"])
    return subject


def _level(key: str, raw) -> dict:
    if not isinstance(raw, dict):
        raise ValueError("bad_level")
    expected = raw.get("students_expected")
    subjects = raw.get("subjects")
    if subjects is not None and not isinstance(subjects, list):
        raise ValueError("bad_subject")
    return {"name": _text(raw.get("name")) or key,
            "students_expected": None if expected is None else _whole(expected, 0, "students_expected_below_0"),
            "class_size": _whole(raw.get("class_size"), 1, "class_size_below_1", default=DEFAULT_CLASS_SIZE),
            "naming": _text(raw.get("naming")),
            "subjects": [_subject(s) for s in subjects or []]}


def normalise_levels(raw) -> dict:
    """`raw` as the stored shape: keyed by `level_key`, defaults filled, unknown keys dropped, subjects in
    file order. A level's key is taken from the entry's key as written (so "Sec 3" and "3" are one
    level; two keys that meet raise "duplicate_level"). ValueError(code) on a bad value."""
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        raise ValueError("bad_levels")
    out: dict = {}
    for written, entry in raw.items():
        if not isinstance(written, str) or not written.strip():
            raise ValueError("bad_level")
        key = level_key(written)
        if not key:
            raise ValueError("bad_level")
        if key in out:
            raise ValueError("duplicate_level")
        level = _level(written.strip(), entry)
        out[key] = level
    return out


def get(db, tid: str | None = None) -> dict:
    """The timetable's levels. Bad stored data is logged by type name and read as no levels; it never raises."""
    stored = db.get_value(KEY, tid)
    try:
        return normalise_levels(stored)
    except Exception as e:          # noqa: BLE001 - a store that cannot be read is an empty one, never a failed page
        log.warning("levels for a timetable could not be read (%s from %s); treated as none",
                    type(e).__name__, type(stored).__name__)
        return {}


def put(db, tid: str | None, value) -> dict:
    """Normalise `value` and store it, returning what was stored. ValueError propagates, storing nothing."""
    value = normalise_levels(value)
    db.set_value(KEY, value, tid)
    return value


def banded_meta(levels: dict, level) -> dict:
    """The Banded subjects of one level as {subject: {"periods", "room_kind", "dept", "lessons" (only with a
    lesson pattern)}}. `level` is a level name or a key; either goes through `level_key`. The result is keyed
    by the subject AS WRITTEN in the file, so a caller matches it by `students.norm` of both sides ("Physics"
    and "physics" meet)."""
    entry = (levels or {}).get(level_key(level))
    if not entry:
        return {}
    return {s["subject"]: {"periods": s["periods"], "room_kind": s["room_kind"], "dept": s["dept"],
                           **({"lessons": dict(s["lessons"])} if s.get("lessons") else {})}
            for s in entry["subjects"] if s["taught_as"] == "banded"}
