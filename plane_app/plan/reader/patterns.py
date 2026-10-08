"""Safe patterns for reading recipes (Global Constraints of the smart workbook reader plan).

A recipe's regular expressions come from a model, so every one passes `safe_pattern` — the one check,
wherever a pattern is used — and only ever sees a cell's first TEXT_MAX characters (`search`). The
check is static and conservative: it refuses what is known to backtrack badly. It cannot prove a
pattern fast, so recipes.run also runs every reading in a child process under a time limit."""
from __future__ import annotations

import functools
import re

try:                                    # the standard library's own regex parser (3.11+), else its old name
    from re import _parser as _sre
except ImportError:                     # pragma: no cover
    import sre_parse as _sre            # type: ignore[no-redef]

PATTERN_MAX = 200
TEXT_MAX = 500              # a pattern only ever sees this much of a cell
ADJACENT_UNBOUNDED_MAX = 2  # .*.* is allowed, .*.*.* is not


class RecipeError(ValueError):
    """A recipe that cannot be run, said plainly."""


_REPEATS = {"MAX_REPEAT", "MIN_REPEAT", "POSSESSIVE_REPEAT"}
_BACKREFS = {"GROUPREF", "GROUPREF_EXISTS", "GROUPREF_IGNORE", "GROUPREF_LOC_IGNORE", "GROUPREF_UNI_IGNORE"}
# items that hold no sub-pattern; anything neither here nor a known container is refused (fail closed)
_LEAVES = {"LITERAL", "NOT_LITERAL", "ANY", "IN", "AT", "CATEGORY", "RANGE", "NEGATE"}
_CONTAINERS = _REPEATS | {"SUBPATTERN", "BRANCH", "ASSERT", "ASSERT_NOT", "ATOMIC_GROUP"}


def _children(op: str, av) -> list:
    """The sub-patterns one parsed regex item holds; RecipeError for an item this check does not know."""
    if op in _REPEATS:
        return [av[2]]
    if op == "SUBPATTERN":
        return [av[-1]]
    if op == "BRANCH":
        return list(av[1])
    if op in ("ASSERT", "ASSERT_NOT"):
        return [av[1]]
    if op == "ATOMIC_GROUP":
        return [av]
    if op in _LEAVES:
        return []
    raise RecipeError(f"a pattern may not use {op.lower().replace('_', ' ')}")


def _unbounded(op: str, av) -> bool:
    return op in _REPEATS and av[1] == _sre.MAXREPEAT


def _check(items, inside_repeat: bool) -> None:
    run = 0
    for op, av in items:
        name = str(op)
        if name in _BACKREFS:
            raise RecipeError("a pattern may not use backreferences")
        run = run + 1 if _unbounded(name, av) else 0
        if run > ADJACENT_UNBOUNDED_MAX:
            raise RecipeError("a pattern may not have more than two unbounded repeats in a row (such as .*.*.*); "
                              "bound one, as in \\d{1,3}")
        repeats = name in _REPEATS and av[1] > 1          # a + or * or {n,m}; ? is no repeat
        if repeats and inside_repeat:
            raise RecipeError("a pattern may not repeat a repeat (nested quantifiers such as (a+)+ can run forever)")
        if name == "BRANCH" and inside_repeat:
            raise RecipeError("a pattern may not repeat an alternation (such as (a|aa)+); use a character class "
                              "such as [ab]+")
        for child in _children(name, av):
            _check(child, inside_repeat or repeats)


@functools.lru_cache(maxsize=512)
def _compile(text: str) -> re.Pattern:
    if len(text) > PATTERN_MAX:
        raise RecipeError(f"a pattern is longer than {PATTERN_MAX} characters")
    if "(?P=" in text:
        raise RecipeError("a pattern may not use backreferences")
    try:
        tree = _sre.parse(text)
        compiled = re.compile(text)
    except (re.error, RecursionError, OverflowError) as e:
        raise RecipeError(f"pattern {text[:60]!r} does not compile: {e}")
    _check(tree, inside_repeat=False)
    return compiled


def safe_pattern(text) -> re.Pattern:
    """A recipe's pattern compiled, or RecipeError: text of at most PATTERN_MAX characters that
    compiles, with no backreference, no repeat or alternation inside a repeat, no more than two
    unbounded repeats in a row, and nothing the check does not know."""
    if not isinstance(text, str) or not text:
        raise RecipeError("a pattern must be text")
    return _compile(text)


def search(pattern: re.Pattern, text: str):
    return pattern.search(text[:TEXT_MAX])
