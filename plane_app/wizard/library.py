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

from ..db import PRESETS, RULES as SOLVE_RULES

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
# Optional keys a template saved from a timetable carries (learning spec §1.3): the solver's preset and
# weights, and the plan's own rules, so rules learned on one timetable travel with the template.
EDGE_SUBJECTS_MAX, EDGE_SUBJECT_CHARS = 30, 60

# A school's own templates (learning spec §1.3): global kv `{id: template}`, listed after the built-in
# ones in their domain. Their ids always start with LOCAL_PREFIX, so one never shadows a built-in id.
LOCAL_KEY = "local_templates"
LOCAL_PREFIX = "local-"
# Other schools' approved templates from the engine library (learning spec §2.2), listed after the school's
# own: `shared-<item id>`, which no built-in or local id can equal.
SHARED_PREFIX = "shared-"
SHARED_ERROR = "The shared library could not be reached."

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


def _validate_optional(template: dict, where: str) -> None:
    if "solve" in template:
        solve = template["solve"]
        spot = f"{where}: solve"
        if not isinstance(solve, dict):
            raise WizardError(f"{spot}: must be an object")
        if solve.get("preset") not in (*PRESETS, "custom"):
            raise WizardError(f"{spot}: preset {solve.get('preset')!r} must be one of {[*PRESETS, 'custom']}")
        weights = solve.get("weights")
        if not isinstance(weights, dict) or set(weights) != set(SOLVE_RULES):
            raise WizardError(f"{spot}: weights must name exactly {SOLVE_RULES}")
        for rule, value in weights.items():
            if not _is_int(value) or value < 0:
                raise WizardError(f"{spot}: weight {rule!r} must be a whole number of at least 0, not {value!r}")
    if "plan_rules" in template:
        rules = template["plan_rules"]
        spot = f"{where}: plan_rules"
        if not isinstance(rules, dict):
            raise WizardError(f"{spot}: must be an object")
        edge = rules.get("edge_subjects")
        if not isinstance(edge, list) or len(edge) > EDGE_SUBJECTS_MAX:
            raise WizardError(f"{spot}: edge_subjects must be a list of at most {EDGE_SUBJECTS_MAX} subjects")
        if not all(isinstance(x, str) and x.strip() and len(x) <= EDGE_SUBJECT_CHARS for x in edge):
            raise WizardError(f"{spot}: every edge subject must be a name of 1-{EDGE_SUBJECT_CHARS} characters")
        if not isinstance(rules.get("no_double_across_rest"), bool):
            raise WizardError(f"{spot}: no_double_across_rest must be true or false")


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
    _validate_optional(template, where)
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


def local(db) -> dict[str, dict]:
    """The school's own templates that still pass validation, in the order they were saved. An entry
    that does not (a hand-edited row, a template from an older schema) is skipped, not an error."""
    stored = db.get_value(LOCAL_KEY) if db is not None else None
    out = {}
    for template_id, template in (stored.items() if isinstance(stored, dict) else ()):
        if not (isinstance(template_id, str) and template_id.startswith(LOCAL_PREFIX)
                and isinstance(template, dict) and template.get("id") == template_id):
            continue
        try:
            validate_template(template)
        except WizardError:
            continue
        out[template_id] = template
    return out


def _shared(db, engine, errors: list | None = None) -> dict[str, dict]:
    """Other schools' approved templates (learning spec §2.2), when there is an engine to ask. The engine
    being unreachable is not an error here: the wizard lists the rest and `errors` gets SHARED_ERROR."""
    if db is None or engine is None:
        return {}
    from ..learning import sharing                       # not at import time: sharing imports this module
    try:
        return {t["id"]: t for t in sharing.shared_templates(db, engine)}
    except Exception:       # noqa: BLE001 - an engine error or an unreadable answer: list without them
        if errors is not None:
            errors.append(SHARED_ERROR)
        return {}


def _all(db=None, engine=None, errors: list | None = None) -> dict[str, dict]:
    """The built-in templates, then (given a db) the school's own, then (given an engine too) other schools'
    shared ones. The built-in cache is never written to: the others are read fresh each time (the shared ones
    through `sharing`'s short cache)."""
    templates = _load()
    if db is None:
        return templates
    return {**templates, **local(db), **_shared(db, engine, errors)}


def load_all(db=None, engine=None) -> dict[str, dict]:
    """Every template by id, in the order of the launch table, then the school's own when a db is
    given, then shared ones when an engine is given too. Cached; the caller gets a copy it may edit (the
    wizard fills knobs into a template before rendering from it)."""
    return copy.deepcopy(_all(db, engine))


def get(template_id: str, db=None, engine=None) -> dict:
    """One template by id — a built-in one, with a db one of the school's own, with an engine as well a
    shared one (`shared-…`; only those ask the engine). `KeyError` when the id is unknown — an id the model
    invented, or a shared template the engine no longer offers or cannot be asked about."""
    if template_id.startswith(SHARED_PREFIX):
        return copy.deepcopy(_shared(db, engine)[template_id])
    return copy.deepcopy(_all(db)[template_id])


def card(template: dict) -> dict:
    """What a candidate card shows; a school's own template is marked `local`, another school's shared one
    `shared: {schools, kept}` (how many schools used it and how many kept it)."""
    out = {field: template[field] for field in CARD_FIELDS}
    if template["id"].startswith(LOCAL_PREFIX):
        out["local"] = True
    elif template["id"].startswith(SHARED_PREFIX):
        record = template.get("shared") if isinstance(template.get("shared"), dict) else {}
        out["shared"] = {"schools": record.get("schools", 0), "kept": record.get("kept", 0)}
    return out


def _domains(templates: dict) -> list[dict]:
    return [{"id": domain, "name": name,
             "templates": [card(t) for t in templates.values() if t["domain"] == domain]}
            for domain, name in DOMAINS]


def domains(db=None, engine=None) -> list[dict]:
    """The domains in the wizard's order, each with the cards of its templates: the built-in ones,
    then (given a db) the school's own, then (given an engine too) other schools' shared ones."""
    return _domains(_all(db, engine))


def listing(db=None, engine=None) -> dict:
    """`{"domains": domains(db, engine)}`, plus `shared_error` when the shared library could not be reached."""
    errors: list = []
    out = {"domains": _domains(_all(db, engine, errors))}
    if errors:
        out["shared_error"] = errors[0]
    return out


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
