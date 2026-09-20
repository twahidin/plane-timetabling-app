"""The start wizard's curated template library (spec
docs/superpowers/specs/2026-09-20-start-wizard-design.md §3).

One JSON file per template, shipped as package data. The model chooses a template and sets its
knobs; everything the wizard produces afterwards — settings, workbook, sample organisation — is
rendered by the app from the template, so a template that is wrong is a bug the tests must catch
rather than something a user can type. Validation is by hand: the app has no jsonschema
dependency and the shape is small enough to read.
"""
from __future__ import annotations

import copy
import json
from pathlib import Path

LIBRARY_DIR = Path(__file__).parent / "library"

# The launch table of spec §3, in the order the wizard offers them.
TEMPLATE_IDS = (
    "edu-secondary-bands", "edu-primary-class-teacher", "edu-college-modules",
    "health-ward-rota", "health-clinic-sessions",
    "biz-rooms-desks", "biz-service-desk",
    "sports-courts-coaches", "sports-tournament",
)
DOMAINS = (("education", "Education"), ("health", "Health"),
           ("business", "Business"), ("sports", "Sports"))

CARD_FIELDS = ("id", "name", "summary", "when_to_choose")   # what a candidate card shows
VOCABULARY_KEYS = ("person", "group", "requirement", "venue")
LABEL_KINDS = ("day-period", "day-hour", "period")
SHEET_KINDS = ("deployment", "generic")
KNOB_KINDS = ("int", "choice", "bool")
MAX_QUESTIONS = 6

# Required keys and their kinds. Nested shapes follow; `list` and `dict` members are checked
# item by item below, which is where the messages that name a file's fault come from.
TEMPLATE_SCHEMA = {
    "id": str, "domain": str, "name": str, "summary": str, "when_to_choose": str,
    "vocabulary": dict, "knobs": list, "questions": list, "tradeoffs": list,
    "time": dict, "rules": dict, "sheets": str, "sample": dict,
}
TIME_SCHEMA = {"slot_minutes": int, "day_start": str, "slots_per_day": int,
               "labels": str, "days_per_week": int}
RULES_SCHEMA = {"max_load_slots": int, "max_run_slots": int, "mandatory_rest": list}
KNOB_SCHEMA = {"key": str, "label": str, "help": str, "kind": str}
# `sheets` decides the shape of `sample`: the deployment workbook is the education one.
SAMPLE_SCHEMA = {
    "deployment": {"levels": int, "classes_per_level": int, "subjects": list, "teachers": int},
    "generic": {"units": int, "staff": int, "duties": list, "venues": list},
}
SUBJECT_SCHEMA = {"name": str, "periods": int, "lengths": dict, "band": bool}
DUTY_SCHEMA = {"name": str, "per_cycle": int, "length_slots": int, "min_staff": int, "venue_kind": str}
VENUE_SCHEMA = {"name": str, "kind": str, "capacity": int}

_cache: dict[str, dict] | None = None


class WizardError(ValueError):
    """A template file the library refuses to ship, or a knob value the wizard will not accept."""


# ---------------------------------------------------------------------------
# validation
# ---------------------------------------------------------------------------

def _is_int(value) -> bool:
    """`True` is an `int` in Python; a count that arrives as a boolean is a mistake, not a 1."""
    return isinstance(value, int) and not isinstance(value, bool)


def _same(value, option) -> bool:
    """Membership that does not let `True` match the option `1`."""
    return value == option and isinstance(value, bool) == isinstance(option, bool)


def _field(obj: dict, key: str, kind: type, where: str):
    if not isinstance(obj, dict):
        raise WizardError(f"{where}: must be an object, not {type(obj).__name__}")
    if key not in obj:
        raise WizardError(f"{where}: missing {key!r}")
    value = obj[key]
    if kind is int:
        if not _is_int(value):
            raise WizardError(f"{where}: {key!r} must be a whole number, not {value!r}")
    elif kind is bool:
        if not isinstance(value, bool):
            raise WizardError(f"{where}: {key!r} must be true or false, not {value!r}")
    elif not isinstance(value, kind):
        raise WizardError(f"{where}: {key!r} must be {kind.__name__}, not {type(value).__name__}")
    return value


