"""Running validated recipes against a workbook, in this process and without a time limit: what
recipes.run's child process does (and what tests time directly). See recipes for the language."""
from __future__ import annotations

import re

from openpyxl.utils import column_index_from_string

from .grid import Grid
from .language import LIST_FIELDS, NUMBER_FIELDS, REQUIRED, split_list
from .patterns import safe_pattern, search
from .render import open_book

RECORDS_MAX = 5000          # records in one reading, all targets together
DOUBTS_MAX = 200            # doubts kept; the rest are counted


class _Doubts:
    """Doubts in order, each once, at most DOUBTS_MAX of them; `extra` counts the rest."""

    def __init__(self):
        self.items: list[str] = []
        self.seen: set[str] = set()
        self.extra = 0

    def append(self, text: str) -> None:
        if text in self.seen:
            return
        if len(self.items) >= DOUBTS_MAX:
            self.extra += 1
            return
        self.seen.add(text)
        self.items.append(text)


class _Budget:
    """The records a reading may still collect."""

    def __init__(self):
        self.left = RECORDS_MAX

    @property
    def spent(self) -> bool:
        return self.left <= 0

    def take(self, out: list, record: dict | None) -> None:
        if record is not None and self.left > 0:
            out.append(record)
            self.left -= 1


def _show(text: str) -> str:
    return text if len(text) <= 60 else text[:57] + "..."


def _add(fields: dict, name: str, value: str, where: str) -> None:
    """Fields hold (value, where) pairs until _finish; a list field gathers every value it is given."""
    if name in LIST_FIELDS:
        fields.setdefault(name, []).append((value, where))
    elif name not in fields:
        fields[name] = (value, where)


def _groups(match, pattern: re.Pattern, where: str, fields: dict) -> None:
    """The named groups a match filled. A group that took no part (an optional size) or matched only
    blanks leaves its field out, and is no doubt: only a cell that does not match is one."""
    for name in pattern.groupindex:
        value = match.group(name)
        if value is not None and value.strip():
            _add(fields, name, value.strip(), where)


def _whole(text: str) -> int | None:
    try:
        number = float(text.replace(",", ""))
    except ValueError:
        return None
    return int(number) if number.is_integer() and number >= 0 else None


def _finish(fields: dict, target: str, at: str, doubts, missing_at: dict | None = None) -> dict | None:
    """A record of plain values: numbers for NUMBER_FIELDS, lists for LIST_FIELDS, text otherwise;
    None (and a doubt, at the field's own column when `missing_at` names one) when a required field
    is missing."""
    record: dict = {}
    for name, held in fields.items():
        if name in LIST_FIELDS:
            items: list[str] = []
            for value, _ in held:
                items.extend(split_list(name, value))
            items = list(dict.fromkeys(items))
            if items:
                record[name] = items
        elif name in NUMBER_FIELDS:
            value, where = held
            number = _whole(value)
            if number is None:
                doubts.append(f"{where}: {name} '{_show(value)}' is not a whole number")
            else:
                record[name] = number
        else:
            record[name] = held[0]
    for name in REQUIRED[target]:
        if name not in record:
            doubts.append(f"{(missing_at or {}).get(name, at)}: no {name}")
            return None
    record["at"] = at
    return record


def _run_table(grid: Grid, recipe: dict, doubts, budget: _Budget) -> list[dict]:
    target = recipe["target"]
    columns = {f: [column_index_from_string(letter) for letter in letters] for f, letters in recipe["columns"].items()}
    parse = {f: safe_pattern(p) for f, p in recipe["parse"].items()}
    fill_down = set(recipe.get("fill_down") or ())
    skip_blank = recipe.get("skip_blank")
    last_row = min(recipe.get("last_row") or grid.rows, grid.rows)
    any_col = min(c for cols in columns.values() for c in cols)
    above: dict[str, list] = {}
    out: list[dict] = []
    for r in range(recipe["first_row"], last_row + 1):
        if budget.spent:
            break
        cells: dict[str, list[tuple[str, str]]] = {}
        for field, cols in columns.items():
            found = [(grid.text(r, c), grid.at(r, c)) for c in cols if grid.text(r, c)]
            if field in fill_down:
                if found:
                    above[field] = found
                else:
                    found = above.get(field, [])
            if found:
                cells[field] = found
        if not cells or (skip_blank and skip_blank not in cells):
            continue
        fields: dict = {}
        for field, found in cells.items():
            pattern = parse.get(field)
            for text, where in found:
                if pattern is None:
                    _add(fields, field, text, where)
                    continue
                m = search(pattern, text)
                if m is None:
                    doubts.append(f"{where}: '{_show(text)}' does not match")
                elif pattern.groupindex:
                    _groups(m, pattern, where, fields)
                elif m.group(0).strip():
                    _add(fields, field, m.group(0).strip(), where)
        # the row's first cell holding a value of its own (not one filled down) names the record
        first_col = min((c for cols in columns.values() for c in cols if grid.text(r, c)), default=any_col)
        missing_at = {f: grid.at(r, cols[0]) for f, cols in columns.items()}
        budget.take(out, _finish(fields, target, grid.at(r, first_col), doubts, missing_at))
    return out


