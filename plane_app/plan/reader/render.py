"""Sheets as compact text (spec docs/superpowers/specs/2026-10-08-smart-workbook-reader-design.md §3.1):
what the recipe writer reads. Each non-empty sheet is its non-empty cells as `A1=value` lines in row
order (first MAX_ROWS rows x MAX_COLS columns, values only — formulas are never evaluated), its merged
ranges as `merged: A1:C1` lines, at most MAX_CHARS characters: a longer sheet keeps its top and its
bottom with a note on what was cut."""
from __future__ import annotations

import io
import zipfile
from datetime import date, datetime, time

from openpyxl.utils import get_column_letter

MAX_ROWS = 200
MAX_COLS = 30
MAX_CHARS = 60_000
CELL_CHARS = 200        # one cell's text in the rendering
MERGED_MAX = 300        # merged ranges listed per sheet
UNPACKED_MAX = 40 * 1024 * 1024     # bytes of sheet XML and shared strings, unpacked, the reader loads
TOO_LARGE = "This workbook is too large for the reader; fill in the template instead."


class WorkbookError(ValueError):
    """Bytes that openpyxl cannot open as a workbook, or one too large to load."""


def _unpacked_size(data: bytes) -> int:
    """The unpacked bytes of every XML part of the workbook (sheets wherever the workbook keeps them,
    shared strings, styles), from the zip's own directory: nothing is unpacked to know it.
    zipfile.BadZipFile for bytes that are no zip."""
    with zipfile.ZipFile(io.BytesIO(data)) as z:
        return sum(info.file_size for info in z.infolist() if info.filename.lower().endswith(".xml"))


def open_book(data: bytes):
    """The workbook with cell values as last saved (`data_only`: a formula is never evaluated here).
    Not read-only: merged ranges are only known to a fully loaded sheet, and a loaded sheet costs many
    times its XML, so a workbook whose sheets unpack to more than UNPACKED_MAX is refused unloaded."""
    import openpyxl
    try:
        too_large = _unpacked_size(data) > UNPACKED_MAX
    except Exception as e:      # not a zip at all: said as for any other unreadable file
        raise WorkbookError(f"this file could not be read as a workbook ({e.__class__.__name__})")
    if too_large:
        raise WorkbookError(TOO_LARGE)
    try:
        return openpyxl.load_workbook(io.BytesIO(data), data_only=True)
    except Exception as e:      # zip, xml or openpyxl's own errors: none of them is the caller's to know
        raise WorkbookError(f"this file could not be read as a workbook ({e.__class__.__name__})")


def cell_text(value) -> str:
    """A cell's value as text: a whole number without ".0", a date in ISO form, text stripped."""
    if value is None:
        return ""
    if isinstance(value, bool):
        return "TRUE" if value else "FALSE"
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    if isinstance(value, (datetime, date, time)):
        return value.isoformat()
    return str(value).strip()


def ref(row: int, col: int) -> str:
    return f"{get_column_letter(col)}{row}"


def _merged_lines(ws) -> list[str]:
    ranges = sorted((r for r in ws.merged_cells.ranges if r.min_row <= MAX_ROWS and r.min_col <= MAX_COLS),
                    key=lambda r: (r.min_row, r.min_col))
    return [f"merged: {r.coord}" for r in ranges[:MERGED_MAX]]


def cells(ws, max_row: int, max_col: int) -> list[tuple[int, int, object]]:
    """(row, column, value) of every non-empty cell within max_row x max_col, in row order. Only the
    cells the sheet holds are visited: iter_rows would make every cell of the window, and one stray
    cell at row 100 000 must not cost a hundred thousand rows."""
    held = getattr(ws, "_cells", None)
    if held is None:                    # pragma: no cover  (a read-only sheet; open_book never makes one)
        return [(r, c, v) for r, row in enumerate(ws.iter_rows(max_row=max_row, max_col=max_col, values_only=True), 1)
                for c, v in enumerate(row, 1) if v is not None]
    return sorted((r, c, cell.value) for (r, c), cell in held.items()
                  if r <= max_row and c <= max_col and cell.value is not None)


def _cell_lines(ws) -> list[tuple[int, str]]:
    out: list[tuple[int, str]] = []
    for r, c, value in cells(ws, MAX_ROWS, MAX_COLS):
        text = cell_text(value)
        if text:
            text = text.replace("\r\n", "\n").replace("\n", "\\n")[:CELL_CHARS]
            out.append((r, f"{ref(r, c)}={text}"))
    return out


def _fit(head: list[str], lines: list[tuple[int, str]]) -> str:
    """`head` and every cell line when they fit in MAX_CHARS; else the head, the top cells and the
    bottom cells around one note naming the rows left out."""
    full = "\n".join(head + [line for _, line in lines])
    if len(full) <= MAX_CHARS:
        return full
    note_room = 120
    budget = MAX_CHARS - note_room - sum(len(h) + 1 for h in head)
    half = budget // 2
    top: list[tuple[int, str]] = []
    used = 0
    for item in lines:
        if used + len(item[1]) + 1 > half:
            break
        top.append(item)
        used += len(item[1]) + 1
    bottom: list[tuple[int, str]] = []
    used = 0
    for item in reversed(lines[len(top):]):
        if used + len(item[1]) + 1 > half:
            break
        bottom.append(item)
        used += len(item[1]) + 1
    bottom.reverse()
    left_out = lines[len(top):len(lines) - len(bottom)]
    first, last = left_out[0][0], left_out[-1][0]
    note = (f"... cut: {len(left_out)} cells of rows {first}-{last} not shown "
            f"(the sheet is longer than {MAX_CHARS} characters) ...")
    return "\n".join(head + [line for _, line in top] + [note] + [line for _, line in bottom])


def sheet_text(ws) -> str | None:
    """One sheet's rendering, None when the window holds no value at all."""
    cells = _cell_lines(ws)
    if not cells:
        return None
    head = _merged_lines(ws)
    if ws.max_row > MAX_ROWS or ws.max_column > MAX_COLS:
        head.insert(0, f"note: the sheet has {ws.max_row} rows x {ws.max_column} columns; only rows "
                       f"1-{min(ws.max_row, MAX_ROWS)} and columns A-{get_column_letter(min(ws.max_column, MAX_COLS))} "
                       f"are shown")
    return _fit(head, cells)


def sheets(data: bytes, book=None) -> list[dict]:
    """[{name, text, rows, cols}] for each non-empty sheet; rows and cols are the sheet's own size.
    `book`: `data` already opened (open_book), so it is not loaded again."""
    wb = book if book is not None else open_book(data)
    out = []
    for ws in wb.worksheets:
        text = sheet_text(ws)
        if text is not None:
            out.append({"name": ws.title, "text": text, "rows": ws.max_row, "cols": ws.max_column})
    return out
