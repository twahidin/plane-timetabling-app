"""The lesson pattern grammar: how a school writes a subject's lesson lengths, "2x3p" or
"1x2p, 4x3p", and how a requirement's `lessons` ({length: count}, as plan.model stores it) reads
and prints it.

Items are comma-separated, each `NxLp`, `N x L`, `NxL` or `Lp` (one lesson of L). Case and spaces
are ignored. Every problem is plain text for the file's conflict list, never an exception."""
from __future__ import annotations

import re

from . import model as M

# N lessons of L periods: "2x3p", "2 x 3", "2X3P", or "3p" for one. A bare "3" is not one: it is a block
# number or a count in a school's own column.
_ITEM = re.compile(r"^(?:(\d{1,3})x(\d{1,3})p?|(\d{1,3})p)$")      # 3 digits at most: int() never meets a huge run


def parse_pattern(text: str, periods: int | None) -> tuple[dict[str, int] | None, str | None]:
    """(lessons, problem) for a pattern. Blank text is (None, None): no pattern, so the caller keeps
    its default lessons. A length repeated in two items adds up. The problem is a pattern that is
    not readable (each length 1..LESSON_LENGTH_MAX, each count at least 1, three digits at most), has
    more than SPLIT_LESSONS_MAX lessons, or, when `periods` is
    given, one whose lessons do not total that many periods."""
    text = (text or "").strip()
    items = [i.strip() for i in text.split(",") if i.strip()]
    if not items:
        return None, None
    lessons: dict[str, int] = {}
    for item in items:
        m = _ITEM.match(re.sub(r"\s+", "", item).lower())
        count, length = ((int(m.group(1)), int(m.group(2))) if m.group(2) else (1, int(m.group(3)))) if m else (0, 0)
        if not m or count < 1 or not 1 <= length <= M.LESSON_LENGTH_MAX:
            return None, f"pattern {text}: {item} isn't like 2x3p"
        lessons[str(length)] = lessons.get(str(length), 0) + count
    n_lessons = sum(lessons.values())
    if n_lessons > M.SPLIT_LESSONS_MAX:
        return None, f"pattern {text}: at most {M.SPLIT_LESSONS_MAX} lessons, not {n_lessons}"
    total = sum(int(length) * n for length, n in lessons.items())
    if periods is not None and total != periods:
        return None, f"pattern {text} makes {total} periods, not {periods}"
    return lessons, None


def format_pattern(lessons: dict[str, int]) -> str:
    """The pattern text for `lessons`, shortest lengths first and zero counts left out:
    {"3": 2, "2": 1} is "1x2p, 2x3p". parse_pattern reads it back."""
    return ", ".join(f"{n}x{length}p" for length, n in sorted(lessons.items(), key=lambda kv: int(kv[0])) if n)
