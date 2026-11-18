"""The Run button on commands in chat answers: the click runs the command (read-only shell when
the answer marked it read-only, else the change shell as an approved `bash_write`), stores the
result in the chat and the model answers it at once."""

from __future__ import annotations

import json
from typing import Any

import pytest
from fastapi.testclient import TestClient

from ai_server_monitor.agent.shell import HostShell, ShellResult
from ai_server_monitor.config import Bootstrap

from .jobfakes import ScriptedModels
from .test_jobs import make_app

READ_ONLY = "# [read-only] how full is /\ndf -h /"


def frames(text: str) -> list[dict[str, Any]]:
    events = []
    for frame in text.strip().split("\n\n"):
        lines = dict(line.split(": ", 1) for line in frame.splitlines() if ": " in line)
        events.append({"type": lines["event"], **json.loads(lines["data"])})
    return events


def ask(client: TestClient, message: str) -> str:
    response = client.post("/api/chat", json={"message": message})
    assert response.status_code == 200, response.text
    return frames(response.text)[0]["session_id"]


def press(client: TestClient, session_id: str, command: str) -> list[dict[str, Any]]:
    response = client.post(f"/api/chat/sessions/{session_id}/run", json={"command": command})
    assert response.status_code == 200, response.text
    return frames(response.text)


def test_a_read_only_command_runs_sandboxed_and_the_model_answers_at_once(
    bootstrap: Bootstrap, models: ScriptedModels, shell: list[str], root_shell: list[str]
) -> None:
    models.script = ["Run the df command below.", "/ is at 40%: fine."]
    with TestClient(make_app(bootstrap)) as client:
        session_id = ask(client, "how full is /?")
        events = press(client, session_id, READ_ONLY)
        run = events[0]
        assert run["type"] == "run" and run["mode"] == "read-only" and run["exit_code"] == 0
        assert "/dev/sda1  100G  40G  60G  40% /" in run["output"]
        assert shell == [READ_ONLY] and root_shell == []  # the sandbox, not the change shell
        # The model answers the result right away, in the same stream.
        assert [e["type"] for e in events][-1] == "done"
        assert events[-1]["text"] == "/ is at 40%: fine."
        seen = models.requests[-1]["text"]
        assert "I ran this command with the Run button" in seen and "40% /" in seen
        messages = client.get(f"/api/chat/sessions/{session_id}").json()["messages"]
        assert messages[-2]["role"] == "user" and messages[-2]["meta"]["run"]["mode"] == "read-only"
        assert messages[-1]["content"] == "/ is at 40%: fine."
        audit = client.get("/api/chat/audit", params={"kind": "shell"}).json()["items"]
        assert audit[0]["by"] == "user" and audit[0]["session"] == session_id
        assert client.get("/api/actions").json()["items"] == []  # nothing as root


@pytest.mark.parametrize("command", ["# [write] mark a test file\ntouch /tmp/asm-run", "uptime"])
def test_a_write_or_unmarked_command_runs_as_an_approved_host_command(
    bootstrap: Bootstrap,
    models: ScriptedModels,
    shell: list[str],
    root_shell: list[str],
    command: str,
) -> None:
    models.script = ["ok", "Done: it ran as root."]
    with TestClient(make_app(bootstrap)) as client:
        session_id = ask(client, "hi")
        events = press(client, session_id, command)
        run = events[0]
        assert run["mode"] == "write" and run["exit_code"] == 0 and run["action_id"]
        assert run["output"].strip() == "nginx restarted"
        assert root_shell == [command] and shell == []
        assert events[-1]["type"] == "done" and events[-1]["text"] == "Done: it ran as root."
        action = client.get(f"/api/actions/{run['action_id']}").json()
        assert action["action"] == "host_command" and action["status"] == "executed"
        assert action["decided_by"] == "user:run-button"
        assert action["source"] == f"user:run-button:{session_id}"
        assert "ran as root" in models.requests[-1]["text"]


@pytest.fixture
def failing(monkeypatch: pytest.MonkeyPatch) -> None:
    async def run(self: HostShell, command: str, *, timeout_s: float, max_chars: int) -> Any:
        output = "ls: cannot access '/nope': No such file or directory\n"
        return ShellResult(exit_code=2, output=output, duration_s=0.02, output_bytes=len(output))

    monkeypatch.setattr(HostShell, "run", run)


@pytest.mark.usefixtures("failing")
def test_a_failing_command_is_stored_with_its_exit_code(
    bootstrap: Bootstrap, models: ScriptedModels
) -> None:
    models.script = ["ok", "It does not exist."]
    with TestClient(make_app(bootstrap)) as client:
        session_id = ask(client, "hi")
        run = press(client, session_id, "# [read-only] list it\nls /nope")[0]
        assert run["exit_code"] == 2 and "No such file" in run["output"]
        stored = client.get(f"/api/chat/sessions/{session_id}").json()["messages"][-2]
        assert stored["meta"]["run"]["exit_code"] == 2 and "exit code 2" in stored["content"]


def test_running_in_a_missing_session_is_404(bootstrap: Bootstrap) -> None:
    with TestClient(make_app(bootstrap)) as client:
        response = client.post("/api/chat/sessions/nope/run", json={"command": "uptime"})
        assert response.status_code == 404
        empty = client.post("/api/chat/sessions/nope/run", json={"command": ""})
        assert empty.status_code == 422


def test_with_changing_commands_off_the_card_says_why_it_did_not_run(
    bootstrap: Bootstrap, models: ScriptedModels, root_shell: list[str]
) -> None:
    models.script = ["ok", "It did not run: changes are off."]
    with TestClient(make_app(bootstrap, approvals={"shell_changes": "off"})) as client:
        session_id = ask(client, "hi")
        run = press(client, session_id, "touch /tmp/asm-run")[0]
        assert root_shell == [] and run["exit_code"] is None and run["stopped"] == "error"
        assert run["output"] == "policy for this action is off"
