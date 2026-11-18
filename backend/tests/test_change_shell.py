"""Approved changing commands: the chat's `bash_write`, the approval flow and the chat that
continues by itself once the owner decided."""

from __future__ import annotations

import asyncio
import threading
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
        assert decided.json()["counts"] in ({"approved": 1}, {"executed": 1})
        messages = wait_for_answer(client, done["session_id"], "Restarted: nginx is running again.")
        assert root_shell == [COMMAND]
        # the model got the command's output as the tool result, then answered
        assert "nginx restarted" in models.requests[-1]["text"]
        assert "exit code 0" in models.requests[-1]["text"]
        assert messages[-1]["meta"]["continued"] is True
        # "chat.message" also matches "chat.message.updated" (the note, published first)
        updated = client.portal.call(sub.__anext__)  # type: ignore[union-attr]
        assert updated.type == "chat.message.updated"
        event = client.portal.call(sub.__anext__)  # type: ignore[union-attr]
        assert event.type == "chat.message" and event.data["session_id"] == done["session_id"]
        assert event.data["text"] == "Restarted: nginx is running again."
        # the permanent audit row: command, reason, exit code, output
        [row] = client.get("/api/actions", params={"action": "host_command"}).json()["items"]
        assert row["status"] == "executed" and row["result"]["exit_code"] == 0
        assert row["result"]["output"] == "nginx restarted\n"
        # one action by id, the same shape as in the list; 404 for an unknown id
        assert client.get(f"/api/actions/{row['id']}").json() == row
        assert client.get("/api/actions/99999").status_code == 404
        # the approval's title is short; the command stays in the item's arguments
        approval = client.get(f"/api/approvals/{batch['id']}").json()
        assert approval["summary"] == "command on the host"
        assert COMMAND not in approval["title"]
        # the waiting note was updated to the outcome, and an open page was told
        note = next(m for m in messages if m["id"] == done["message_id"])
        assert (
            note["content"] == f"Approval #{batch['id']}: {COMMAND}: approved and run, exit code 0."
        )
        assert note["meta"]["outcome"] == {str(row["id"]): "executed"}
        assert note["meta"]["waiting_for"] == [row["id"]]
        assert updated.data["message_id"] == done["message_id"]
        assert updated.data["text"] == note["content"]


def test_the_approve_call_returns_before_a_long_command_has_finished(
    bootstrap: Bootstrap,
    models: ScriptedModels,
    root_shell: list[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from ai_server_monitor.agent.change_shell import ChangeShell
    from ai_server_monitor.agent.shell import ShellResult

    gate = threading.Event()

    async def slow_run(self: ChangeShell, command: str, *, timeout_s: float, max_chars: int) -> Any:
        while not gate.is_set():
            await asyncio.sleep(0.01)
        return ShellResult(exit_code=0, output="done\n", duration_s=60.0)

    monkeypatch.setattr(ChangeShell, "run", slow_run)
    app = make_app(bootstrap)
    with TestClient(app) as client:
        done = ask_for_restart(client, models)[-1]
        [batch] = client.get("/api/approvals").json()["items"]
        models.script = ["Restarted after a minute."]
        decided = client.post(f"/api/approvals/{batch['id']}", json={"decision": "approve"})
        assert decided.status_code == 200 and decided.json()["counts"] == {"approved": 1}
        gate.set()
        wait_for_answer(client, done["session_id"], "Restarted after a minute.")
        [row] = client.get("/api/actions", params={"action": "host_command"}).json()["items"]
        assert row["status"] == "executed" and row["result"]["exit_code"] == 0
        assert row["result"]["output"] == "done\n"


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
        messages = wait_for_answer(
            client, done["session_id"], "Understood, nginx was not restarted."
        )
        assert root_shell == []
        assert told in models.requests[-1]["text"]
        [row] = client.get("/api/actions", params={"action": "host_command"}).json()["items"]
        assert row["status"] == ("rejected" if outcome == "reject" else "expired")
        # the stored waiting note now says what happened
        note = next(m for m in messages if m["id"] == done["message_id"])
        assert note["content"] == (
            f"Approval #{batch['id']}: {COMMAND}: "
            + ("rejected." if outcome == "reject" else "expired without a decision.")
        )
        assert note["meta"]["outcome"] == {str(row["id"]): row["status"]}


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


def test_the_missing_capabilities_reason_names_the_compose_lines() -> None:
    from ai_server_monitor.agent.change_shell import COMPOSE_HINT

    assert "`- SYS_ADMIN`" in COMPOSE_HINT and "`- SYS_PTRACE`" in COMPOSE_HINT
    assert "`cap_add:`" in COMPOSE_HINT and "docker compose up -d" in COMPOSE_HINT


def test_a_command_running_at_a_stop_is_closed_as_failed_and_the_chat_is_told(
    bootstrap: Bootstrap, models: ScriptedModels, root_shell: list[str]
) -> None:
    from ai_server_monitor.actions.service import INTERRUPTED

    with TestClient(make_app(bootstrap)) as client:
        done = ask_for_restart(client, models)[-1]
        [batch] = client.get("/api/approvals").json()["items"]
        ctx = client.app.state.ctx  # type: ignore[attr-defined]
        # approved and running when the process dies: the row stays "approved"
        client.portal.call(  # type: ignore[union-attr]
            ctx.db.execute, "UPDATE actions SET status = 'approved' WHERE batch_id = ?",
            (batch["id"],),
        )  # fmt: skip
    models.script = ["The restart was interrupted; please check nginx."]
    with TestClient(make_app(bootstrap)) as client:
        messages = wait_for_answer(
            client, done["session_id"], "The restart was interrupted; please check nginx."
        )
        [row] = client.get("/api/actions", params={"action": "host_command"}).json()["items"]
        assert row["status"] == "failed" and row["result"]["error"] == INTERRUPTED
        assert INTERRUPTED in models.requests[-1]["text"]
        note = next(m for m in messages if m["id"] == done["message_id"])
        assert note["content"].endswith(f"approved, but it failed ({INTERRUPTED}).")
        assert root_shell == []  # not run again
