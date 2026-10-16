"""The document reader (spec docs/superpowers/specs/2026-10-12-assistant-files-design.md §2.2–2.3): a Word
or PDF document's text, read by one structured call into the smart reader's records (so patch.to_patch
makes the plan patch), the plan rules it can hold, notes and doubts; or, for a timetable, today's
organisation extraction. The reply is data: validated here, never evaluated. Nothing is stored."""
from __future__ import annotations

from ... import week
from ...intake import IntakeError, extract_organisation
from .. import model as M
from .language import LIST_FIELDS, NUMBER_FIELDS, REQUIRED, TARGETS
from .patch import to_patch

TEXT_MAX = 240_000
LIST_MAX = 2000
NOTES_MAX = 50
DOUBTS_MAX = 30
NUMBER_MAX = 10_000                    # a class, room or week of lessons is never bigger than this


class DocumentError(ValueError):
    """The document could not be read into a reading (the model's second reply was unusable)."""


def _str(v) -> str:
    return " ".join(str(v).split()) if isinstance(v, (str, int, float)) and not isinstance(v, bool) else ""


def _int(v) -> int | None:
    return v if isinstance(v, int) and not isinstance(v, bool) else None


def _grid(settings: dict) -> tuple[int, int]:
    """(days, slots a day) of the school's cycle: the labels run `days` of `slots` periods each. A saved week
    gives its days and its longest day's periods (shorter days are named in the prompt)."""
    t = (settings or {}).get("time") or {}
    s = week.shape(t, (settings or {}).get("rules"))
    if not s["legacy"] and s["n_days"] and max(s["open_per_day"]):
        return s["n_days"], max(s["open_per_day"])
    slots = _int(t.get("slots_per_day")) or 8
    labels = t.get("labels")
    days = len(labels) // slots if isinstance(labels, list) else 0
    return days or _int(t.get("days_per_week")) or 5, slots


def _schema() -> dict:
    def obj(props):
        return {"type": "object", "properties": props, "required": list(props), "additionalProperties": False}
    def field(f):
        if f in NUMBER_FIELDS:
            return {"type": "integer"}
        if f in LIST_FIELDS:
            return {"type": "array", "items": {"type": "string"}}
        return {"type": "string"}
    records = obj({t: {"type": "array", "items": obj({f: field(f) for f in fields})} for t, fields in TARGETS.items()})
    pin = obj({"name": {"type": "string"}, "day": {"type": "integer"}, "slot": {"type": "integer"},
               "dur": {"type": "integer"}, "everyone": {"type": "boolean"}})
    return obj({"kind": {"type": "string", "enum": ["plan", "timetable"]}, "summary": {"type": "string"},
                "records": records,
                "rules": obj({"edge_subjects": {"type": "array", "items": {"type": "string"}},
                              "pinned": {"type": "array", "items": pin}}),
                "notes": {"type": "array", "items": {"type": "string"}},
                "doubts": {"type": "array", "items": {"type": "string"}}})


def _prompt(settings: dict) -> str:
    t = settings.get("time") or {}
    days, slots = _grid(settings)
    labels = (t.get("labels") or [])[:slots] if isinstance(t.get("labels"), list) else []
    s = week.shape(t, settings.get("rules"))
    if not s["legacy"]:                          # periods only: a break is not a period, and a padded day has closed ends
        longest = s["open_per_day"].index(max(s["open_per_day"])) if s["open_per_day"] else 0
        labels = [p["label"] for p in s["positions"] if p["day"] == longest and p["kind"] == "period"]
    short = "; ".join(week.describe(s)) if not s["legacy"] and len(set(s["open_per_day"])) > 1 else ""
    return (
        "You read a school's planning document for a timetabling app. Say kind 'timetable' when the document "
        "IS a timetable (a grid of lessons by day and period); otherwise 'plan'. For a plan, list what it states, "
        "as written: rooms (venues), classes, home rooms (homes), staff with their department, and requirements "
        "(a subject for classes: periods a week, teachers by name). Use empty strings, 0 and [] for what a "
        "record does not say. Rules: only subjects to keep to the start or end of the day (edge_subjects) and "
        "fixed whole-school or named events at a fixed day and period (pinned: day from 0 = the first school "
        f"day, slot from 0 = the first period; the school has {days} days and "
        f"{slots} periods a day, periods labelled {', '.join(labels) or 'P1, P2, ...'}"
        + (f"; the days are not all alike: {short}" if short else "") + "). "
        "Every other preference or constraint goes in notes, one short sentence each. Put anything you are "
        "unsure of (a name that may be someone else, a class you cannot place) in doubts. Do not invent data.")


def _cut(text: str, limit: int) -> tuple[str, str | None]:
    if len(text) <= limit:
        return text, None
    head = text[:limit]
    at = max(head.rfind("\n## Page "), head.rfind("\n\n"))
    head = head[:at] if at > limit // 2 else head
    rest = text[len(head):].lstrip("\n")
    first = rest.split("\n", 1)[0].strip()
    if first.startswith("## "):
        return head, f"Only the start of the document was read: from {first[3:]} on it was too long."
    later = next((line[3:].strip() for line in rest.split("\n") if line.startswith("## ")), None)
    where = f"; the cut falls before {later}" if later else ""
    return head, f"Only the start of the document was read: it was too long and was cut part-way{where}."