def _run_pairs(grid: Grid, recipe: dict, doubts, budget: _Budget) -> list[dict]:
    target = recipe["target"]
    anchor = safe_pattern(recipe["anchor"])
    value = safe_pattern(recipe["value"]) if "value" in recipe else None
    labels = [(lab["field"], grid.labels(lab["field"], safe_pattern(lab["pattern"]), lab["search"]))
              for lab in recipe["labels"]]
    out: list[dict] = []
    for (r, c), text in sorted(grid.values.items()):
        if budget.spent:
            break
        m = search(anchor, text)
        if m is None:
            continue
        at = grid.at(r, c)
        fields: dict = {}
        _groups(m, anchor, at, fields)
        if value is not None:
            vr, vc = r + recipe["offset"][0], c + recipe["offset"][1]
            shown = grid.shown(vr, vc) if vr >= 1 and vc >= 1 else ""
            if shown:
                vm = search(value, shown)
                if vm is None:
                    doubts.append(f"{grid.at(vr, vc)}: '{_show(shown)}' does not match")
                else:
                    _groups(vm, value, grid.at(vr, vc), fields)
        for field, index in labels:
            found = index.nearest(r, c)
            if found is not None:
                _add(fields, field, found, at)
        budget.take(out, _finish(fields, target, at, doubts))
    return out


def _sheet(wb, name: str):
    exact = next((ws for ws in wb.worksheets if ws.title == name), None)
    if exact is not None:
        return exact
    want = " ".join(name.split()).lower()
    return next((ws for ws in wb.worksheets if " ".join(ws.title.split()).lower() == want), None)


def execute(data: bytes, recipes: list[tuple[int, dict | str]]) -> dict:
    """{records, doubts, per_recipe} for (number, validated recipe) pairs, run in order against the
    workbook; in place of a recipe, text is why that one did not validate, a doubt in its turn. Bounded: at most
    RECORDS_MAX records (then a doubt, and no further recipe runs) and DOUBTS_MAX doubts (then one
    "...and N more"). `per_recipe` counts the records each recipe read, in order (0 for one that did
    not run), so a target's records split back into their recipes'."""
    wb = open_book(data)
    records: dict[str, list[dict]] = {}
    doubts = _Doubts()
    budget = _Budget()
    grids: dict[str, Grid] = {}
    stopped = None
    per_recipe: list[int] = []
    for i, recipe in recipes:
        per_recipe.append(0)
        if isinstance(recipe, str):
            doubts.append(f"recipe {i}: {recipe}")
            continue
        found = records.setdefault(recipe["target"], [])
        if budget.spent:
            continue
        ws = _sheet(wb, recipe["sheet"])
        if ws is None:
            doubts.append(f"recipe {i}: no sheet named {recipe['sheet']!r}")
            continue
        grid = grids.get(ws.title) or grids.setdefault(ws.title, Grid(ws))
        step = _run_table if recipe["kind"] == "table" else _run_pairs
        read = step(grid, recipe, doubts, budget)
        per_recipe[-1] = len(read)
        found.extend(read)
        if budget.spent and stopped is None:
            stopped = f"recipe {i}: stopped at {RECORDS_MAX:,} records; the recipe may be too broad"
    out = list(doubts.items)
    if stopped:
        out.append(stopped)
    if doubts.extra:
        out.append(f"...and {doubts.extra:,} more")
    return {"records": records, "doubts": out, "per_recipe": per_recipe}