def _strings(obj: dict, key: str, where: str, *, at_least: int = 1, at_most: int | None = None) -> list:
    items = _field(obj, key, list, where)
    if not all(isinstance(s, str) and s.strip() for s in items):
        raise WizardError(f"{where}: every {key} entry must be a non-empty string")
    if len(items) < at_least:
        raise WizardError(f"{where}: needs at least {at_least} {key}")
    if at_most is not None and len(items) > at_most:
        raise WizardError(f"{where}: at most {at_most} {key}, got {len(items)}")
    return items


def _knob_range(knob: dict, where: str) -> None:
    """A knob offers either a range (`min`/`max`) or a list of `options`, never neither."""
    kind = knob["kind"]
    if kind == "int":
        low, high = _field(knob, "min", int, where), _field(knob, "max", int, where)
        if low > high:
            raise WizardError(f"{where}: min {low} is above max {high}")
    elif kind == "choice":
        options = _field(knob, "options", list, where)
        if len(options) < 2:
            raise WizardError(f"{where}: a choice needs at least two options")
    elif kind == "bool":
        if [o for o in _field(knob, "options", list, where) if isinstance(o, bool)] != [False, True]:
            raise WizardError(f"{where}: a yes/no knob's options must be [false, true]")


def _validate_knobs(template: dict, where: str) -> None:
    knobs = _field(template, "knobs", list, where)
    if not knobs:
        raise WizardError(f"{where}: needs at least one knob")
    seen = set()
    for knob in knobs:
        for key, kind in KNOB_SCHEMA.items():
            _field(knob, key, kind, f"{where}: knob")
        spot = f"{where}: knob {knob['key']!r}"
        if knob["key"] in seen:
            raise WizardError(f"{spot}: appears twice")
        seen.add(knob["key"])
        if knob["kind"] not in KNOB_KINDS:
            raise WizardError(f"{spot}: kind {knob['kind']!r} must be one of {KNOB_KINDS}")
        _knob_range(knob, spot)
        if "default" not in knob:
            raise WizardError(f"{spot}: missing 'default'")
        _check_value(knob, knob["default"], spot, field="default")
    if "cycle_days" not in seen:
        raise WizardError(f"{where}: every template needs a 'cycle_days' knob")
    cycle = next(k for k in knobs if k["key"] == "cycle_days")
    if not _is_int(cycle["default"]):
        raise WizardError(f"{where}: knob 'cycle_days': default must be a whole number of days")


def _validate_time_and_rules(template: dict, where: str) -> None:
    time = _field(template, "time", dict, where)
    for key, kind in TIME_SCHEMA.items():
        _field(time, key, kind, f"{where}: time")
    if time["labels"] not in LABEL_KINDS:
        raise WizardError(f"{where}: time.labels {time['labels']!r} must be one of {LABEL_KINDS}")
    if time["days_per_week"] not in (5, 7):
        raise WizardError(f"{where}: time.days_per_week must be 5 or 7, not {time['days_per_week']}")
    if time["slot_minutes"] < 1 or time["slots_per_day"] < 1:
        raise WizardError(f"{where}: time.slot_minutes and time.slots_per_day must be positive")
    if not (len(time["day_start"]) == 5 and time["day_start"][2] == ":"
            and time["day_start"].replace(":", "").isdigit()):
        raise WizardError(f"{where}: time.day_start {time['day_start']!r} must read like '07:30'")

    rules = _field(template, "rules", dict, where)
    for key, kind in RULES_SCHEMA.items():
        _field(rules, key, kind, f"{where}: rules")
    for offset in rules["mandatory_rest"]:
        if not _is_int(offset) or not 0 <= offset < time["slots_per_day"]:
            raise WizardError(f"{where}: rules.mandatory_rest offset {offset!r} is outside the day")
    for key in ("max_load_slots", "max_run_slots"):
        if not 1 <= rules[key] <= time["slots_per_day"]:
            raise WizardError(f"{where}: rules.{key} must be between 1 and the {time['slots_per_day']} "
                              f"slots of a day")


