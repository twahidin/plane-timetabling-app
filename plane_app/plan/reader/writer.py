"""The recipe writer (spec docs/superpowers/specs/2026-10-08-smart-workbook-reader-design.md §3, §5): a
model reads the rendered sheets and writes reading recipes; the app validates and runs them itself.

- `write(provider, sheets, plan_summary)`: one call against SCHEMA (compact, every field required —
  empty when unused — so the API's grammar stays small); every recipe validated, an invalid one
  left out and named in `unsure`.
- `read(provider, data, plan, db)`: a workbook as a reading. A saved reader first (`readers`): the
  one saved for this layout signature (or having it as an alias), then the three most used whose
  recipes' sheets this workbook all has (case and spacing aside); the first that reads records
  without errors is used, with no provider call. A reading that cannot run at all (ReadingError) is raised, never repaired. Else the model writes
  recipes; those that read nothing, fail validation or give a patch the plan refuses go back to the
  model once, together, with their errors and their sheets; what still fails is given up with a
  doubt. Nothing is written: the reading carries the patch for Apply.
- `run`: recipes.run, at most READINGS_AT_ONCE at a time (each reading is a child process).

The model's reply is data: recipes are validated and run by recipes.run, never evaluated."""
from __future__ import annotations

import json
import logging
import re
import threading

from ...llm import ProviderError
from .. import model as M
from . import readers, render, signature
from . import recipes as R
from .execute import DOUBTS_MAX
from .language import KINDS, SEARCHES, TARGETS

_log = logging.getLogger(__name__)

SUMMARY_MAX = 300
UNSURE_MAX = 20
UNSURE_TEXT_MAX = 300
SAMPLES_MAX = 5
USER_MAX = 240_000          # characters of rendered sheets in one call; a sheet past it is named only
CLASSES_SHOWN = 300         # class codes the prompt lists
READINGS_AT_ONCE = 2
BUSY_SECONDS = 120          # how long a reading waits for a turn
REASON_MAX = 300            # a recipe's error, as unsure or a doubt
SAME_SHEET_TRIED = 3        # saved readers tried by their sheets alone, most used first

# ---------------------------------------------------------------------------
# the reply's schema: the structured-outputs dialect, every property required, no unions
# ---------------------------------------------------------------------------

_FIELDS = sorted({f for fields in TARGETS.values() for f in fields})
_S = {"type": "string"}
_I = {"type": "integer"}
_FIELD = {"type": "string", "enum": _FIELDS}


def _obj(properties: dict) -> dict:
    return {"type": "object", "properties": properties, "required": list(properties), "additionalProperties": False}


def _list(items: dict) -> dict:
    return {"type": "array", "items": items}


_RECIPE = _obj({
    "kind": {"type": "string", "enum": list(KINDS)},
    "target": {"type": "string", "enum": list(TARGETS)},
    "sheet": _S,
    "note": _S,
    "header_row": _I,
    "first_row": _I,
    "last_row": _I,
    "columns": _list(_obj({"field": _FIELD, "letters": _list(_S)})),
    "parse": _list(_obj({"field": _FIELD, "pattern": _S})),
    "skip_blank": _S,
    "fill_down": _list(_S),
    "anchor": _S,
    "offset": _list(_I),
    "value": _S,
    "labels": _list(_obj({"field": _FIELD, "pattern": _S, "search": {"type": "string", "enum": list(SEARCHES)}})),
})
SCHEMA = _obj({"summary": _S, "recipes": _list(_RECIPE), "unsure": _list(_S)})

_ROW_KEYS = ("header_row", "first_row", "last_row")


def _empty(key: str, value) -> bool:
    """What the schema's "unused" looks like: "", [], {}, null, and row 0."""
    if value is None or (isinstance(value, (str, list, dict)) and not value):
        return True
    return key in _ROW_KEYS and value == 0 and not isinstance(value, bool)


def _as_map(items: list, value_keys: tuple[str, ...]) -> dict | list:
    """[{field, letters}] (or {field, pattern}) as {field: value}; the list itself when it is not that
    shape, so validation says what is wrong."""
    out: dict = {}
    for item in items:
        if not isinstance(item, dict) or not isinstance(item.get("field"), str):
            return items
        value = next((item[k] for k in value_keys if k in item), None)
        if _empty("", value):
            continue
        out[item["field"]] = value
    return out


