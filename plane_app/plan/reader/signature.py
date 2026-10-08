"""A workbook's layout signature (spec docs/superpowers/specs/2026-10-08-smart-workbook-reader-design.md
§5): structure, not data, so next term's file of the same layout finds the reader saved for this one.

Per sheet: its name (digits as "#"), its column count bucketed to fives, and the sorted labels of its
header rows. A header row is one of the sheet's leading rows (within the first HEADER_ROWS) where at
least HEADER_SHARE of the non-empty cells are labels and none is a number; the first row that is not
ends the header (blank rows aside), so a table's data — names, subjects, class lists — never counts
however much text it holds. A label is text that is not a number, not a code (CODE: "B4-1",
"4P4 (42)", "LEVEL 4") and at most LABEL_MAX characters; its digits count as one "#" ("Venue
Allocation 2027" and "... 2028" are one label). The row count is left out: a longer list is the same
layout."""
from __future__ import annotations

import hashlib
import json
import re

from .render import MAX_COLS, cells, open_book

HEADER_ROWS = 10
HEADER_SHARE = 0.6
COL_BUCKET = 5
LABEL_MAX = 30
CODE = re.compile(r"^[A-Z0-9][A-Z0-9 /().-]{0,9}$")
NUMBER = re.compile(r"^[+-]?\d+(?:[.,]\d+)*%?$")
_DIGITS = re.compile(r"\d+")


def label_like(value) -> str | None:
    """The label a cell holds, or None: only text is a label, never a number, a date or a code."""
    if not isinstance(value, str):
        return None
    text = " ".join(value.split())
    if not text or len(text) > LABEL_MAX or CODE.match(text) or NUMBER.match(text):
        return None
    return _DIGITS.sub("#", text)


def _is_number(value) -> bool:
    return (isinstance(value, (int, float)) and not isinstance(value, bool)) or \
        (isinstance(value, str) and bool(NUMBER.match(value.strip())))


def header_labels(ws) -> list[str]:
    rows: dict[int, list] = {}
    for r, _c, value in cells(ws, HEADER_ROWS, MAX_COLS):
        if not (isinstance(value, str) and not value.strip()):
            rows.setdefault(r, []).append(value)
    labels: set[str] = set()
    for r in sorted(rows):
        values = rows[r]
        found = [label for label in (label_like(v) for v in values) if label]
        if any(_is_number(v) for v in values) or len(found) < HEADER_SHARE * len(values):
            break
        labels.update(found)
    return sorted(labels)


def _sheet_part(ws) -> list:
    return [_DIGITS.sub("#", ws.title), ws.max_column // COL_BUCKET, header_labels(ws)]


def of(data: bytes, book=None) -> str:
    """sha256 (hex) of every sheet's part, in workbook order. `book`: `data` already opened
    (render.open_book), so it is not loaded again."""
    wb = book if book is not None else open_book(data)
    parts = [_sheet_part(ws) for ws in wb.worksheets]
    return hashlib.sha256(json.dumps(parts, ensure_ascii=False).encode("utf-8")).hexdigest()
