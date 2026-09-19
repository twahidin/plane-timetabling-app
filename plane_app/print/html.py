"""HTML rendering of grids: a page per grid, print CSS, no external assets."""
from __future__ import annotations

from .grid import Grid


def render(templates, grids: list[Grid], generated: str, timetable: str) -> str:
    return templates.env.get_template("print/grid.html").render(grids=grids, generated=generated, timetable=timetable)