def from_reply(raw):
    """A recipe as the schema has the model write it (every field present, empty when unused;
    columns and parse as lists) in the recipe language (recipes.validate): empty fields left out,
    columns and parse as maps. The language form passes through unchanged; anything else is returned
    as it is, for validation to refuse."""
    if not isinstance(raw, dict):
        return raw
    out: dict = {}
    for key, value in raw.items():
        if _empty(key, value):
            continue
        if key == "columns" and isinstance(value, list):
            value = _as_map(value, ("letters", "letter", "column", "columns"))
        elif key == "parse" and isinstance(value, list):
            value = _as_map(value, ("pattern",))
        if _empty(key, value):
            continue
        out[key] = value
    return out


# ---------------------------------------------------------------------------
# the prompt
# ---------------------------------------------------------------------------

def plan_summary(plan: dict | None) -> dict:
    """What the prompt says of the plan a reading goes into: its words, class codes, departments and
    how many staff and rooms it has."""
    plan = plan or {}
    vocabulary = {**M.DEFAULT_VOCABULARY, **{k: v for k, v in (plan.get("vocabulary") or {}).items() if v}}
    staff = [s for s in plan.get("staff") or [] if isinstance(s, dict)]
    return {
        "vocabulary": vocabulary,
        "classes": [c["code"] for c in plan.get("classes") or [] if isinstance(c, dict) and c.get("code")],
        "staff": len(staff),
        "departments": sorted({s["dept"] for s in staff if isinstance(s.get("dept"), str) and s["dept"]}),
        "venues": len(plan.get("venues") or []),
    }


def _example(**fields) -> str:
    """One recipe in the reply's form, every field present: what the model is to write."""
    blank = {"note": "", "header_row": 0, "first_row": 0, "last_row": 0, "columns": [], "parse": [],
             "skip_blank": "", "fill_down": [], "anchor": "", "offset": [], "value": "", "labels": []}
    recipe = {"kind": fields.pop("kind"), "target": fields.pop("target"), "sheet": fields.pop("sheet")}
    recipe.update({**blank, **fields})
    return json.dumps(recipe, ensure_ascii=False)


_EXAMPLE_ROOMS = _example(
    kind="pairs", target="venues", sheet="Room Map",
    anchor=r"^(?P<name>[A-Z]\d-\d+)$",
    labels=[{"field": "block", "pattern": r"^TOWER\s+(?P<block>[A-Z])$", "search": "up"},
            {"field": "level", "pattern": r"^LEVEL\s+(?P<level>\d+)$", "search": "left"}])
_EXAMPLE_HOMES = _example(
    kind="pairs", target="homes", sheet="Room Map",
    anchor=r"^(?P<room>[A-Z]\d-\d+)$", offset=[1, 0],
    value=r"^(?P<class>\d[A-Z]+\d?)\s*(?:\((?P<size>\d+)\))?$")
_EXAMPLE_STAFF = _example(
    kind="table", target="staff", sheet="Teachers", note="column A is a running number",
    header_row=3, first_row=4,
    columns=[{"field": "name", "letters": ["B"]}, {"field": "department", "letters": ["D"]}],
    skip_blank="name", fill_down=["department"])

