"""Alert thresholds for PromQL query results: a strict grammar, nothing evaluated.

`> 80`, `>= 0.5`, `< 10`, `<= 1`, `== 0`, `!= 1` (operator, number). Anything else is
rejected, because the text can come from a language model or a web form.
"""

from __future__ import annotations

import math
import re

_THRESHOLD = re.compile(r"^\s*(>=|<=|==|!=|>|<)\s*(-?\d+(?:\.\d+)?(?:[eE][+-]?\d+)?)\s*$")
GRAMMAR_HELP = "Use an operator and a number, e.g. > 80, >= 0.5, < 10, <= 1, == 0 or != 1."


def parse_threshold(text: str) -> tuple[str, float]:
    """(operator, limit) for a valid threshold; ValueError otherwise."""
    match = _THRESHOLD.match(text) if isinstance(text, str) else None
    if match is None:
        raise ValueError(f"invalid threshold {str(text)[:40]!r}. {GRAMMAR_HELP}")
    limit = float(match.group(2))
    if not math.isfinite(limit):
        raise ValueError(f"invalid threshold {str(text)[:40]!r}. {GRAMMAR_HELP}")
    return match.group(1), limit


def normalize_threshold(text: str | None) -> str | None:
    """Canonical form ('> 80') of a valid threshold; None/empty stays None."""
    if text is None or (isinstance(text, str) and not text.strip()):
        return None
    op, limit = parse_threshold(text)
    return f"{op} {limit:g}"


def breaches(threshold: str | None, value: object) -> bool:
    """Does `value` cross `threshold`? Non-numeric values and bad thresholds never do."""
    if not threshold or isinstance(value, bool) or not isinstance(value, int | float):
        return False
    try:
        op, limit = parse_threshold(threshold)
    except ValueError:
        return False
    if not math.isfinite(value):
        return False
    return {
        ">": value > limit,
        ">=": value >= limit,
        "<": value < limit,
        "<=": value <= limit,
        "==": value == limit,
        "!=": value != limit,
    }[op]
