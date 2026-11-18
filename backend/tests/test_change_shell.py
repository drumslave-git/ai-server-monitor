"""Approved changing commands: the chat's `bash_write`, the approval flow and the chat that
continues by itself once the owner decided."""

from __future__ import annotations

import time
from typing import Any

import pytest
from fastapi.testclient import TestClient

from ai_server_monitor.config import Bootstrap

from .jobfakes import ScriptedModels, finish, tool
from .test_chat import ask
from .test_jobs import make_app

COMMAND = "systemctl restart nginx"


def wait_for_answer(client: TestClient, session_id: str, text: str) -> list[dict[str, Any]]:
    """The session's messages once the continued answer is there."""
    deadline = time.monotonic() + 10
    while True:
        messages = client.get(f"/api/chat/sessions/{session_id}").json()["messages"]
        if messages[-1]["role"] == "assistant" and messages[-1]["content"] == text:
            return messages
        assert time.monotonic() < deadline, messages[-1]
        time.sleep(0.05)


def ask_for_restart(client: TestClient, models: ScriptedModels) -> list[dict[str, Any]]:
    models.script = [tool("bash_write", command=COMMAND, reason="nginx hangs")]
    return ask(client, "please restart nginx")


def test_approved_command_runs_and_the_chat_continues_by_itself(
    bootstrap: Bootstrap, models: ScriptedModels, root_shell: list[str]
) -> None:
    app = make_app(bootstrap)
    with TestClient(app) as client:
        sub = app.state.ctx.events.subscribe("chat.message")
        events = ask_for_restart(client, models)
        assert "bash_write" in models.requests[0]["tools"]
        call = next(e for e in events if e["type"] == "tool_call")
        assert call["kind"] == "write" and call["args"]["command"] == COMMAND
        done = events[-1]
        assert done["text"].startswith("Waiting for approval #") and COMMAND in done["text"]
        assert root_shell == []  # nothing ran yet
        [batch] = client.get("/api/approvals").json()["items"]
        [item] = client.get("/api/actions", params={"batch": batch["id"]}).json()["items"]
        assert item["action"] == "host_command" and item["status"] == "pending"
        assert item["args"]["command"] == COMMAND and item["reason"] == "nginx hangs"
        assert item["source"] == f"chat:{done['session_id']}"

        models.script = ["Restarted: nginx is running again."]
        decided = client.post(f"/api/approvals/{batch['id']}", json={"decision": "approve"})
        assert decided.json()["counts"] == {"executed": 1}
        assert root_shell == [COMMAND]
        messages = wait_for_answer(client, done["session_id"], "Restarted: nginx is running again.")
        # the model got the command's output as the tool result, then answered
        assert "nginx restarted" in models.requests[-1]["text"]
        assert "exit code 0" in models.requests[-1]["text"]
        assert messages[-1]["meta"]["continued"] is True
        event = client.portal.call(sub.__anext__)  # type: ignore[union-attr]
        assert event.data["session_id"] == done["session_id"]
        assert event.data["text"] == "Restarted: nginx is running again."
        # the permanent audit row: command, reason, exit code, output
        [row] = client.get("/api/actions", params={"action": "host_command"}).json()["items"]
        assert row["status"] == "executed" and row["result"]["exit_code"] == 0
        assert row["result"]["output"] == "nginx restarted\n"


@pytest.mark.parametrize("outcome", ["reject", "expire"])
def test_a_rejected_or_expired_command_is_reported_back(
    bootstrap: Bootstrap, models: ScriptedModels, root_shell: list[str], outcome: str
) -> None:
    app = make_app(bootstrap)
    with TestClient(app) as client:
        done = ask_for_restart(client, models)[-1]
        [batch] = client.get("/api/approvals").json()["items"]
        models.script = ["Understood, nginx was not restarted."]
        if outcome == "reject":
            client.post(f"/api/approvals/{batch['id']}", json={"decision": "reject"})
            told = "rejected"
        else:
            ctx = app.state.ctx

            async def expire() -> None:
                await ctx.db.execute("UPDATE actions SET expires_at = 1 WHERE batch_id = ?",
                                     (batch["id"],))  # fmt: skip
                await ctx.actions.sweep_expired()

            client.portal.call(expire)  # type: ignore[union-attr]
            told = "expired"
        wait_for_answer(client, done["session_id"], "Understood, nginx was not restarted.")
        assert root_shell == []
        assert told in models.requests[-1]["text"]
        [row] = client.get("/api/actions", params={"action": "host_command"}).json()["items"]
        assert row["status"] == ("rejected" if outcome == "reject" else "expired")


def test_auto_policy_runs_at_once_in_the_same_answer(
    bootstrap: Bootstrap, models: ScriptedModels, root_shell: list[str]
) -> None:
    app = make_app(bootstrap, approvals={"shell_changes": "auto"})
    with TestClient(app) as client:
        models.script = [
            tool("bash_write", command=COMMAND, reason="nginx hangs"),
            "Restarted.",
        ]
        events = ask(client, "restart nginx, no need to ask")
        assert root_shell == [COMMAND] and events[-1]["text"] == "Restarted."
        [result] = [e for e in events if e["type"] == "tool_result"]
        assert result["ok"] and "nginx restarted" in result["preview"]
        assert client.get("/api/approvals").json()["items"] == []
        [row] = client.get("/api/actions", params={"action": "host_command"}).json()["items"]
        assert row["status"] == "executed" and row["policy"] == "auto"


def test_policy_off_or_no_capabilities_means_no_tool_and_the_status_says_why(
    bootstrap: Bootstrap, models: ScriptedModels
) -> None:
    # no capabilities: the real probe fails here (not root / not Linux)
    with TestClient(make_app(bootstrap)) as client:
        models.script = ["ok"]
        ask(client, "hi")
        assert "bash_write" not in models.requests[-1]["tools"]
        status = client.get("/api/status").json()["change_shell"]
        assert status["policy"] == "approve" and not status["available"]
        assert not status["offered"] and status["reason"]


def test_policy_off_hides_the_tool_even_when_it_would_work(
    bootstrap: Bootstrap, models: ScriptedModels, root_shell: list[str]
) -> None:
    with TestClient(make_app(bootstrap, approvals={"shell_changes": "off"})) as client:
        models.script = ["ok"]
        ask(client, "hi")
        assert "bash_write" not in models.requests[-1]["tools"]
        status = client.get("/api/status").json()["change_shell"]
        assert status["available"] and not status["offered"]


def test_never_offered_to_jobs_or_the_worker(
    bootstrap: Bootstrap, models: ScriptedModels, root_shell: list[str], shell: list[str]
) -> None:
    app = make_app(bootstrap, approvals={"shell_changes": "auto"})
    with TestClient(app) as client:
        models.script = [finish("ok", "Fine.")]
        client.portal.call(app.state.ctx.jobs.run, "summary", "manual")  # type: ignore[union-attr]
        assert "bash_write" not in models.requests[-1]["tools"]
        models.script = [tool("delegate", task="look around"), "found nothing", "All fine."]
        ask(client, "have a look")
        main, worker, _ = models.requests[-3:]
        assert "bash_write" in main["tools"] and worker["tools"] == ["bash"]
