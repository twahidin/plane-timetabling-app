"""Saved readers (spec docs/superpowers/specs/2026-10-08-smart-workbook-reader-design.md §5): the recipes
that read one workbook layout, remembered under its layout signature so the next file of that layout is
read without the AI.

The school's, not one timetable's: one global kv row `readers`, `{signature: {signature, name, recipes,
made_at, uses, aliases}}` (the way learning/templates.py keeps `local_templates`). `aliases` are the
signatures of other layouts the reader was applied to (found by their sheet names), so the next file of
such a layout finds it at once; at most ALIASES_MAX, the newest kept. Recipes are stored as
`recipes.validate` returns them. Every write is a read-modify-write of that one row, under one lock."""
from __future__ import annotations

import builtins
import threading
import time

from . import recipes as R

KEY = "readers"
READERS_MAX = 50            # saved readers a school keeps
NAME_MAX = 80
SIGNATURE_MAX = 128
ALIASES_MAX = 20            # other layouts' signatures one reader answers to

_lock = threading.Lock()


def _stored(db) -> dict:
    value = db.get_value(KEY)
    return dict(value) if isinstance(value, dict) else {}


def _signature(signature) -> str:
    if not isinstance(signature, str) or not signature or len(signature) > SIGNATURE_MAX:
        raise R.RecipeError("a reader needs its workbook's layout signature")
    return signature


def _aliases(entry: dict) -> builtins.list[str]:
    aliases = entry.get("aliases")
    return [a for a in aliases if isinstance(a, str)] if isinstance(aliases, builtins.list) else []


def find(db, signature: str) -> dict | None:
    """The reader saved for this layout signature, or the one that has it as an alias; else None."""
    if not isinstance(signature, str):
        return None
    stored = _stored(db)
    entry = stored.get(signature)
    if isinstance(entry, dict):
        return entry
    return next((e for e in stored.values() if isinstance(e, dict) and signature in _aliases(e)), None)


def list(db) -> list[dict]:      # noqa: A001  (readers.list, as the brief names it)
    """Every saved reader, oldest first."""
    entries = [e for e in _stored(db).values() if isinstance(e, dict)]
    return sorted(entries, key=lambda e: (e.get("made_at") or 0, str(e.get("name", ""))))


def _short(name) -> str:
    """A reader's name from a reading's summary: the part before its first colon, cut at a word."""
    name = " ".join(str(name or "").split()).split(":")[0].strip()
    if len(name) > NAME_MAX:
        name = name[:NAME_MAX - 1].rsplit(" ", 1)[0].rstrip(",;-") + "…"
    return name or "Saved reader"


def save(db, signature: str, name, recipes, now: float | None = None) -> dict:
    """Remember `recipes` (each validated; RecipeError otherwise) for workbooks of `signature` under
    `name`. Saving a signature again replaces its name and recipes and keeps its uses."""
    signature = _signature(signature)
    if not isinstance(recipes, builtins.list) or not 1 <= len(recipes) <= R.RECIPES_MAX:
        raise R.RecipeError(f"a reader has 1 to {R.RECIPES_MAX} recipes")
    checked = [R.validate(r) for r in recipes]
    name = _short(name)
    with _lock:
        stored = _stored(db)
        old = stored.get(signature) if isinstance(stored.get(signature), dict) else None
        if old is None and len(stored) >= READERS_MAX:
            raise R.RecipeError(f"at most {READERS_MAX} saved readers; delete one first")
        uses = old.get("uses") if old is not None else 0
        entry = {"signature": signature, "name": name, "recipes": checked,
                 "made_at": time.time() if now is None else now,
                 "uses": uses if isinstance(uses, int) and not isinstance(uses, bool) else 0}
        if old is not None and _aliases(old):       # saved again: the layouts it answers to stay
            entry["aliases"] = _aliases(old)
        stored[signature] = entry
        db.set_value(KEY, stored)
    return entry


def delete(db, signature: str) -> None:
    """`KeyError` when no reader is saved for `signature`."""
    with _lock:
        stored = _stored(db)
        del stored[signature]
        db.set_value(KEY, stored)


def add_alias(db, reader_signature: str, signature: str) -> dict | None:
    """Let the reader saved for `reader_signature` answer to `signature` too (a layout it was applied
    to). Nothing when they are equal, the alias is there already, or `signature` is no signature; None
    (and nothing written) when no reader is saved for `reader_signature`. The newest ALIASES_MAX kept."""
    with _lock:
        stored = _stored(db)
        entry = stored.get(reader_signature) if isinstance(reader_signature, str) else None
        if not isinstance(entry, dict):
            return None
        aliases = _aliases(entry)
        if (signature == reader_signature or signature in aliases or not isinstance(signature, str)
                or not signature or len(signature) > SIGNATURE_MAX):
            return entry
        entry["aliases"] = (aliases + [signature])[-ALIASES_MAX:]
        db.set_value(KEY, stored)
        return entry


def used(db, signature: str) -> dict | None:
    """Count one more use of the reader saved for `signature`; None (and nothing written) when none is."""
    with _lock:
        stored = _stored(db)
        entry = stored.get(signature) if isinstance(signature, str) else None
        if not isinstance(entry, dict):
            return None
        uses = entry.get("uses")
        entry["uses"] = (uses if isinstance(uses, int) and not isinstance(uses, bool) else 0) + 1
        db.set_value(KEY, stored)
        return entry
