"""Reading recipes (spec docs/superpowers/specs/2026-10-08-smart-workbook-reader-design.md §2): JSON
that says where a workbook keeps its data, never the data itself, run deterministically.

Every recipe has `kind`, `target` (TARGETS) and `sheet`, and an optional `note`.

- `table`: one record per row from `first_row` (to `last_row`, else the sheet's end). `columns`
  maps a field to a column letter (a list of letters for `classes` or `teachers`: "Class 1",
  "Class 2", ...). `parse` maps a column's field to a pattern whose named groups fill fields (a
  pattern without named groups fills the column's own field with what it matched). `skip_blank`
  names a field without which a row is skipped; `fill_down` lists fields whose blank (or merged)
  cells repeat the last value above. `header_row` documents where the header is.
- `pairs`: every cell matching `anchor` is a record (its named groups fill fields); the cell at
  `offset` [rows, cols] from it holds a value read by `value`'s named groups; each of `labels`
  [{field, pattern, search: "left"|"up"}] is the nearest cell in that direction, merged cells
  included, matching its pattern: the group named for the field, else the whole match.

A non-empty cell a pattern addresses but does not match is a doubt ("H5: 'Staff Rm' does not
match"); a named group a match leaves out (an optional size) only leaves its field out. Empty cells are skipped. Every pattern passes
`patterns.safe_pattern` and only sees a cell's first TEXT_MAX characters; and since no static check
proves a pattern fast, `run` reads in a child process stopped after RUN_SECONDS."""
from __future__ import annotations

import multiprocessing
import pickle
import re
import sys
import time

from .execute import execute
from .language import KINDS, LIST_FIELDS, NUMBER_FIELDS, REQUIRED, SEARCHES, TARGETS  # noqa: F401
from .patch import to_patch  # noqa: F401  (recipes.to_patch: records -> plan patch)
from .patterns import PATTERN_MAX, TEXT_MAX, RecipeError, safe_pattern  # noqa: F401
from .render import WorkbookError

RUN_SECONDS = 20            # a reading's wall-clock limit (tests lower it)
MEMORY_MAX = 1 << 30        # a reading's address space, where the platform enforces it
MESSAGE_MAX = 8 << 20       # the child's one message back, pickled
RECIPES_MAX = 20            # recipes in one reader
OFFSET_MAX = 20
LABELS_MAX = 6
COLUMNS_MAX = 30            # columns of one list field
ROW_LIMIT = 1_048_576       # Excel's own
SHEET_NAME_MAX = 100
NOTE_MAX = 500

class ReadingError(RecipeError):
    """The reading itself could not run (no child process to be had, the reader busy): no recipe's
    fault, so nothing to repair and no reason to try another reader — the caller's error. A child that
    dies part-way is a plain RecipeError: the workbook or the recipes may be too large."""


_COMMON = {"kind", "target", "sheet", "note"}
_KEYS = {"table": _COMMON | {"header_row", "first_row", "last_row", "columns", "parse", "skip_blank", "fill_down"},
         "pairs": _COMMON | {"anchor", "offset", "value", "labels"}}
_LABEL_KEYS = {"field", "pattern", "search"}
_COLUMN = re.compile(r"^[A-Za-z]{1,3}$")


# ---------------------------------------------------------------------------
# validate
# ---------------------------------------------------------------------------

def _field(name, target: str, where: str) -> str:
    if not isinstance(name, str) or name not in TARGETS[target]:
        raise RecipeError(f"{where}: unknown field {name!r} for {target} (fields: {', '.join(TARGETS[target])})")
    return name