_PROMPT = """You read a school's workbook for a timetabling app. You see each non-empty sheet as text: one line per non-empty cell, `B4=value`, in row order (values as last saved; a line break inside a cell shows as \\n), and `merged: A3:I3` for each merged range. Do not copy the data. Write reading recipes: small JSON instructions saying where the data is. The app runs them itself, exactly, against the whole workbook, and shows the person what they read before anything changes.

Reply with one JSON object:
- "summary": what this workbook is, in one short sentence (e.g. "Room allocation 2027: rooms by tower and level, with the class housed in each").
- "recipes": at most 20; usually one per sheet and target. Write recipes only for data the workbook really holds; write none for a sheet holding nothing below.
- "unsure": anything you could not place or are not sure of, one short sentence each.

TARGETS. Each recipe fills one target, with only that target's fields:
- venues (the rooms): name (required), block, level, capacity (a whole number), kind
- homes (which class is housed in which room): room (required), class, size (a whole number)
- classes: code (required), level, size (a whole number)
- staff: name (required), department
- requirements (who teaches what to which classes): subject (required), classes (required, a list), teachers (a list), periods (a whole number, per class), department, level
A list field's cell may hold several values. classes split on , ; / & + "and" and line breaks; teachers on , ; & + "and" and line breaks, never on "/" ("Ravi s/o Kumar" is one person).

EVERY RECIPE has "kind" ("table" or "pairs"), "target", "sheet" (the sheet's name exactly as shown) and "note" (a short remark for the person checking, or ""). Give every other field too, empty when the kind does not use it: "" for text, 0 for a row, [] for a list.

KIND "table": one record per row.
- "first_row": the first data row. "last_row": the last, or 0 for the sheet's end. "header_row": the header's row (above first_row), or 0.
- "columns": the column holding each field, [{"field": ..., "letters": ["B"]}]: one letter a field, but classes and teachers may list several columns ("Class 1", "Class 2", ...; up to 30).
- "parse": patterns for a column's text, [{"field": a field in columns, "pattern": ...}]. The pattern's named groups fill fields, so one column "4P1 (40)" given as class with ^(?P<class>\\d[A-Z]+\\d?)\\s*\\((?P<size>\\d+)\\)$ fills class and size; a pattern without named groups fills the column's own field with what it matched. A column without a pattern gives its text as it is.
- "skip_blank": a field in columns without which a row is skipped (totals, notes, spacer rows), or "".
- "fill_down": fields in columns whose blank or merged cells repeat the value above (a department written once beside its rows).
A row with no value in any of the columns is skipped; a row without a required field is reported.

KIND "pairs": one record per cell matching a pattern, wherever it is (floor plans, grids, side tables).
- "anchor": every cell this pattern matches is a record; its named groups fill fields.
- "offset" [rows, columns] and "value": the cell that far from the anchor (each from -20 to 20, not [0, 0]; [1, 0] is the cell below) is read with the value pattern, whose named groups fill fields (it needs at least one). Give [] and "" when the anchor cell is the whole record.
- "labels": up to 6, [{"field": ..., "pattern": ..., "search": "up" or "left"}]: the nearest cell above the anchor in its column ("up") or left of it in its row ("left") that matches the pattern fills the field, merged cells included (a title merged across the plan is above every cell under it). The value is the group named after the field, else the whole match.

PATTERNS are Python regular expressions. Each is checked before it runs, and a recipe breaking a rule is refused:
- at most 200 characters; a pattern sees a cell's first 500 characters and matches anywhere in it, so use ^ and $ for the whole cell;
- named groups only for the target's fields: (?P<name>...), (?P<size>\\d+);
- no backreferences (\\1, (?P=name));
- nothing that repeats inside a repeat: (\\w+)+, (\\w*)* and (\\d{1,3}-)+ are refused (a ? inside is fine, and so is (\\d-){1,3});
- no alternation inside a repeat: (ab|cd)+ is refused; write a class such as [abcd]+, and keep alternation outside repeats, as in ^(?:Sec|Year)\\s*(?P<level>\\d)$;
- at most two unbounded repeats (+, *, {n,}) side by side: .*.* is allowed, .*.*.* is not;
- prefer bounded counts such as \\d{1,3} and [A-Z]{1,4} to + and *.
An optional group, (?:\\((?P<size>\\d+)\\))?, that finds nothing just leaves its field out. An anchor or label pattern only picks cells; but a non-empty cell that a parse or value pattern is given and does not match is reported to the person checking, so write each pattern for what the cells really hold (spacing, case, brackets), and no looser than it must be.

EXAMPLES (invented). Sheet "Room Map" is a floor plan: "TOWER N" rows merged across the plan, rows starting "LEVEL 3" followed by room codes such as "N3-12", and under each room its class with an optional size, "3E2 (38)" or "3E2". Its rooms, and the class in each:
{rooms}
{homes}
Sheet "Teachers" is a list: the header in row 3 (No., Name, Post, Department), names from row 4, each department written once beside its first teacher, a "Total" row at the end with no name:
{staff}

THE PLAN this workbook is read into. It calls a person a {person}, a group a {group}, a requirement a {requirement} and a venue a {venue}. {plan_facts} Write class codes and names as the workbook writes them; the app matches them with the plan's."""


