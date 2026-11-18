"""The Run button on commands in chat answers: run in the read-only shell, stored in the chat."""

from __future__ import annotations

import json
from typing import Any

import pytest
from fastapi.testclient import TestClient

from ai_server_monitor.agent.shell import HostShell, ShellResult
from ai_server_monitor.config import Bootstrap

from .jobfakes import ScriptedModels
from .test_jobs import make_app


def ask(client: TestClient, message: str, session_id: str | None = None) -> str:
    response = client.post("/api/chat", json={"message": message, "session_id": session_id})
    assert response.status_code == 200, response.text
    frame = response.text.split("\n\n")[0]
    return json.loads(frame.split("data: ", 1)[1])["session_id"]


def test_a_run_is_stored_in_the_chat_and_the_next_question_sees_it(
    bootstrap: Bootstrap, models: ScriptedModels, shell: list[str]
) -> None:
    models.script = ["Run the df command below.", "It shows / at 40%."]
    with TestClient(make_app(bootstrap)) as client:
        session_id = ask(client, "how full is /?")
        asked = len(models.requests)
        response = client.post(f"/api/chat/sessions/{session_id}/run", json={"command": "df -h /"})
        assert response.status_code == 200, response.text
        run = response.json()
        assert run["exit_code"] == 0 and run["stopped"] is None and run["command"] == "df -h /"
        assert "/dev/sda1  100G  40G  60G  40% /" in run["output"]
        assert shell == ["df -h /"] and len(models.requests) == asked  # no model turn
        last = client.get(f"/api/chat/sessions/{session_id}").json()["messages"][-1]
        assert last["id"] == run["message_id"] and last["role"] == "user"
        assert last["meta"]["run"]["exit_code"] == 0
        audit = client.get("/api/chat/audit", params={"kind": "shell"}).json()["items"]
        assert audit[0]["by"] == "user" and audit[0]["cmd"] == "df -h /"
        assert audit[0]["session"] == session_id
        # The owner's next question: the model gets the run as data in the history.
        ask(client, "what does it mean?", session_id)
        seen = models.requests[-1]["text"]
        assert "I ran this command on the server myself" in seen
        assert 'trust=\\"untrusted\\"' in seen or 'trust="untrusted"' in seen
        assert "/dev/sda1  100G  40G  60G  40% /" in seen and "exit code 0" in seen


@pytest.fixture
def failing(monkeypatch: pytest.MonkeyPatch) -> None:
    async def run(self: HostShell, command: str, *, timeout_s: float, max_chars: int) -> Any:
        output = "touch: cannot touch '/etc/x': Read-only file system\n"
        return ShellResult(exit_code=1, output=output, duration_s=0.02, output_bytes=len(output))

    monkeypatch.setattr(HostShell, "run", run)


@pytest.mark.usefixtures("failing")
def test_a_failing_command_is_stored_with_its_exit_code(
    bootstrap: Bootstrap, models: ScriptedModels
) -> None:
    models.script = ["ok"]
    with TestClient(make_app(bootstrap)) as client:
        session_id = ask(client, "hi")
        run = client.post(f"/api/chat/sessions/{session_id}/run", json={"command": "touch /etc/x"})
        assert run.status_code == 200
        assert run.json()["exit_code"] == 1 and "Read-only file system" in run.json()["output"]
        stored = client.get(f"/api/chat/sessions/{session_id}").json()["messages"][-1]
        assert stored["meta"]["run"]["exit_code"] == 1
        assert "exit code 1" in stored["content"]


def test_running_in_a_missing_session_is_404(bootstrap: Bootstrap) -> None:
    with TestClient(make_app(bootstrap)) as client:
        response = client.post("/api/chat/sessions/nope/run", json={"command": "uptime"})
        assert response.status_code == 404
        empty = client.post("/api/chat/sessions/nope/run", json={"command": ""})
        assert empty.status_code == 422
