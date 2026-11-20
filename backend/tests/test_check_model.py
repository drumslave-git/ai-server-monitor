"""The Settings Test button (`POST /api/settings/test-llm`, `llm.check`): the tool-call part
runs like a real run of the profile (streamed, its instructions and tools) and passes only on a
real `bash` tool call."""

from __future__ import annotations

import json
import re
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient

from ai_server_monitor.agent.shell import HostShell, ShellResult
from ai_server_monitor.config import Bootstrap, Settings
from ai_server_monitor.llm.check import TEXT_INSTEAD
from ai_server_monitor.main import create_app

from .conftest import LLM_URL
from .test_chat import FakeLLM, call, make_app, say

CHAT_TOOLS = [
    "bash",
    "delegate",
    "jobs_list",
    "job_results",
    "job_save",
    "job_delete",
    "summary_member",
    "job_run",
]


@pytest.fixture
def echo_shell(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """A host shell that runs `echo WORD` (prints WORD) and records the commands."""
    commands: list[str] = []

    async def run(self: HostShell, command: str, *, timeout_s: float, max_chars: int) -> Any:
        commands.append(command)
        output = command.removeprefix("echo ").strip() + "\n"
        return ShellResult(exit_code=0, output=output, duration_s=0.01, output_bytes=len(output))

    monkeypatch.setattr(HostShell, "run", run)
    return commands


def word_of(body: dict[str, Any]) -> str:
    text = json.dumps(body["messages"])
    match = re.search(r"echo (check-[0-9a-f]+)", text)
    assert match, text
    return match[1]


def run_test(client: TestClient, profile: str = "main", **body: Any) -> dict[str, Any]:
    payload = {"base_url": LLM_URL, "model": "fake", **body}
    response = client.post(f"/api/settings/test-llm?profile={profile}", json=payload)
    assert response.status_code == 200, response.text
    return response.json()


def test_main_passes_on_a_real_bash_call_made_like_a_chat_turn(
    bootstrap: Bootstrap, echo_shell: list[str]
) -> None:
    llm = FakeLLM(
        lambda body: call("bash", {"command": f"echo {word_of(body)}"}),
        lambda body: say(word_of(body)),
    )
    with TestClient(make_app(bootstrap, llm)) as client:
        result = run_test(client)
    assert result["ok"] is True and result["tool_call"] is True, result
    assert re.fullmatch(r"check-[0-9a-f]+ \(printed by bash\)", result["reply"])
    assert echo_shell == [f"echo {result['reply'].split()[0]}"]
    first = llm.requests[0]
    # as a chat turn: streamed, the chat's instructions and tools, nothing forced
    assert first["stream"] is True
    assert "assistant of an AI server monitor" in first["messages"][0]["content"]
    assert [t["function"]["name"] for t in first["tools"]][: len(CHAT_TOOLS)] == CHAT_TOOLS
    assert "temperature" not in first and "reasoning_effort" not in first


@pytest.mark.parametrize(
    "reply",
    ['bash[ARGS]{"command": "echo check"}', "bash {\n  echo check\n}", "All good, nothing to do."],
)
def test_main_fails_when_the_model_answers_with_text(
    bootstrap: Bootstrap, echo_shell: list[str], reply: str
) -> None:
    llm = FakeLLM(say(reply))
    with TestClient(make_app(bootstrap, llm)) as client:
        result = run_test(client)
    assert result["ok"] is False and result["error"] == TEXT_INSTEAD
    assert result["reply"] == reply[:200] and echo_shell == []


def test_other_chat_tools_are_offered_but_do_nothing(
    bootstrap: Bootstrap, echo_shell: list[str]
) -> None:
    llm = FakeLLM(
        call("job_delete", {"slug": "docker:status"}),
        lambda body: call("bash", {"command": f"echo {word_of(body)}"}),
        say("done"),
    )
    with TestClient(make_app(bootstrap, llm)) as client:
        result = run_test(client)
        assert client.get("/api/jobs/docker:status").status_code == 200  # not deleted
    assert result["ok"] is True
    assert "Not available in the connection test" in json.dumps(llm.requests[1]["messages"])


def test_worker_passes_like_a_topic_job(bootstrap: Bootstrap, echo_shell: list[str]) -> None:
    llm = FakeLLM(
        lambda body: call("bash", {"command": f"echo {word_of(body)}"}),
        call("final_result", {"status": "ok", "summary": "printed it", "findings": []}),
    )
    with TestClient(make_app(bootstrap, llm, worker={"model": "small"})) as client:
        result = run_test(client, "worker", model="small")
    assert result["ok"] is True and result["tool_call"] is True, result
    first = llm.requests[0]
    assert first["stream"] is True and first["model"] == "small"
    assert sorted(t["function"]["name"] for t in first["tools"]) == ["bash", "final_result"]


def test_worker_fails_when_it_answers_with_text(
    bootstrap: Bootstrap, echo_shell: list[str]
) -> None:
    llm = FakeLLM(*[say('bash[ARGS]{"command": "echo x"}')] * 6)
    with TestClient(make_app(bootstrap, llm, worker={"model": "small"})) as client:
        result = run_test(client, "worker", model="small")
    assert result["ok"] is False and result["error"].startswith(TEXT_INSTEAD)
    assert echo_shell == []


def _app(bootstrap: Bootstrap, handler: Any) -> Any:
    settings = Settings.model_validate(
        {"llm": {"base_url": LLM_URL, "model": "fake", "api_key": "sk-test"}}
    )
    return create_app(bootstrap, llm_transport=httpx.MockTransport(handler), seed_settings=settings)


def test_a_model_missing_from_the_list_is_reported_before_any_chat(
    bootstrap: Bootstrap,
) -> None:
    chats: list[Any] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            data = [{"id": m, "object": "model"} for m in ("a", "b")]
            return httpx.Response(200, json={"object": "list", "data": data})
        chats.append(request)
        return httpx.Response(500)

    with TestClient(_app(bootstrap, handler)) as client:
        result = run_test(client, model="gone")
    assert result["ok"] is False and "no model named 'gone'" in result["error"]
    assert result["model_listed"] is False and result["models"] == ["a", "b"]
    assert chats == []


def test_an_unreachable_endpoint_is_reported(bootstrap: Bootstrap) -> None:
    def refuse(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused", request=request)

    with TestClient(_app(bootstrap, refuse)) as client:
        result = run_test(client)
    assert result["ok"] is False and result["error"].startswith("Could not reach")
    assert LLM_URL in result["error"]