def system_prompt(summary: dict) -> str:
    v = summary.get("vocabulary") or M.DEFAULT_VOCABULARY
    classes = summary.get("classes") or []
    if classes:
        shown = ", ".join(classes[:CLASSES_SHOWN])
        more = f" and {len(classes) - CLASSES_SHOWN} more" if len(classes) > CLASSES_SHOWN else ""
        facts = [f"It has {len(classes)} classes: {shown}{more}."]
    else:
        facts = ["It has no classes yet."]
    facts.append(f"It has {summary.get('staff', 0)} staff and {summary.get('venues', 0)} rooms.")
    if summary.get("departments"):
        facts.append(f"Departments: {', '.join(summary['departments'])}.")
    return (_PROMPT.replace("{rooms}", _EXAMPLE_ROOMS).replace("{homes}", _EXAMPLE_HOMES)
            .replace("{staff}", _EXAMPLE_STAFF).replace("{person}", v.get("person", "teacher"))
            .replace("{group}", v.get("group", "class")).replace("{requirement}", v.get("requirement", "lesson"))
            .replace("{venue}", v.get("venue", "room")).replace("{plan_facts}", " ".join(facts)))


def sheets_text(sheets: list[dict], only: set[str] | None = None) -> str:
    """The rendered sheets (those named in `only`, when given) for the user turn, at most USER_MAX
    characters: a sheet past that is named, not shown."""
    parts: list[str] = []
    used = 0
    for s in sheets:
        if only is not None and s["name"] not in only:
            continue
        part = f"# Sheet: {s['name']} ({s['rows']} rows x {s['cols']} columns)\n{s['text']}"
        if used + len(part) > USER_MAX:
            part = f"# Sheet: {s['name']} ({s['rows']} rows x {s['cols']} columns): not shown, the workbook is too long"
        parts.append(part)
        used += len(part) + 2
    return "\n\n".join(parts)


# ---------------------------------------------------------------------------
# write
# ---------------------------------------------------------------------------

def _describe(recipe) -> str:
    if not isinstance(recipe, dict):
        return "a recipe"
    target = recipe.get("target") if isinstance(recipe.get("target"), str) else "unknown"
    sheet = recipe.get("sheet")
    return f"the {target[:40]} recipe for sheet {sheet[:100]!r}" if isinstance(sheet, str) else f"the {target[:40]} recipe"


def _cap(text: str) -> str:
    return text[:1].upper() + text[1:]


def _reason(text) -> str:
    """An error as the person reads it: at most REASON_MAX characters."""
    text = str(text)
    return text if len(text) <= REASON_MAX else text[:REASON_MAX - 3] + "..."


def _text(value, limit: int) -> str:
    return " ".join(value.split())[:limit] if isinstance(value, str) else ""


def _parse(reply) -> tuple[str, list[dict], list[tuple[object, str]], list[str]]:
    """(summary, valid recipes as validated, [(recipe, why it is invalid)], the model's own unsure)."""
    if not isinstance(reply, dict):
        raise ProviderError("the model did not reply with a JSON object")
    summary = _text(reply.get("summary"), SUMMARY_MAX)
    raw = reply.get("recipes") if isinstance(reply.get("recipes"), list) else []
    unsure = [t for t in (_text(u, UNSURE_TEXT_MAX) for u in reply.get("unsure") or [] if isinstance(u, str)) if t]
    unsure = unsure[:UNSURE_MAX]
    valid: list[dict] = []
    invalid: list[tuple[object, str]] = []
    for recipe in raw[:R.RECIPES_MAX]:
        recipe = from_reply(recipe)
        try:
            valid.append(R.validate(recipe))
        except R.RecipeError as e:
            invalid.append((recipe, _reason(e)))
    if len(raw) > R.RECIPES_MAX:
        unsure.append(f"The model wrote {len(raw)} recipes, more than {R.RECIPES_MAX}; the rest were left out.")
    return summary, valid, invalid, unsure


def _ask(provider, sheets: list[dict], summary: dict):
    return _parse(provider.extract_json(system_prompt(summary), sheets_text(sheets), SCHEMA))


def write(provider, sheets: list[dict], plan_summary: dict) -> dict:
    """{summary, recipes, unsure}: the model's reading of `sheets` (render.sheets), each recipe
    validated; an invalid one is left out and named in `unsure` with the reason. ProviderError as the
    provider raises it."""
    summary, valid, invalid, unsure = _ask(provider, sheets, plan_summary)
    unsure += [f"{_cap(_describe(recipe))} was left out: {why}" for recipe, why in invalid]
    return {"summary": summary, "recipes": valid, "unsure": unsure}


