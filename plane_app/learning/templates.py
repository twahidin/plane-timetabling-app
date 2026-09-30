"""Save the current timetable as a start-wizard template (spec docs/superpowers/specs/2026-09-30-learning-design.md
§1.3): time and rules from the settings, the vocabulary and a small sample in the plan's shape, the solver's
preset and weights and the plan's own rules, so what was learned on one timetable starts the next.

What leaves the timetable is shape, never identity: counts of levels, classes and people, the plan's subject
names with their periods and lesson lengths, and the words it uses. No person's name, short name or id, no
class code, requirement id, reduction reason, source file or sign-off name is copied; the wizard invents its
own placeholders when it renders the sample. The timetable's name (often the school's) is not copied either:
the trade-off line says only that it was made from a finished timetable, and when.

Saved templates are the school's, not one timetable's: global kv `local_templates` (`{id: template}`)."""
from __future__ import annotations

import re
import secrets
import threading
import time as _time
from collections import Counter

from ..db import DEFAULT_SOFT, PRESETS, RULES
from ..plan import model as plan_model
from ..wizard import library as L
from ..wizard.library import WizardError

NAME_CHARS, TEXT_CHARS = 80, 300      # the dialog's fields
SLUG_CHARS = 24
MAX_LOCAL = 50                         # saved templates a school keeps
MAX_LEVELS, MAX_CLASSES, MAX_SUBJECTS, MAX_PEOPLE = 4, 8, 6, 8      # the library's sample limits
MAX_UNITS, MAX_VENUES, MAX_DUTIES, MAX_PER_CYCLE = 4, 4, 6, 5
MAX_LESSON = 4
WEEKDAYS = {"mon", "tue", "wed", "thu", "fri", "sat", "sun"}
_CLOCK = re.compile(r"(\d{1,2}):(\d{2})")
_lock = threading.Lock()               # save and delete are a read-modify-write of one kv row

QUESTIONS = ["How many days does your timetable run before it repeats?"]


# ---- the dialog's answers -------------------------------------------------------------------------------------

def _meta(meta) -> dict:
    if not isinstance(meta, dict):
        raise WizardError("the template needs a name, a line about it, when to choose it and its kind")
    out = {}
    for key, label, most in (("name", "a name", NAME_CHARS), ("summary", "one line about it", TEXT_CHARS),
                             ("when_to_choose", "when to choose it", TEXT_CHARS)):
        value = meta.get(key)
        if not isinstance(value, str) or not value.strip():
            raise WizardError(f"give the template {label}")
        if len(value.strip()) > most:
            raise WizardError(f"{label[0].upper()}{label[1:]} must be at most {most} characters")
        out[key] = value.strip()
    domain = meta.get("domain")
    if domain not in dict(L.DOMAINS):
        raise WizardError(f"kind {domain!r} must be one of {[d for d, _ in L.DOMAINS]}")
    out["domain"] = domain
    return out


