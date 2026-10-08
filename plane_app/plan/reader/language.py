"""The recipe language's vocabulary (spec docs/superpowers/specs/2026-10-08-smart-workbook-reader-design.md
§2): the targets and their fields, which fields are required, numbers or lists, and how a list cell
splits."""
from __future__ import annotations

import re

TARGETS = {
    "venues": ("name", "block", "level", "capacity", "kind"),
    "homes": ("class", "room", "size"),
    "classes": ("code", "level", "size"),
    "staff": ("name", "department"),
    "requirements": ("subject", "classes", "teachers", "periods", "department", "level"),
}
REQUIRED = {"venues": ("name",), "homes": ("room",), "classes": ("code",), "staff": ("name",),
            "requirements": ("subject", "classes")}
NUMBER_FIELDS = frozenset({"size", "capacity", "periods"})
LIST_FIELDS = frozenset({"classes", "teachers"})
KINDS = ("table", "pairs")
SEARCHES = ("left", "up")

# "4P1, 4P2", "4P1/4P2", "4P1 AND 4P2", one a line (Alt+Enter); teachers never split on "/" ("Ravi s/o
# Kumar" is one person)
_SPLIT = {
    "classes": re.compile(r"\s*(?:[,;/&+\r\n]|\band\b)\s*", re.IGNORECASE),
    "teachers": re.compile(r"\s*(?:[,;+&\r\n]|\s+and\s+)\s*", re.IGNORECASE),
}


def split_list(field: str, text: str) -> list[str]:
    return [part for part in _SPLIT[field].split(text) if part]