# ---------------------------------------------------------------------------
# run: at most READINGS_AT_ONCE at a time
# ---------------------------------------------------------------------------

_slots = threading.BoundedSemaphore(READINGS_AT_ONCE)


def run(data: bytes, recipes: list) -> dict:
    """recipes.run, waiting (up to BUSY_SECONDS) while READINGS_AT_ONCE readings are running;
    ReadingError when no turn comes."""
    if not _slots.acquire(timeout=BUSY_SECONDS):
        raise R.ReadingError("other workbooks are being read; try again in a minute")
    try:
        return R.run(data, recipes)
    finally:
        _slots.release()


# ---------------------------------------------------------------------------
# read
# ---------------------------------------------------------------------------

def _base(plan: dict | None) -> dict:
    return M.normalise(plan) if plan else M.empty_plan()


class _Tried:
    """Recipes run: those that read records fitting the plan (`kept`, with their records), those that
    did not (`failing`, with why) and the reading's doubts."""

    def __init__(self):
        self.kept: list[tuple[dict, list[dict]]] = []
        self.failing: list[tuple[object, str]] = []
        self.doubts: list[str] = []


def _try(data: bytes, recipes: list[dict], plan: dict | None) -> _Tried:
    tried = _Tried()
    if not recipes:
        return tried
    try:
        out = run(data, recipes)
    except R.ReadingError:              # the reading could not run at all: the caller's error, not the recipes'
        raise
    except R.RecipeError as e:          # too slow, too large: no recipe of these read, and each may be why
        tried.failing = [(r, _reason(e)) for r in recipes]
        return tried
    base = _base(plan)
    taken = {target: 0 for target in out["records"]}
    counts = out.get("per_recipe") or [0] * len(recipes)
    said: dict[int, list[str]] = {}     # recipe number -> its own doubts ("recipe 2: no sheet named ...")
    for doubt in out["doubts"]:
        number = _recipe_number(doubt, len(recipes))
        if number is None:
            tried.doubts.append(doubt)
        else:
            said.setdefault(number, []).append(doubt.split(": ", 1)[1])
    for i, (recipe, n) in enumerate(zip(recipes, counts), start=1):
        target = recipe["target"]
        start = taken.get(target, 0)
        records = out["records"].get(target, [])[start:start + n]
        taken[target] = start + n
        own = said.get(i, [])
        if not records:
            why = "; ".join(own) or f"it read nothing from sheet {recipe['sheet']!r}: no cell matched"
            tried.failing.append((recipe, _reason(why)))
            continue
        try:
            patch, _ = R.to_patch({target: records}, plan)
            M.apply_patch(base, patch)
        except M.PlanError as e:
            tried.failing.append((recipe, _reason(f"what it read does not fit the plan: {e}")))
            continue
        tried.kept.append((recipe, records))
        tried.doubts += [f"{_cap(_describe(recipe))}: {_reason(d)}" for d in own]
    return tried


def _recipe_number(doubt: str, count: int) -> int | None:
    """The recipe a doubt of recipes.run is about ("recipe 3: ..."), None for a cell's doubt."""
    if not doubt.startswith("recipe "):
        return None
    head = doubt[len("recipe "):].split(": ", 1)[0]
    return int(head) if head.isdigit() and 1 <= int(head) <= count else None


def _reading(summary: str, kept: list[tuple[dict, list[dict]]], doubts: list[str], plan: dict | None,
             sig: str, reader: str | None) -> dict:
    records: dict[str, list[dict]] = {}
    for recipe, found in kept:
        records.setdefault(recipe["target"], []).extend(found)
    patch, patch_doubts = R.to_patch(records, plan)
    # a room with no class under it is a room, not a home room: not counted or shown as one
    shown = {target: [r for r in found if target != "homes" or r.get("class")] for target, found in records.items()}
    doubts = _capped(doubts + _capped(patch_doubts))
    try:
        M.apply_patch(_base(plan), patch)
    except M.PlanError as e:            # first: the one doubt that says why nothing will be applied
        doubts.insert(0, _reason(f"Together, what was read does not fit the plan: {e}"))
        patch = {}
    return {
        "summary": summary,
        "counts": {target: len(found) for target, found in shown.items() if found},
        "samples": {target: found[:SAMPLES_MAX] for target, found in shown.items() if found},
        "doubts": doubts,
        "records": records,
        "patch": patch,
        "recipes": [recipe for recipe, _ in kept],
        "signature": sig,
        "reader": reader,
    }


