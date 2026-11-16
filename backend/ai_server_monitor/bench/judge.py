"""The optional judge: a second model that grades an answer 1-5 against the scenario's rubric.

Its grade is stored beside the rule score and never mixed into it. Plain HTTP to an
OpenAI-compatible `/chat/completions`; no tracing (it is not the model under test).
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any

import httpx

from ..config import BenchJudgeSettings
from ..llm.text import strip_reasoning
from ..tracing.parse import empty_answer_message

log = logging.getLogger(__name__)

TIMEOUT_S = 120.0
MAX_TOKENS = 4096
"""The grade is a few words, but the judge may be a thinking model: its reasoning counts too."""
SYSTEM = """\
You grade answers written by a server-monitoring assistant. You get the task, a rubric that \
says what a 5/5 answer contains, and the assistant's answer (with the tools it used). Grade \
strictly against the rubric: 5 = everything in the rubric, 4 = small gaps, 3 = half, 2 = mostly \
missing or partly wrong, 1 = wrong, invented or no answer. Facts not backed by the task data \
are errors. Reply with ONLY JSON: {"score": <1-5>, "reason": "<one sentence>"}"""


def judge_messages(task: str, rubric: str, answer: str, tools: list[str]) -> list[dict[str, str]]:
    used = ", ".join(tools) if tools else "none"
    return [
        {"role": "system", "content": SYSTEM},
        {
            "role": "user",
            "content": (
                f"TASK:\n{task}\n\nRUBRIC (what a 5/5 answer contains):\n{rubric}\n\n"
                f"TOOLS THE ASSISTANT CALLED: {used}\n\nASSISTANT'S ANSWER:\n{answer[:6000]}"
            ),
        },
    ]


def parse_grade(text: str) -> tuple[int | None, str]:
    text = strip_reasoning(text)
    candidates = [text, *re.findall(r"\{.*?\}", text, re.DOTALL)]
    for candidate in candidates:
        try:
            value = json.loads(candidate)
        except ValueError:
            continue
        if isinstance(value, dict) and "score" in value:
            try:
                score = round(float(value["score"]))
            except (TypeError, ValueError):
                continue
            if 1 <= score <= 5:
                return score, str(value.get("reason") or "")[:300]
    match = re.search(r"\b([1-5])\s*/\s*5\b", text)
    if match:
        return int(match.group(1)), text.strip()[:300]
    return None, f"unparseable judge reply: {text.strip()[:120]}"


async def grade(
    cfg: BenchJudgeSettings,
    *,
    task: str,
    rubric: str,
    answer: str,
    tools: list[str],
    transport: httpx.AsyncBaseTransport | None = None,
) -> tuple[int | None, str]:
    """→ (score 1-5 or None, reason). Never raises."""
    headers = {}
    key = cfg.api_key.get_secret_value().strip()
    if key:
        headers["Authorization"] = f"Bearer {key}"
    payload: dict[str, Any] = {
        "model": cfg.model,
        "messages": judge_messages(task, rubric, answer, tools),
        "temperature": 0,
        "max_tokens": MAX_TOKENS,
        "stream": False,
    }
    try:
        async with httpx.AsyncClient(
            headers=headers, timeout=TIMEOUT_S, transport=transport
        ) as client:
            response = await client.post(
                f"{cfg.base_url.rstrip('/')}/chat/completions", json=payload
            )
        if response.status_code != 200:
            return None, f"judge HTTP {response.status_code}"
        choice = response.json()["choices"][0]
        content = choice["message"].get("content") or ""
        if choice.get("finish_reason") == "length" and not strip_reasoning(content):
            return None, empty_answer_message(MAX_TOKENS)
    except (httpx.HTTPError, ValueError, KeyError, IndexError) as exc:
        log.info("judge call failed: %s", exc)
        return None, f"judge failed: {type(exc).__name__}"
    return parse_grade(content)