def _records(raw) -> tuple[dict, list[str]]:
    doubts: list[str] = []
    out: dict[str, list[dict]] = {}
    raw = raw if isinstance(raw, dict) else {}
    for target, fields in TARGETS.items():
        rows = raw.get(target)
        if rows is None:
            continue
        if not isinstance(rows, list):
            doubts.append(f"The {target} read could not be used.")
            continue
        kept = []
        for row in rows[:LIST_MAX]:
            if not isinstance(row, dict):
                continue
            rec = {}
            for f in fields:
                v = row.get(f)
                if f in NUMBER_FIELDS:
                    n = _int(v)
                    if n is not None and 0 < n <= NUMBER_MAX:
                        rec[f] = n
                    elif n is not None and n != 0:
                        doubts.append(f"A {target} entry's {f} ({_str(v)[:20]}) is not a sensible number; it was left blank.")
                elif f in LIST_FIELDS:
                    items = [_str(x) for x in v] if isinstance(v, list) else []
                    if any(items):
                        rec[f] = [x for x in items if x]
                elif _str(v):
                    rec[f] = _str(v)
            if all(rec.get(f) for f in REQUIRED[target]):
                rec["at"] = "the document"
                kept.append(rec)
            else:
                doubts.append(f"A {target} entry without its {' and '.join(REQUIRED[target])} was left out.")
        if kept:
            out[target] = kept
    return out, doubts


def _rules(raw, settings: dict) -> tuple[dict, list[str]]:
    days, slots = _grid(settings)
    shape = week.shape((settings or {}).get("time"), (settings or {}).get("rules"))
    opened = shape["open_per_day"] if not shape["legacy"] else None
    periods = [[p["index"] for p in shape["positions"] if p["day"] == k and p["kind"] == "period"] for k in range(shape["n_days"])]
    raw = raw if isinstance(raw, dict) else {}
    edge = [_str(s) for s in raw.get("edge_subjects") or [] if _str(s)] if isinstance(raw.get("edge_subjects"), list) else []
    pinned, doubts = [], []
    for p in raw.get("pinned") or [] if isinstance(raw.get("pinned"), list) else []:
        if not isinstance(p, dict) or not _str(p.get("name")):
            continue
        day, slot = _int(p.get("day")), _int(p.get("slot"))
        if day is None or slot is None or not (0 <= day < days and 0 <= slot < (opened[day] if opened else slots)):
            doubts.append(f"{_str(p.get('name'))}: its day or period is not one this timetable has; left out.")
            continue
        if opened:                               # a period number to the position it sits at (breaks count as positions)
            slot = periods[day][slot]
        dur = _int(p.get("dur"))
        dur = dur if dur is not None and 1 <= dur <= slots else 1
        pinned.append({"name": _str(p["name"]), "day": day, "slot": slot, "dur": dur, "everyone": bool(p.get("everyone"))})
    rules = {}
    if edge:
        rules["edge_subjects"] = edge
    if pinned:
        rules["pinned"] = pinned
    return rules, doubts


def read_document(provider, name: str, text: str, plan: dict | None, settings: dict) -> dict:
    header = f"# Document: {name}\n"
    sent, cut = _cut(text, max(TEXT_MAX - len(header), 0))        # the whole message stays within TEXT_MAX
    reply = provider.extract_json(_prompt(settings), header + sent, _schema())
    reply = reply if isinstance(reply, dict) else {}
    doubts = [cut] if cut else []
    summary = _str(reply.get("summary"))[:300] or name
    if reply.get("kind") == "timetable":
        try:
            org, notes = extract_organisation(provider, settings, [(name, sent)])      # what was read, not the uncut text
        except IntakeError as e:
            raise DocumentError(f"The timetable in {name} could not be read: {e}")
        return {"kind": "timetable", "summary": summary, "organisation": org,
                "doubts": (doubts + [_str(n.get("note")) for n in notes if isinstance(n, dict) and _str(n.get("note"))])[:DOUBTS_MAX]}
    records, d1 = _records(reply.get("records"))
    rules, d2 = _rules(reply.get("rules"), settings)
    notes = [_str(n)[:M.NOTE_MAX] for n in reply.get("notes") or [] if _str(n)] if isinstance(reply.get("notes"), list) else []
    said = [_str(d)[:300] for d in reply.get("doubts") or [] if _str(d)] if isinstance(reply.get("doubts"), list) else []
    patch, d3 = to_patch(records, plan)
    try:
        M.apply_patch(plan or M.empty_plan(), patch)
    except M.PlanError as e:
        d3 = [f"Together, what was read does not fit the plan: {e}"] + d3
        patch = {}
    return {"kind": "plan", "summary": summary, "records": records, "patch": patch, "rules": rules,
            "notes": notes[:NOTES_MAX], "doubts": (doubts + said + d1 + d2 + d3)[:DOUBTS_MAX]}