_MORE = re.compile(r"^\.\.\.and ([\d,]+) more$")


def _capped(doubts: list[str]) -> list[str]:
    """Doubts in order, each once, at most DOUBTS_MAX, then "...and N more" counting the rest; an
    "...and N more" already among them (recipes.run's own) adds its N to that count."""
    kept: list[str] = []
    extra = 0
    for doubt in dict.fromkeys(doubts):
        more = _MORE.match(doubt)
        if more:
            extra += int(more.group(1).replace(",", ""))
        elif len(kept) < DOUBTS_MAX:
            kept.append(doubt)
        else:
            extra += 1
    return kept + [f"...and {extra:,} more"] if extra else kept


def sheet_key(name) -> str:
    """A sheet name as execute._sheet matches it: case and spacing aside."""
    return " ".join(str(name).split()).lower()


def _uses(entry: dict) -> int:
    uses = entry.get("uses")
    return uses if isinstance(uses, int) and not isinstance(uses, bool) else 0


def _candidates(db, sig: str, names: set[str]) -> list[tuple[dict, bool]]:
    """(saved reader, strict) to try: the one saved for this signature (used when it reads anything),
    then up to SAME_SHEET_TRIED others, most used first, every sheet of whose recipes this workbook
    has (case and spacing aside), used only when they read records without errors."""
    out: list[tuple[dict, bool]] = []
    exact = readers.find(db, sig)              # saved for this signature, or answering to it as an alias
    if exact is not None:
        out.append((exact, False))
    have = {sheet_key(n) for n in names}
    same = []
    for e in readers.list(db):
        recipes = e.get("recipes")
        if (e.get("signature") in (sig, (exact or {}).get("signature")) or not isinstance(recipes, list)
                or not recipes):
            continue
        if not all(isinstance(r, dict) and isinstance(r.get("sheet"), str) for r in recipes):
            continue
        if {sheet_key(r["sheet"]) for r in recipes} <= have:
            same.append(e)
    same.sort(key=lambda e: -_uses(e))         # stable: equally used, the older first
    out += [(e, True) for e in same[:SAME_SHEET_TRIED]]
    return out


def _saved(entry: dict, strict: bool, data: bytes, plan: dict | None, sig: str) -> dict | None:
    recipes = entry.get("recipes")
    if not isinstance(recipes, list) or not recipes or len(recipes) > R.RECIPES_MAX:
        return None
    tried = _try(data, recipes, plan)
    if not tried.kept or (strict and tried.failing):
        return None
    doubts = tried.doubts + [f"{_cap(_describe(r))} did not read: {why}" for r, why in tried.failing]
    name = str(entry.get("name") or "Saved reader")
    # Apply counts a use of this reader (and notes `sig` as its alias): never saves it again
    return {**_reading(name, tried.kept, doubts, plan, sig, name), "reader_signature": entry.get("signature")}


class NoProvider(ProviderError):
    """No model provider is set up: the message is ours, safe to show."""


def _provider(provider):
    """The provider itself, or one made by a factory; ProviderError when there is none."""
    if provider is not None and not hasattr(provider, "extract_json") and callable(provider):
        try:
            provider = provider()
        except ProviderError as e:          # the factory's setup message (no key, no model): ours to show
            raise NoProvider(str(e)) from e
    if provider is None:
        raise NoProvider("No model provider is set up, and no saved reader reads this workbook. "
                            "Add one in Settings.")
    return provider


def _repair_prompt(failing: list[tuple[object, str]], sheets: list[dict]) -> str:
    names = [s["name"] for s in sheets]
    lines = ["Some of your recipes for this workbook did not work. For each problem below write a corrected "
             "recipe, or none when the workbook does not hold that data, and reply with the same JSON object "
             "as before: a summary, only the corrected recipes, and unsure.", ""]
    for n, (recipe, why) in enumerate(failing, start=1):
        lines += [f"Problem {n}: {_describe(recipe)}",
                  f"Recipe: {json.dumps(recipe, ensure_ascii=False, default=str)[:4000]}",
                  f"What went wrong: {why}", ""]
    lines.append("The workbook's sheets: " + ", ".join(repr(n) for n in names))
    wanted = {r.get("sheet") for r, _ in failing if isinstance(r, dict)}
    only = None if wanted - set(names) else wanted          # a sheet it lacks: show them all
    lines += ["", sheets_text(sheets, only)]
    return "\n".join(lines)