def _template_id(name: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")[:SLUG_CHARS].strip("-") or "template"
    return f"{L.LOCAL_PREFIX}{slug}-{secrets.token_hex(2)}"


# ---- time and rules, from the settings ------------------------------------------------------------------------

def _int(value, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _label_kind(labels: list) -> str:
    """"day-hour" for labels that end in a clock time, "day-period" for ones that name a weekday, else
    "period" — the three ways the wizard writes a cycle's labels."""
    labels = [str(x) for x in labels or []]
    if any(_CLOCK.search(x) for x in labels):
        return "day-hour"
    if any(WEEKDAYS & {w.lower() for w in x.split()} for x in labels):
        return "day-period"
    return "period"


def _cycle(settings: dict) -> tuple[dict, int]:
    """The template's `time` and the number of days in the cycle."""
    t = settings["time"]
    slots_per_day = max(1, _int(t.get("slots_per_day"), 8))
    match = _CLOCK.fullmatch(str(t.get("start") or ""))
    hours, minutes = (int(match.group(1)), int(match.group(2))) if match else (8, 0)
    start = f"{hours:02d}:{minutes:02d}" if hours < 24 and minutes < 60 else "08:00"
    days = max(1, len(t.get("labels") or []) // slots_per_day)
    return {"slot_minutes": max(1, _int(t.get("slot_minutes"), 60)), "day_start": start,
            "slots_per_day": slots_per_day, "labels": _label_kind(t.get("labels")),
            "days_per_week": 5 if _int(t.get("days_per_week"), 5) <= 5 else 7}, days


def _rules(settings: dict, slots_per_day: int) -> dict:
    r = settings.get("rules") or {}
    clamp = lambda v: min(max(_int(v, slots_per_day), 1), slots_per_day)
    rest = sorted({_int(x, -1) for x in r.get("mandatory_rest") or []} & set(range(slots_per_day)))
    return {"max_load_slots": clamp(r.get("max_load")), "max_run_slots": clamp(r.get("max_run")), "mandatory_rest": rest}


def _solve(settings: dict) -> dict:
    s = settings.get("solve") or {}
    preset = s.get("preset") if s.get("preset") in (*PRESETS, "custom") else "balanced"
    weights = s.get("weights") if isinstance(s.get("weights"), dict) else {}
    return {"preset": preset, "weights": {r: max(0, _int(weights.get(r), DEFAULT_SOFT[r])) for r in RULES}}


# ---- the sample, from the plan (deployment) or the live organisation (generic) --------------------------------

def _lessons(req: dict) -> dict[int, int]:
    return {int(k): int(v) for k, v in req["lessons"].items() if int(v)}


def _clip(pattern: dict[int, int], longest: int) -> dict:
    """Lesson lengths no longer than `longest`: a longer lesson becomes that many-period lessons and the rest,
    so the periods still add up."""
    out: Counter = Counter()
    for length, count in pattern.items():
        whole, rest = divmod(length, longest)
        out[longest] += whole * count
        if rest:
            out[rest] += count
    return {str(k): out[k] for k in sorted(out) if out[k]}


def _subjects(plan: dict, max_run: int) -> list[dict]:
    """The <= 6 subjects with the most periods across the plan, each with the most common periods a class and,
    among the requirements with that many, the most common lesson lengths."""
    banded = {opt for b in plan["bands"] for opt in b["options"]}
    reqs: dict[str, list] = {}
    in_band: set[str] = set()
    for r in plan["requirements"]:
        name = str(r.get("subject") or "").strip()
        lessons = _lessons(r)
        if not name or len(name) > L.EDGE_SUBJECT_CHARS or not lessons:
            continue
        reqs.setdefault(name, []).append(lessons)
        if r["id"] in banded:
            in_band.add(name)
    total = {name: sum(k * v for ls in items for k, v in ls.items()) for name, items in reqs.items()}
    out = []
    for name in sorted(reqs, key=lambda n: (-total[n], n))[:MAX_SUBJECTS]:
        periods = Counter(sum(k * v for k, v in ls.items()) for ls in reqs[name])
        most = max(periods, key=lambda p: (periods[p], p))
        patterns = Counter(tuple(sorted(ls.items())) for ls in reqs[name] if sum(k * v for k, v in ls.items()) == most)
        pattern = max(patterns, key=lambda p: (patterns[p], p))
        out.append({"name": name, "periods": most, "lengths": _clip(dict(pattern), min(MAX_LESSON, max_run)),
                    "band": name in in_band})
    return out


def _deployment_sample(plan: dict, subjects: list[dict]) -> dict:
    per_level = Counter(str(c.get("level") or "") for c in plan["classes"])
    top = sorted(per_level.items(), key=lambda kv: (-kv[1], kv[0]))[:MAX_LEVELS]
    levels = max(1, len(top))
    average = sum(n for _, n in top) / len(top) if top else 1
    return {"levels": levels, "classes_per_level": max(1, min(int(average + 0.5), MAX_CLASSES // levels)),
            "teachers": min(max(1, len(plan["staff"])), MAX_PEOPLE), "subjects": subjects}


def _generic_sample(org: dict, vocabulary: dict, max_run: int) -> dict:
    """Counts only: how many people, units and places the live organisation has, and its events grouped by
    length. Every name is invented from the timetable's own words ("Shift 1", "Ward 2")."""
    persons = {p.get("id") for p in org.get("persons") or [] if isinstance(p, dict)}
    staff = min(max(1, len(persons)), MAX_PEOPLE)
    units = min(max(1, len(org.get("groups") or [])), MAX_UNITS)
    places = [l for l in org.get("locations") or [] if isinstance(l, dict) and not l.get("rest")]
    caps = sorted(c for l in places if (c := _int(l.get("cap"), 0)) > 0)
    kind = vocabulary["venue"]
    venues = [{"name": f"{kind.capitalize()} {i + 1}", "kind": kind, "capacity": caps[len(caps) // 2] if caps else 30}
              for i in range(min(max(1, len(places)), MAX_VENUES))]
    events = [e for e in org.get("events") or [] if isinstance(e, dict) and not e.get("fixed")]
    by_length: dict[int, list[int]] = {}
    for e in events:
        length = min(max(_int(e.get("dur"), 1), 1), max_run)
        by_length.setdefault(length, []).append(len([m for m in e.get("members") or [] if m in persons]))
    word = vocabulary["requirement"].capitalize()
    duties = []
    for i, (length, crews) in enumerate(sorted(by_length.items(), key=lambda kv: (-len(kv[1]), kv[0]))[:MAX_DUTIES]):
        crew = Counter(crews)
        size = max(crew, key=lambda n: (crew[n], n))
        duties.append({"name": f"{word} {i + 1}", "per_cycle": min(len(crews), MAX_PER_CYCLE), "length_slots": length,
                       "min_staff": min(max(1, size), staff), "venue_kind": kind})
    if not duties:
        duties = [{"name": f"{word} 1", "per_cycle": 1, "length_slots": 1, "min_staff": 1, "venue_kind": kind}]
    return {"units": units, "staff": staff, "duties": duties, "venues": venues}


# ---- export -------------------------------------------------------------------------------------------------

def _plan(db) -> dict | None:
    try:
        stored = db.get_value("plan")
        return plan_model.normalise(stored) if stored else None
    except plan_model.PlanError:
        return None


def _day(at: float) -> str:
    t = _time.localtime(at)
    return f"{t.tm_mday} {_time.strftime('%b %Y', t)}"


def export_template(db, meta: dict, now: float | None = None) -> dict:
    """The current timetable as a wizard template that passes `library.validate_template` (else
    `WizardError` naming the fault). Nothing is stored; `save_local` does that."""
    meta = _meta(meta)
    settings = db.get_settings()
    plan = _plan(db)
    time, days = _cycle(settings)
    rules = _rules(settings, time["slots_per_day"])
    vocabulary = dict(plan["vocabulary"]) if plan else dict(plan_model.DEFAULT_VOCABULARY)
    subjects = _subjects(plan, rules["max_run_slots"]) if plan else []
    if subjects:
        sheets, sample = "deployment", _deployment_sample(plan, subjects)
    else:
        org = db.get_org("live") or db.get_org("draft") or {}
        sheets, sample = "generic", _generic_sample(org, vocabulary, rules["max_run_slots"])
    plan_rules = (plan or plan_model.empty_plan())["rules"]
    edge = [s for s in plan_rules.get("edge_subjects") or []
            if isinstance(s, str) and s.strip() and len(s) <= L.EDGE_SUBJECT_CHARS][:L.EDGE_SUBJECTS_MAX]
    template = {
        "id": _template_id(meta["name"]), **meta,
        "vocabulary": vocabulary,
        "knobs": [{"key": "cycle_days", "label": "Days in the timetable cycle",
                   "help": (f"{days} in the timetable this was made from. Five or seven for a pattern that repeats "
                            "every week; ten or fourteen for one that runs over two weeks."),
                   "kind": "int", "default": days, "min": 1, "max": max(28, days)}],
        "questions": list(QUESTIONS),
        "tradeoffs": [f"Made from a finished timetable on {_day(_time.time() if now is None else now)}."],
        "time": time, "rules": rules, "sheets": sheets, "sample": sample,
        "solve": _solve(settings),
        "plan_rules": {"edge_subjects": edge, "no_double_across_rest": bool(plan_rules.get("no_double_across_rest", True))},
    }
    return L.validate_template(template)


# ---- the school's saved templates ---------------------------------------------------------------------------

def _stored(db) -> dict:
    v = db.get_value(L.LOCAL_KEY)
    return dict(v) if isinstance(v, dict) else {}


def local_templates(db) -> list[dict]:
    """Every saved template, oldest first (an invalid one too, so it can still be deleted)."""
    return [t for t in _stored(db).values() if isinstance(t, dict)]


def save_local(db, template: dict) -> dict:
    L.validate_template(template)
    template_id = template["id"]
    if not template_id.startswith(L.LOCAL_PREFIX) or template_id in L.load_all():
        raise WizardError(f"a saved template's id must start with {L.LOCAL_PREFIX!r}, not {template_id!r}")
    with _lock:
        stored = _stored(db)
        if template_id not in stored and len(stored) >= MAX_LOCAL:
            raise WizardError(f"at most {MAX_LOCAL} saved templates; delete one first")
        stored[template_id] = template
        db.set_value(L.LOCAL_KEY, stored)
    return template


def delete_local(db, template_id: str) -> None:
    """`KeyError` when there is no saved template of that id."""
    with _lock:
        stored = _stored(db)
        del stored[template_id]
        db.set_value(L.LOCAL_KEY, stored)
