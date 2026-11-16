"""The sampling values a run really used: temperature, top P and top K as numbers, not "auto".

A run's setting is a number or "auto". "auto" leaves the field out of the request, so the server
uses the model's own default (Ollama reads it from the model's template, `/api/show`) and, for a
parameter the model does not list, its built-in one. Ollama's built-ins are fixed in its source
(`api.DefaultOptions`: temperature 0.8, top_k 40, top_p 0.9); any other server is not asked, so no
value is invented for it.

Each parameter is kept as `{"setting", "value", "source", "text"}`; `text` is what the pages and the
PDFs show: "0.2 (set)", "1 (model default)", "0.8 (Ollama default)" or "server default (not
reported)".
Runs made before this was recorded get the model default read at display time instead, labelled
"model default, read now"; when that cannot be read the text is "auto · not recorded".
"""

from __future__ import annotations

from typing import Any

from ..llm.modelinfo import ModelInfo

KEYS = ("temperature", "top_p", "top_k")
OLLAMA_DEFAULTS: dict[str, float] = {"temperature": 0.8, "top_k": 40, "top_p": 0.9}
"""What Ollama uses when a request and the model leave a parameter out (api/types.go)."""

SOURCE_WORDS = {
    "set": "set",
    "model": "model default",
    "model_now": "model default, read now",
    "ollama": "Ollama default",
}


def _num(v: float) -> str:
    return f"{v:g}"


def _text(value: float | None, source: str) -> str:
    if value is None:
        return "server default (not reported)" if source == "server" else "auto · not recorded"
    return f"{_num(value)} ({SOURCE_WORDS[source]})"


def effective_sampling(
    llm: dict[str, Any],
    info: ModelInfo | None,
    backend_kind: str | None,
    *,
    now: bool = False,
) -> dict[str, dict[str, Any]]:
    """The value each of temperature / top_p / top_k applied, from the run's settings `llm`, the
    model's own info and the backend. With `now` the model's info was read after the run, so the
    source says so; without a readable one an old run's "auto" stays "auto · not recorded"."""
    out: dict[str, dict[str, Any]] = {}
    for key in KEYS:
        setting = llm.get(key)
        if setting is None:
            continue
        value: float | None
        if isinstance(setting, int | float) and not isinstance(setting, bool):
            value, source = float(setting), "set"
        elif info is not None and key in info.parameters:
            value, source = float(info.parameters[key]), "model_now" if now else "model"
        elif now and info is None:
            value, source = None, "unread"
        elif info is not None or backend_kind == "ollama":
            value, source = OLLAMA_DEFAULTS[key], "ollama"
        else:
            value, source = None, "server"
        out[key] = {
            "setting": setting,
            "value": value,
            "source": source,
            "text": _text(value, source),
        }
    return out
