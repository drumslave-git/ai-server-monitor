"""A chat answer that is only a tool call written as text (live 0.7.6, ministral-3:14b: LLM
calls 9918 `bash {...}` and 9924 `bash[ARGS]{...}`) goes back once; written like that again it
is stored with a note. Nothing is run from text."""

from __future__ import annotations

from typing import Any

import pytest
from fastapi.testclient import TestClient

from ai_server_monitor.chat.engine import TEXT_CALL, TEXT_CALL_NOTE, text_tool_call
from ai_server_monitor.config import Bootstrap

from .jobfakes import ScriptedModels, tool
from .test_chat_run import frames
from .test_jobs import make_app

DOCKER = "bash {\n  docker ps --format '{{.Names}}\\t{{.Status}}\\t{{.Image}}'\n}"
MISTRAL = 'bash[ARGS]{"command": "df -h /mnt/disks/disk1TB_1"}'


def ask(client: TestClient, message: str) -> str:
    response = client.post("/api/chat", json={"message": message})
    session_id = frames(response.text)[0]["session_id"]
    return client.get(f"/api/chat/sessions/{session_id}").json()["messages"][-1]["content"]


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        (DOCKER, True),
        (MISTRAL, True),
        ('[TOOL_CALLS]bash[ARGS]{"command": "uptime"}', True),
        ('nope[ARGS]{"command": "uptime"}', False),  # not an offered tool
        ("Run this:\n\n```bash\nbash {\n  ls\n}\n```", False),
        ("You can write `bash { ls }` in a script.", False),
    ],
)
def test_only_a_whole_answer_that_is_a_call_of_an_offered_tool_counts(
    text: str, expected: bool
) -> None:
    assert text_tool_call(text, {"bash", "delegate"}) is expected


def test_a_text_call_is_sent_back_once_and_the_real_call_runs(
    bootstrap: Bootstrap, models: ScriptedModels, shell: list[str]
) -> None:
    models.script = [DOCKER, tool("bash", command="docker ps"), "All containers are up."]
    with TestClient(make_app(bootstrap)) as client:
        stored = ask(client, "which containers run?")
    assert stored == "All containers are up."
    assert TEXT_CALL in models.requests[1]["text"]
    assert shell == ["docker ps"]  # the real call, nothing from the text


def test_a_text_call_twice_is_stored_with_the_note(
    bootstrap: Bootstrap, models: ScriptedModels, shell: list[str]
) -> None:
    models.script = [DOCKER, MISTRAL]
    with TestClient(make_app(bootstrap)) as client:
        stored = ask(client, "which containers run?")
    assert stored == f"{TEXT_CALL_NOTE}\n\n```\n{MISTRAL}\n```"
    assert shell == [] and len(models.requests) == 2


def test_an_ordinary_answer_that_mentions_a_call_is_untouched(
    bootstrap: Bootstrap, models: ScriptedModels
) -> None:
    answer = "In a script you would write `bash { ls }`; here it is not needed."
    models.script = [answer]
    with TestClient(make_app(bootstrap)) as client:
        stored = ask(client, "how?")
    assert stored == answer and len(models.requests) == 1


def test_explain_keeps_its_prose_when_only_text_calls_follow(
    bootstrap: Bootstrap, models: ScriptedModels, shell: list[str]
) -> None:
    """Live turn c50f6658b35c: the answer sent back by LookFirst was followed by a text-form
    call, which was stored instead of the prose."""
    prose = "### Current state\nThe disk is 97% full."
    models.script = [prose, MISTRAL, MISTRAL]
    with TestClient(make_app(bootstrap)) as client:
        ctx = client.app.state.ctx  # type: ignore[attr-defined]

        async def explain() -> list[dict[str, Any]]:
            return [e async for e in ctx.chat.run("explain the disk", require_look=True)]

        events = client.portal.call(explain)
    done = events[-1]
    assert done["type"] == "done"
    assert done["text"] == f"{prose}\n\n---\n\n{TEXT_CALL_NOTE}\n\n```\n{MISTRAL}\n```"
    assert shell == [] and len(models.requests) == 3