def _validate_sample(template: dict, where: str) -> None:
    """The sample must be small enough that task 2 can generate and build it in well under a
    second, and every duty must be placeable: a shift longer than `max_run_slots` never is."""
    sheets = template["sheets"]
    sample = _field(template, "sample", dict, where)
    for key, kind in SAMPLE_SCHEMA[sheets].items():
        _field(sample, key, kind, f"{where}: sample")
    run = template["rules"]["max_run_slots"]

    if sheets == "deployment":
        if sample["levels"] * sample["classes_per_level"] > 8:
            raise WizardError(f"{where}: sample has more than 8 classes; keep it quick to build")
        if not 1 <= len(sample["subjects"]) <= 6:
            raise WizardError(f"{where}: sample needs 1-6 subjects, got {len(sample['subjects'])}")
        for subject in sample["subjects"]:
            spot = f"{where}: sample subject"
            for key, kind in SUBJECT_SCHEMA.items():
                _field(subject, key, kind, spot)
            spot = f"{where}: sample subject {subject['name']!r}"
            total = 0
            for length, count in subject["lengths"].items():
                if not (isinstance(length, str) and length.isdigit() and 1 <= int(length) <= 4):
                    raise WizardError(f"{spot}: lesson length {length!r} must be '1'-'4'")
                if not _is_int(count) or count < 0:
                    raise WizardError(f"{spot}: lesson count {count!r} must not be negative")
                if int(length) > run:
                    raise WizardError(f"{spot}: a lesson of {length} periods runs past max_run_slots {run}")
                total += int(length) * count
            if total != subject["periods"]:
                raise WizardError(f"{spot}: lengths add up to {total} periods, not {subject['periods']}")
    else:
        if sample["units"] > 4:
            raise WizardError(f"{where}: sample has more than 4 units; keep it quick to build")
        if not 1 <= len(sample["duties"]) <= 6:
            raise WizardError(f"{where}: sample needs 1-6 duties, got {len(sample['duties'])}")
        kinds = set()
        for venue in _field(sample, "venues", list, where):
            for key, kind in VENUE_SCHEMA.items():
                _field(venue, key, kind, f"{where}: sample venue")
            kinds.add(venue["kind"])
        for duty in sample["duties"]:
            for key, kind in DUTY_SCHEMA.items():
                _field(duty, key, kind, f"{where}: sample duty")
            spot = f"{where}: sample duty {duty['name']!r}"
            if duty["length_slots"] > run:
                raise WizardError(f"{spot}: it is {duty['length_slots']} slots long, past max_run_slots {run}")
            if duty["min_staff"] < 1 or duty["per_cycle"] < 1:
                raise WizardError(f"{spot}: per_cycle and min_staff must be at least 1")
            if duty["venue_kind"] not in kinds:
                raise WizardError(f"{spot}: no sample venue of kind {duty['venue_kind']!r}")

    people = sample["teachers"] if sheets == "deployment" else sample["staff"]
    if not 1 <= people <= 8:
        raise WizardError(f"{where}: sample needs 1-8 people, got {people}")


