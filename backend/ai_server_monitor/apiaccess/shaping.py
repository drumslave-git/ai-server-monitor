"""Making an API answer fit a small model's context.

`/api/v3/movie` is thousands of objects with posters, ratings and alternate titles. The
model asks for what it needs with `select` (JMESPath); without one it gets a trimmed view
(big fields dropped, long text cut, empty values removed). Either way the list is limited
and the result is held under a character budget, and the answer says what was left out so
the model can narrow the call instead of guessing.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

import jmespath
from jmespath import functions
from jmespath.exceptions import JMESPathError

from .errors import ApiAccessError

DEFAULT_LIMIT = 50
MAX_LIMIT = 500
MAX_RESULT_CHARS = 5500  # TODO setting; just below the chat's own 6000-char tool result cap
STRING_CAP = 200
SELECTED_STRING_CAP = 600
NESTED_LIST_CAP = 10
MAX_DEPTH = 8

# Never shown unless selected: pictures, links and bulk that no answer needs.
DROP_KEYS = frozenset(
    {
        "images",
        "remotePoster",
        "remoteFanart",
        "remoteBanner",
        "alternateTitles",
        "mediaInfo",
        "magnet_uri",
        "infohash_v1",
        "infohash_v2",
        "ratings",
        "popularity",
    }
)


class _Functions(functions.Functions):
    @functions.signature({"types": ["string"]})
    def _func_lower(self, value: str) -> str:
        return value.lower()


_OPTIONS = jmespath.Options(custom_functions=_Functions())


@dataclass
class Shaped:
    data: Any
    total: int | None = None
    """Items before the limit/size cap (only set when something was left out)."""
    showing: int | None = None
    notes: list[str] = field(default_factory=list)
    paged: dict[str, Any] | None = None
    """page / pageSize / totalRecords of the server's own paging, when it paged."""


def paged_meta(data: Any) -> dict[str, Any] | None:
    """The *arr paging envelope: {page, pageSize, totalRecords, records: [...]}."""
    if isinstance(data, dict) and isinstance(data.get("records"), list) and "totalRecords" in data:
        return {k: data[k] for k in ("page", "pageSize", "totalRecords") if k in data}
    return None


def run_select(data: Any, expression: str) -> Any:
    try:
        return jmespath.compile(expression).search(data, options=_OPTIONS)
    except JMESPathError as exc:
        raise ApiAccessError(
            f"select is not valid JMESPath ({' '.join(str(exc).split())[:160]}). "
            "Example: records[].{title: title, year: year}"
        ) from None


def prune(value: Any, depth: int = 0) -> Any:
    """The trimmed default view: no pictures/bulk, no empty values, text cut at 200
    characters, long nested lists cut."""
    if isinstance(value, str):
        return value if len(value) <= STRING_CAP else value[: STRING_CAP - 1] + "…"
    if isinstance(value, list):
        kept = [prune(v, depth + 1) for v in value[:NESTED_LIST_CAP]] if depth else None
        if kept is None:  # top level: the caller applies `limit`
            return [prune(v, depth + 1) for v in value]
        if len(value) > NESTED_LIST_CAP:
            kept.append(f"…{len(value) - NESTED_LIST_CAP} more")
        return kept
    if isinstance(value, dict):
        if depth >= MAX_DEPTH:
            return f"{{…{len(value)} keys}}"
        out = {}
        for key, item in value.items():
            if key in DROP_KEYS or item is None or item == "" or item == [] or item == {}:
                continue
            out[key] = prune(item, depth + 1)
        return out
    return value


def _cut_strings(value: Any, cap: int) -> Any:
    if isinstance(value, str):
        return value if len(value) <= cap else value[: cap - 1] + "…"
    if isinstance(value, list):
        return [_cut_strings(v, cap) for v in value]
    if isinstance(value, dict):
        return {k: _cut_strings(v, cap) for k, v in value.items()}
    return value


def _size(value: Any) -> int:
    return len(json.dumps(value, ensure_ascii=False, default=str, separators=(",", ":")))


def shape(
    data: Any,
    *,
    select: str | None = None,
    limit: int = DEFAULT_LIMIT,
    max_chars: int = MAX_RESULT_CHARS,
    keep_tail: bool = False,
) -> Shaped:
    """Select, trim, limit and size-cap `data`. `keep_tail`: when cutting a list keep the
    last items instead of the first (log lines)."""
    limit = max(1, min(int(limit), MAX_LIMIT))
    result = Shaped(data=None, paged=paged_meta(data))
    if select:
        selected = run_select(data, select)
        if selected is None:
            result.notes.append(
                "select matched nothing (null). " + _where_to_look(data) + " Try without select."
            )
            return result
        out: Any = _cut_strings(selected, SELECTED_STRING_CAP)
    else:
        out = prune(data["records"] if result.paged else data)
    original = len(out) if isinstance(out, list) else None
    if isinstance(out, list) and len(out) > limit:
        out = out[-limit:] if keep_tail else out[:limit]
    out, _ = _fit(out, max_chars, keep_tail)
    result.data = out
    if original is not None and isinstance(out, list) and len(out) < original:
        result.total, result.showing = original, len(out)
        which = "last" if keep_tail else "first"
        result.notes.append(
            f"{original} total, showing {which} {len(out)}: use select or query parameters "
            "to narrow"
        )
    _paging_note(result, data)
    return result


def _where_to_look(data: Any) -> str:
    if isinstance(data, dict):
        return f"The response is an object with keys: {', '.join(list(data)[:15])}."
    if isinstance(data, list):
        first = data[0] if data else None
        if isinstance(first, dict):
            return (
                f"The response is a list of {len(data)} objects with keys: "
                f"{', '.join(list(first)[:20])}."
            )
        return f"The response is a list of {len(data)} items."
    return f"The response is a {type(data).__name__}."


def _fit(value: Any, budget: int, keep_tail: bool) -> tuple[Any, int | None]:
    """The value within `budget` characters, and how many list items fit when a list had to
    be shortened (None = nothing cut)."""
    if _size(value) <= budget:
        return value, None
    if isinstance(value, list) and value:
        lo, hi = 1, len(value)
        while lo < hi:  # largest prefix (or suffix) that fits
            mid = (lo + hi + 1) // 2
            part = value[-mid:] if keep_tail else value[:mid]
            if _size(part) <= budget:
                lo = mid
            else:
                hi = mid - 1
        part = value[-lo:] if keep_tail else value[:lo]
        if _size(part) <= budget:
            return part, lo
        value = part
    for cap in (200, 100, 60, 30):
        trimmed = _cut_strings(value, cap)
        if _size(trimmed) <= budget:
            return trimmed, 1 if isinstance(value, list) else None
    text = json.dumps(value, ensure_ascii=False, default=str, separators=(",", ":"))
    return text[: max(budget - 20, 0)] + "…[cut]", 1 if isinstance(value, list) else None


def _paging_note(result: Shaped, raw: Any) -> None:
    """The server pages its answers; tell the model when the page it got is not everything."""
    paged = result.paged
    if not paged or not isinstance(raw, dict):
        return
    got = len(raw.get("records") or [])
    total = paged.get("totalRecords") or 0
    if total > got:
        page = int(paged.get("page") or 1)
        result.notes.append(
            f"the server has {total} records, this response holds {got} (page {page}): "
            f"add query page={page + 1} for the next ones, or all_pages=true"
        )
