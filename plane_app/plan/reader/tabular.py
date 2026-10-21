"""How a spreadsheet cell, a header cell and a CSV's text are read, shared by the student-list reader
(inbox) and the five exact readers (csvpack). Pure: no database, no logging of cell values."""
from __future__ import annotations

import csv
import re

DELIMITERS = ",;\t|"


def cell(v) -> str:
    """A cell as text: whitespace collapsed, None as "", a whole float as an int (an ID or a code typed as
    a number: 1003, not 1003.0)."""
    if isinstance(v, float) and v.is_integer():
        v = int(v)
    return "" if v is None else " ".join(str(v).split())


def head(c) -> str:
    """A header cell as it is compared: lower case, punctuation as spaces ("Admission No." is "admission no")."""
    return " ".join(re.sub(r"[^0-9a-z]+", " ", str(c if c is not None else "").lower()).split())


def decoded(data: bytes) -> str:
    """A CSV's bytes as text: UTF-16 by its mark, else UTF-8 (a BOM dropped), else cp1252."""
    if data.startswith((b"\xff\xfe", b"\xfe\xff")):
        text = data.decode("utf-16", errors="replace")
    else:
        try:
            text = data.decode("utf-8-sig")
        except UnicodeDecodeError:
            text = data.decode("cp1252", errors="replace")
    return text.replace("\x00", "")         # UTF-16 without its mark reads as text between NULs


def delimiters(text: str) -> list[str]:
    """The delimiters to try on a CSV's text, in order: a comma, then the one the sniffer sees, then each
    other common one."""
    try:
        sniffed = csv.Sniffer().sniff(text[:4096], DELIMITERS).delimiter
    except csv.Error:
        sniffed = ","
    return list(dict.fromkeys([",", sniffed, *DELIMITERS]))
