"""Lookup in the bundled endpoint index (`index_data/endpoints.json`, made by
`scripts/gen_api_index.py` from the apps' OpenAPI specs plus hand-written indexes).

`search()` is what the `api_endpoints` tool returns: ready-made recipes first (a small model
copies an example far more reliably than it composes a call), then matching endpoints. Without
a search term it returns the recipes and the groups to search in, never the whole list.
"""

from __future__ import annotations

import json
import math
import re
from functools import lru_cache
from pathlib import Path
from typing import Any

DATA_FILE = Path(__file__).parent / "index_data" / "endpoints.json"
DEFAULT_RESULTS = 12
MAX_RESULTS = 40
MAX_RECIPES = 3

_WORD = re.compile(r"[a-z0-9]+")
_STOP = frozenset(
    [
        "a",
        "an",
        "the",
        "of",
        "in",
        "on",
        "to",
        "for",
        "all",
        "any",
        "what",
        "which",
        "who",
        "how",
        "are",
        "is",
        "we",
        "my",
        "me",
        "our",
        "show",
        "get",
        "list",
        "give",
        "find",
        "call",
        "tell",
        "about",
        "with",
        "and",
        "or",
        "from",
        "do",
        "does",
    ]
)


@lru_cache(maxsize=1)
def load() -> dict[str, Any]:
    return json.loads(DATA_FILE.read_text("utf-8"))


def kinds() -> list[str]:
    return list(load()["services"])


def _stem(word: str) -> str:
    if len(word) <= 3:
        return word
    if word.endswith("ies"):
        return word[:-3] + "y"  # queries -> query, movies -> movy ...
    if word.endswith("ie"):
        return word[:-2] + "y"  # ... and movie -> movy, so both forms meet
    if word.endswith(("sses", "xes", "ches", "shes")):
        return word[:-2]
    if word.endswith("s") and not word.endswith(("ss", "us")):
        return word[:-1]
    return word


def _tokens(text: str) -> list[str]:
    return [_stem(w) for w in _WORD.findall(text.lower()) if w not in _STOP]


def _entry_words(entry: list[str]) -> tuple[set[str], set[str], set[str]]:
    _method, path, tag, summary, params = entry
    path_words = {_stem(w) for w in _WORD.findall(path.lower())}
    path_words.discard("api")
    other = {_stem(w) for w in _WORD.findall(f"{tag} {summary}".lower())}
    return path_words, other, {_stem(w) for w in _WORD.findall(params.lower())}


def _format(entry: list[str]) -> str:
    method, path, _, summary, params = entry
    text = f"{method} {path} - {summary}"
    return f"{text} [{params}]" if params else text


def search(kind: str, query: str | None, *, max_results: int = DEFAULT_RESULTS) -> dict[str, Any]:
    """Recipes and endpoints of one service kind matching `query` (words, any order)."""
    data = load()
    service = data["services"].get(kind)
    if service is None:
        return {"error": f"no endpoint index for {kind!r}"}
    max_results = max(1, min(max_results, MAX_RESULTS))
    tokens = _tokens(query or "")
    entries: list[list[str]] = service["entries"]
    recipes: list[dict[str, Any]] = service["recipes"]
    out: dict[str, Any] = {"service": kind, "prefix": service["prefix"] or "(none)"}
    if not tokens:
        out["recipes"] = recipes[: MAX_RECIPES + 3]
        groups: dict[str, int] = {}
        for entry in entries:
            groups[entry[2]] = groups.get(entry[2], 0) + 1
        out["groups"] = ", ".join(f"{g} ({n})" for g, n in sorted(groups.items()))
        out["hint"] = (
            "Call again with search=<words> (e.g. a group name, 'queue', 'missing') to list the "
            "matching endpoints."
        )
        return out
    out["recipes"] = _rank_recipes(recipes, tokens)
    words = [_entry_words(e) for e in entries]
    total = len(entries)
    document_frequency = {t: sum(1 for w in words if any(t in part for part in w)) for t in tokens}
    scored: list[tuple[float, int]] = []
    for index, (path_w, other_w, param_w) in enumerate(words):
        score = 0.0
        for token in tokens:
            weight = math.log(1 + total / max(document_frequency[token], 1))
            if token in path_w:
                score += 3 * weight
            elif any(w.startswith(token) for w in path_w if len(token) >= 3):
                score += 1.5 * weight
            if token in other_w:
                score += 2 * weight
            if token in param_w:
                score += 0.5 * weight
        if score:
            scored.append((score, index))
    scored.sort(key=lambda s: (-s[0], s[1]))
    chosen = [entries[i] for _, i in scored[:max_results]]
    out["endpoints"] = [_format(e) for e in chosen]
    if len(scored) > max_results:
        out["more"] = f"{len(scored) - max_results} more matches: use more specific words"
    if not chosen:
        out["hint"] = "No endpoint matches; try other words or call without search."
    if any(e[0] != "GET" for e in chosen) and kind in ("radarr", "sonarr", "whisparr", "prowlarr"):
        out["note"] = data["arr_note"]
    return out


def _rank_recipes(recipes: list[dict[str, Any]], tokens: list[str]) -> list[dict[str, Any]]:
    scored: list[tuple[int, int]] = []
    for index, recipe in enumerate(recipes):
        text = f"{recipe['for']} {recipe['call']} {recipe.get('note', '')}".lower()
        words = {_stem(w) for w in _WORD.findall(text)}
        hits = sum(1 for t in tokens if t in words)
        if hits:
            scored.append((hits, index))
    scored.sort(key=lambda s: (-s[0], s[1]))
    return [recipes[i] for _, i in scored[:MAX_RECIPES]]