def _whole(recipe) -> str:
    """A recipe as one comparable text: two recipes are the same only when every field is."""
    return json.dumps(recipe, sort_keys=True, ensure_ascii=False, default=str)


def _given_up(failing: list[tuple[object, str]], mended: list[dict],
              still: list[tuple[object, str]]) -> list[tuple[object, str]]:
    """The failing recipes the repair did not mend, each with why. Each repaired recipe that reads
    (`mended`) answers one failing recipe of its target, one for the same sheet first (a fixed sheet
    name is a fix too); each repair that failed again (`still`) answers one the same way and gives its
    own reason. A failing recipe is one by its whole text, so two for one target and sheet are each
    answered on their own; a failed repair answering none is given up as itself."""
    slots = list({_whole(r): [r, why, False] for r, why in failing}.values())     # [recipe, why, answered]

    def answer(repair):
        target = repair.get("target") if isinstance(repair, dict) else None
        sheet = sheet_key(repair.get("sheet")) if isinstance(repair, dict) else None
        open_ = [s for s in slots if not s[2] and isinstance(s[0], dict) and s[0].get("target") == target]
        same = [s for s in open_ if sheet_key(s[0].get("sheet")) == sheet]
        slot = (same or open_ or [None])[0]
        if slot is not None:
            slot[2] = True
        return slot

    for repair in mended:
        slot = answer(repair)
        if slot is not None:
            slots.remove(slot)
    for repair, why in still:
        slot = answer(repair)
        if slot is None:
            slots.append([repair, why, True])
        else:
            slot[0], slot[1] = repair, why
    return [(r, why) for r, why, _ in slots]


def read(provider, data: bytes, plan: dict | None, db=None) -> dict:
    """The workbook `data` as a reading: {summary, counts: {target: n}, samples: {target: [<= 5
    records]}, doubts, patch, recipes, signature, reader: the saved reader's name or None; and from a
    saved reader, reader_signature: the signature it is saved under}. `patch` is
    for model.apply_patch on `plan`; nothing is written here. `provider` may be a factory (called
    only when no saved reader reads the workbook) or None. WorkbookError for bytes that are no
    workbook; ProviderError when the model is needed and cannot be had; ReadingError when a reading
    cannot run at all (the reader busy, no child process) — never repaired, never passed over."""
    book = render.open_book(data)          # loaded once, here: the signature and the rendering share it
    sig = signature.of(data, book)
    sheets = render.sheets(data, book)
    del book                                # the readings below run in a child process of their own
    if not sheets:
        return _reading("", [], ["The workbook has no data."], plan, sig, None)
    if db is not None:
        for entry, strict in _candidates(db, sig, {s["name"] for s in sheets}):
            reading = _saved(entry, strict, data, plan, sig)
            if reading is not None:
                return reading
    provider = _provider(provider)
    summary = plan_summary(_base(plan))
    text, valid, invalid, unsure = _ask(provider, sheets, summary)
    tried = _try(data, valid, plan)
    kept, doubts = list(tried.kept), list(unsure) + tried.doubts
    failing = tried.failing + invalid
    if failing:
        system = system_prompt(summary)
        try:
            _, again, again_invalid, again_unsure = _parse(
                provider.extract_json(system, _repair_prompt(failing, sheets), SCHEMA))
        except ProviderError as e:          # the provider's own text (ids, statuses) is for the log only
            _log.warning("repairing the recipes failed: %s", e)
            again, again_invalid, again_unsure = [], [], ["The recipes could not be repaired just now."]
        # a recipe that already works, sent again as it is, is not read twice (nor saved twice on Apply)
        working = {_whole(r) for r, _ in kept}
        again = [r for r in again if _whole(r) not in working]
        retried = _try(data, again, plan)
        kept += retried.kept
        doubts += again_unsure + retried.doubts
        given_up = _given_up(failing, [r for r, _ in retried.kept], retried.failing + again_invalid)
        doubts += [_reason(f"The reader gave up on {_describe(r)}: {why}") for r, why in given_up]
    return _reading(text, kept, doubts, plan, sig, None)