def _row(value, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= ROW_LIMIT:
        raise RecipeError(f"{name} must be a row number from 1")
    return value


def _pattern(text, where: str, target: str, named: bool = False) -> re.Pattern:
    try:
        p = safe_pattern(text)
    except RecipeError as e:
        raise RecipeError(f"{where}: {e}")
    for group in p.groupindex:
        _field(group, target, where)
    if named and not p.groupindex:
        raise RecipeError(f"{where}: the pattern needs a named group such as (?P<{TARGETS[target][0]}>...)")
    return p


def _columns(raw, target: str) -> dict[str, list[str]]:
    if not isinstance(raw, dict) or not raw:
        raise RecipeError("columns must map fields to column letters")
    out: dict[str, list[str]] = {}
    for field, letters in raw.items():
        _field(field, target, "columns")
        if isinstance(letters, list):
            if field not in LIST_FIELDS and len(letters) != 1:
                raise RecipeError(f"columns: {field} takes one column")
            if not 1 <= len(letters) <= COLUMNS_MAX:
                raise RecipeError(f"columns: {field} needs 1 to {COLUMNS_MAX} columns")
        else:
            letters = [letters]
        cols = []
        for letter in letters:
            if not isinstance(letter, str) or not _COLUMN.match(letter.strip()):
                raise RecipeError(f"columns: {field}: {letter!r} is not a column letter such as B")
            cols.append(letter.strip().upper())
        out[field] = cols
    return out


def _fields_in(raw, name: str, columns: dict) -> list[str]:
    if not isinstance(raw, list):
        raise RecipeError(f"{name} must be a list of fields")
    for field in raw:
        if not isinstance(field, str) or field not in columns:
            raise RecipeError(f"{name}: {field!r} is not one of the columns")
    return list(dict.fromkeys(raw))


def _validate_table(recipe: dict, target: str) -> tuple[dict, set]:
    first = _row(recipe.get("first_row"), "first_row")
    out: dict = {"first_row": first}
    if recipe.get("header_row") is not None:
        header = _row(recipe["header_row"], "header_row")
        if header >= first:
            raise RecipeError("header_row must be above first_row")
        out["header_row"] = header
    if recipe.get("last_row") is not None:
        last = _row(recipe["last_row"], "last_row")
        if last < first:
            raise RecipeError("last_row must not be above first_row")
        out["last_row"] = last
    columns = _columns(recipe.get("columns"), target)
    out["columns"] = columns
    parse = recipe.get("parse") or {}
    if not isinstance(parse, dict):
        raise RecipeError("parse must map fields to patterns")
    filled = set(columns)
    out["parse"] = {}
    for field, text in parse.items():
        if not isinstance(field, str) or field not in columns:
            raise RecipeError(f"parse: {field!r} is not one of the columns")
        p = _pattern(text, f"parse {field}", target)
        out["parse"][field] = text
        if p.groupindex:
            filled = (filled - {field}) | set(p.groupindex)
    if recipe.get("skip_blank") is not None:
        out["skip_blank"] = _fields_in([recipe["skip_blank"]], "skip_blank", columns)[0]
    if recipe.get("fill_down") is not None:
        out["fill_down"] = _fields_in(recipe["fill_down"], "fill_down", columns)
    return out, filled


def _offset(raw) -> list[int]:
    if (not isinstance(raw, list) or len(raw) != 2
            or any(isinstance(n, bool) or not isinstance(n, int) or abs(n) > OFFSET_MAX for n in raw)):
        raise RecipeError(f"offset must be [rows, columns], two whole numbers from -{OFFSET_MAX} to {OFFSET_MAX}")
    if raw == [0, 0]:
        raise RecipeError("offset [0, 0] is the anchor itself")
    return list(raw)


def _validate_pairs(recipe: dict, target: str) -> tuple[dict, set]:
    anchor = _pattern(recipe.get("anchor"), "anchor", target)
    out: dict = {"anchor": recipe["anchor"]}
    filled = set(anchor.groupindex)
    has_value, has_offset = recipe.get("value") is not None, recipe.get("offset") is not None
    if has_value or has_offset:
        if not has_offset:
            raise RecipeError("a value needs an offset [rows, columns] from the anchor")
        out["offset"] = _offset(recipe["offset"])
        if not has_value:
            raise RecipeError("an offset needs a value pattern")
        value = _pattern(recipe["value"], "value", target, named=True)
        out["value"] = recipe["value"]
        filled |= set(value.groupindex)
    labels = recipe.get("labels") or []
    if not isinstance(labels, list) or len(labels) > LABELS_MAX:
        raise RecipeError(f"labels must be a list of at most {LABELS_MAX}")
    out["labels"] = []
    for i, label in enumerate(labels, start=1):
        if not isinstance(label, dict):
            raise RecipeError(f"label {i} must be an object")
        extra = sorted(str(k) for k in set(label) - _LABEL_KEYS)
        if extra:
            raise RecipeError(f"label {i}: unknown key {extra[0]!r}")
        field = _field(label.get("field"), target, f"label {i}")
        _pattern(label.get("pattern"), f"label {i}", target)
        if not isinstance(label.get("search"), str) or label["search"] not in SEARCHES:
            raise RecipeError(f"label {i}: search must be left or up")
        out["labels"].append({"field": field, "pattern": label["pattern"], "search": label["search"]})
        filled.add(field)
    return out, filled


def validate(recipe) -> dict:
    """The recipe as run (column letters upper case, lists where a list goes), or RecipeError — never
    another exception, whatever shape the recipe has."""
    try:
        return _validate(recipe)
    except RecipeError:
        raise
    except Exception as e:      # a shape the checks above missed: still the caller's plain message
        raise RecipeError(f"the recipe does not read ({e.__class__.__name__})")


def _validate(recipe) -> dict:
    if not isinstance(recipe, dict):
        raise RecipeError("a recipe must be an object")
    kind = recipe.get("kind")
    if not isinstance(kind, str) or kind not in KINDS:
        raise RecipeError(f"unknown kind {kind!r}: a recipe is a table or pairs")
    target = recipe.get("target")
    if not isinstance(target, str) or target not in TARGETS:
        raise RecipeError(f"unknown target {target!r}: one of {', '.join(TARGETS)}")
    extra = sorted(str(k) for k in set(recipe) - _KEYS[kind])
    if extra:
        raise RecipeError(f"unknown key {extra[0]!r} for a {kind} recipe")
    sheet = recipe.get("sheet")
    if not isinstance(sheet, str) or not sheet.strip() or len(sheet) > SHEET_NAME_MAX:
        raise RecipeError("sheet must name one of the workbook's sheets")
    note = recipe.get("note")
    if note is not None and (not isinstance(note, str) or len(note) > NOTE_MAX):
        raise RecipeError(f"note must be text of at most {NOTE_MAX} characters")
    body, filled = (_validate_table if kind == "table" else _validate_pairs)(recipe, target)
    missing = [f for f in REQUIRED[target] if f not in filled]
    if missing:
        raise RecipeError(f"a {target} recipe must fill {', '.join(missing)}")
    out = {"kind": kind, "target": target, "sheet": sheet, **body}
    if note is not None:
        out["note"] = note
    return out


# ---------------------------------------------------------------------------
# run: in a child process, under a time limit
# ---------------------------------------------------------------------------

_CONTEXT = None


def _context():
    """forkserver where the platform has it (Linux, macOS): one clean server process, preloaded with
    the reader, forks each reading; spawn elsewhere. Never plain fork: the app has threads."""
    global _CONTEXT
    if _CONTEXT is None:
        if "forkserver" in multiprocessing.get_all_start_methods():
            ctx = multiprocessing.get_context("forkserver")
            ctx.set_forkserver_preload(["plane_app.plan.reader.execute"])
        else:                                       # pragma: no cover
            ctx = multiprocessing.get_context("spawn")
        _CONTEXT = ctx
    return _CONTEXT


def _limit_memory() -> None:
    """Cap the child's address space at MEMORY_MAX where the platform enforces it (Linux); elsewhere
    (macOS ignores RLIMIT_AS) the reading's own bounds and the time limit are the guard."""
    if not sys.platform.startswith("linux"):
        return
    try:
        import resource
        soft, hard = resource.getrlimit(resource.RLIMIT_AS)
        cap = MEMORY_MAX if hard == resource.RLIM_INFINITY else min(MEMORY_MAX, hard)
        resource.setrlimit(resource.RLIMIT_AS, (cap, hard))
    except (ImportError, ValueError, OSError):      # pragma: no cover
        pass


def _message(result: tuple) -> bytes:
    """The one message the child sends: the pickled result, or a short refusal when that would be
    larger than MESSAGE_MAX — a huge reading never crosses the pipe."""
    try:
        message = pickle.dumps(result, protocol=pickle.HIGHEST_PROTOCOL)
    except MemoryError:
        return pickle.dumps(("memory", ""))
    if len(message) > MESSAGE_MAX:
        return pickle.dumps(("large", ""))
    return message


def _child(conn, data: bytes, recipes: list) -> None:
    try:
        _limit_memory()
        result = ("ok", execute(data, recipes))
    except MemoryError:
        result = ("memory", "")
    except WorkbookError as e:
        result = ("workbook", str(e))
    except Exception as e:                          # reported to the parent, which raises RecipeError
        result = ("error", f"{e.__class__.__name__}: {e}")
    try:
        conn.send_bytes(_message(result))
    finally:
        conn.close()


def _stop(child, grace: float) -> None:
    """Wait `grace` seconds for the child to end, then terminate it, then kill it; always reaped."""
    child.join(grace)
    if child.is_alive():
        child.terminate()
        child.join(2)
    if child.is_alive():                            # pragma: no cover
        child.kill()
        child.join()
    child.close()


def _receive(conn, seconds: float) -> dict:
    """The child's result from `conn`, waiting at most `seconds` for it to begin; the message itself is
    bounded (MESSAGE_MAX), so reading it once begun is short. RecipeError or WorkbookError otherwise."""
    if not conn.poll(max(0.0, seconds)):
        raise TimeoutError
    try:
        kind, payload = pickle.loads(conn.recv_bytes(MESSAGE_MAX))
    except EOFError:            # the child died with nothing sent: killed, most often for its memory
        raise RecipeError("reading stopped: the workbook may be too large") from None
    except (OSError, pickle.UnpicklingError, ValueError, TypeError):
        raise RecipeError("reading stopped before it finished") from None
    if kind == "ok":
        return payload
    if kind == "workbook":
        raise WorkbookError(payload)
    if kind == "memory":
        raise RecipeError("reading used too much memory")
    if kind == "large":
        raise RecipeError("the reading is too large — the recipe may be too broad")
    raise RecipeError(f"reading failed: {payload}")


def _isolated(data: bytes, recipes: list, seconds: float) -> dict:
    deadline = time.monotonic() + seconds
    receive = send = child = None
    grace = 5.0
    try:
        try:
            ctx = _context()
            receive, send = ctx.Pipe(duplex=False)
            child = ctx.Process(target=_child, args=(send, data, recipes), daemon=True)
            child.start()
        except OSError as e:            # no process or pipe to be had (limits, a full table): said plainly
            raise ReadingError(f"the reading could not start ({e.strerror or e.__class__.__name__}); "
                              f"try again in a moment") from None
        send.close()
        send = None
        try:
            return _receive(receive, deadline - time.monotonic())
        except TimeoutError:
            grace = 0.0
            raise RecipeError("reading took too long — the recipe may be too complex") from None
    finally:
        for end in (receive, send):
            if end is not None:
                end.close()
        if child is not None and child.pid is not None:
            _stop(child, grace)


def run(data: bytes, recipes: list, seconds: float | None = None) -> dict:
    """{records: {target: [record]}, doubts: [text], per_recipe: [records read]}: every recipe run
    against the workbook, in order (execute.execute says more). A recipe that does not validate, or names a sheet the workbook lacks, is a doubt. The reading
    happens in a child process: RecipeError when it takes longer than `seconds` (RUN_SECONDS), is too
    large, uses too much memory or dies; ReadingError (a RecipeError) only when the child cannot start."""
    if not isinstance(recipes, list):
        raise RecipeError("recipes must be a list")
    if len(recipes) > RECIPES_MAX:
        raise RecipeError(f"a reader has at most {RECIPES_MAX} recipes, not {len(recipes)}")
    checked: list[tuple[int, dict | str]] = []
    for i, raw in enumerate(recipes, start=1):
        try:
            checked.append((i, validate(raw)))
        except RecipeError as e:
            checked.append((i, str(e)))         # a doubt in its turn
    return _isolated(data, checked, RUN_SECONDS if seconds is None else seconds)
