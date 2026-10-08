"""A sheet as a recipe reads it: its non-empty cells (sparse, within GRID_ROWS x GRID_COLS) and its
merged ranges, where a cell inside a merged range shows the range's top-left value as it does on
screen; and, for `pairs` labels, the nearest matching cell to the left or above any cell, found by
bisection over the label's matches rather than by walking the sheet from every anchor."""
from __future__ import annotations

import bisect
import re

from .patterns import search
from .render import cell_text, cells, ref

GRID_ROWS = 20_000          # what a recipe reads of a sheet
GRID_COLS = 200
MERGED_CELLS_MAX = 200_000


class Grid:
    def __init__(self, ws):
        self.name = ws.title
        self.rows = min(ws.max_row, GRID_ROWS)
        self.cols = min(ws.max_column, GRID_COLS)
        self.values: dict[tuple[int, int], str] = {}
        for r, c, value in cells(ws, self.rows, self.cols):
            text = cell_text(value)
            if text:
                self.values[(r, c)] = text
        # (row, col) -> top-left of its merged range, for every cell of a range but its top-left
        self.merged: dict[tuple[int, int], tuple[int, int]] = {}
        budget = MERGED_CELLS_MAX
        for rng in ws.merged_cells.ranges:
            top = (rng.min_row, rng.min_col)
            if top not in self.values or rng.min_row > self.rows or rng.min_col > self.cols:
                continue                # a merged range shows its top-left; an empty one shows nothing
            for r in range(rng.min_row, min(rng.max_row, self.rows) + 1):
                for c in range(rng.min_col, min(rng.max_col, self.cols) + 1):
                    if budget <= 0:
                        return
                    if (r, c) != top:
                        self.merged[(r, c)] = top
                        budget -= 1

    def text(self, r: int, c: int) -> str:
        return self.values.get((r, c), "")

    def shown(self, r: int, c: int) -> str:
        return self.values.get(self.merged.get((r, c), (r, c)), "")

    def at(self, r: int, c: int) -> str:
        return f"{self.name}!{ref(r, c)}"

    def labels(self, field: str, pattern: re.Pattern, direction: str) -> "Labels":
        return Labels(self, field, pattern, direction)


class Labels:
    """Every cell (merged cells included) showing text that `pattern` matches, indexed by row for
    "left" and by column for "up": `nearest(r, c)` is one bisection. A label's value is the group named
    for its field, else the whole match; a match with an empty value gives None."""

    def __init__(self, grid: Grid, field: str, pattern: re.Pattern, direction: str):
        self.left = direction == "left"
        found: dict[tuple[int, int], str | None] = {}
        for pos, text in grid.values.items():
            m = search(pattern, text)
            if m is not None:
                value = m.group(field) if field in pattern.groupindex else m.group(0)
                found[pos] = value.strip() if value and value.strip() else None
        for pos, top in grid.merged.items():
            if top in found:
                found[pos] = found[top]
        self.lines: dict[int, list[int]] = {}
        self.value: dict[tuple[int, int], str | None] = found
        for (r, c) in found:
            line, along = (r, c) if self.left else (c, r)
            self.lines.setdefault(line, []).append(along)
        for along in self.lines.values():
            along.sort()

    def nearest(self, r: int, c: int) -> str | None:
        line, along = (r, c) if self.left else (c, r)
        positions = self.lines.get(line)
        if not positions:
            return None
        i = bisect.bisect_left(positions, along) - 1
        if i < 0:
            return None
        before = positions[i]
        return self.value[(r, before) if self.left else (before, c)]