def validate_template(template: dict) -> dict:
    """Raise `WizardError` naming the first fault, or return the template unchanged."""
    if not isinstance(template, dict):
        raise WizardError(f"template: must be an object, not {type(template).__name__}")
    where = f"template {template.get('id')!r}" if isinstance(template.get("id"), str) else "template"
    for key, kind in TEMPLATE_SCHEMA.items():
        _field(template, key, kind, where)
    if template["domain"] not in dict(DOMAINS):
        raise WizardError(f"{where}: domain {template['domain']!r} must be one of {[d for d, _ in DOMAINS]}")
    if template["sheets"] not in SHEET_KINDS:
        raise WizardError(f"{where}: sheets {template['sheets']!r} must be one of {SHEET_KINDS}")
    vocabulary = _field(template, "vocabulary", dict, where)
    if set(vocabulary) != set(VOCABULARY_KEYS):
        raise WizardError(f"{where}: vocabulary must name exactly {VOCABULARY_KEYS}")
    for key in VOCABULARY_KEYS:
        _field(vocabulary, key, str, f"{where}: vocabulary")
    for key in ("name", "summary", "when_to_choose"):
        if not template[key].strip():
            raise WizardError(f"{where}: {key} must not be empty")
    _strings(template, "questions", where, at_least=1, at_most=MAX_QUESTIONS)
    _strings(template, "tradeoffs", where, at_least=1)
    _validate_knobs(template, where)
    _validate_time_and_rules(template, where)
    _validate_sample(template, where)
    return template


# ---------------------------------------------------------------------------
# loading
# ---------------------------------------------------------------------------

def _load() -> dict[str, dict]:
    global _cache
    if _cache is None:
        loaded = {}
        for template_id in TEMPLATE_IDS:
            path = LIBRARY_DIR / f"{template_id}.json"
            try:
                template = json.loads(path.read_text(encoding="utf-8"))
            except FileNotFoundError:
                raise WizardError(f"template {template_id!r}: {path.name} is not in the library")
            except json.JSONDecodeError as e:
                raise WizardError(f"template {template_id!r}: {path.name} is not valid JSON: {e}")
            validate_template(template)
            if template["id"] != template_id:
                raise WizardError(f"template {template_id!r}: the file says its id is {template['id']!r}")
            loaded[template_id] = template
        _cache = loaded
    return _cache


def load_all() -> dict[str, dict]:
    """Every template by id, in the order of the launch table. Cached; the caller gets a copy
    it may edit (the wizard fills knobs into a template before rendering from it)."""
    return copy.deepcopy(_load())


def get(template_id: str) -> dict:
    """One template by id. `KeyError` when the id is unknown — an id the model invented."""
    return copy.deepcopy(_load()[template_id])


def domains() -> list[dict]:
    """The domains in the wizard's order, each with the cards of its templates."""
    templates = _load()
    return [{"id": domain, "name": name,
             "templates": [{field: t[field] for field in CARD_FIELDS}
                           for t in templates.values() if t["domain"] == domain]}
            for domain, name in DOMAINS]


# ---------------------------------------------------------------------------
# knobs
# ---------------------------------------------------------------------------

def _check_value(knob: dict, value, where: str, field: str = "value"):
    kind = knob["kind"]
    if kind == "int":
        if not _is_int(value):
            raise WizardError(f"{where}: {field} {value!r} must be a whole number")
        if not knob["min"] <= value <= knob["max"]:
            raise WizardError(f"{where}: {field} {value} is outside {knob['min']}-{knob['max']}")
    elif kind == "bool":
        if not isinstance(value, bool):
            raise WizardError(f"{where}: {field} {value!r} must be true or false")
    else:
        if not any(_same(value, option) for option in knob["options"]):
            raise WizardError(f"{where}: {field} {value!r} is not one of {knob['options']}")
    return value


def check_knobs(template: dict, knobs: dict) -> dict:
    """Return every knob of the template, the caller's values where it gave one and the
    template's default everywhere else. Raises `WizardError` naming the first bad knob."""
    if not isinstance(knobs, dict):
        raise WizardError(f"knobs must be an object, not {type(knobs).__name__}")
    spec = {k["key"]: k for k in template["knobs"]}
    name = template.get("name") or template.get("id") or "this template"
    for key in knobs:
        if key not in spec:
            raise WizardError(f"{key!r} is not a setting of {name}; it takes {sorted(spec)}")
    return {key: _check_value(knob, knobs[key], f"{name}: {knob['label']}")
            if key in knobs else knob["default"]
            for key, knob in spec.items()}
