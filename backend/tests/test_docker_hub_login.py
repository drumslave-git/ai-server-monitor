"""The optional Docker Hub login of Settings → General: a job run's shell gets it, the chat's shell
does not, and the token reaches neither the model, nor the stored runs, nor the trace, nor the
logs, nor the settings API. Invented credentials only."""

from __future__ import annotations

import json
import logging
from typing import Any

import pytest
from fastapi.testclient import TestClient

from ai_server_monitor.agent.shell import HostShell, ShellResult
from ai_server_monitor.agent.tools import app_shell
from ai_server_monitor.config import SECRET_MASK, Bootstrap

from .jobfakes import ScriptedModels, finish, tool
from .test_jobs import make_app, run

USER = "invented-user"
TOKEN = "dckr_pat_inventedtoken0123"
LOGIN = {"general": {"docker_hub_user": USER, "docker_hub_token": TOKEN}}


@pytest.fixture
def seen_envs(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, str]]:
    """A shell that runs nothing: it keeps the environment each command would have had and
    prints it as the real one would for `echo`, the token in clear."""
    envs: list[dict[str, str]] = []

    async def fake_run(self: HostShell, command: str, *, timeout_s: float, max_chars: int) -> Any:
        env = self.environment()
        envs.append(env)
        token, user = env.get("DOCKER_HUB_TOKEN", ""), env.get("DOCKER_HUB_USER", "")
        output = f"hub token: {token}\nhub user: {user}\n"
        return ShellResult(exit_code=0, output=output, duration_s=0.01, output_bytes=len(output))

    monkeypatch.setattr(HostShell, "run", fake_run)
    return envs


def test_a_job_shell_has_the_login_and_the_chat_shell_does_not(bootstrap: Bootstrap) -> None:
    with TestClient(make_app(bootstrap, **LOGIN)) as client:
        ctx = client.app.state.ctx  # type: ignore[attr-defined]
        job = app_shell(ctx, docker_hub_login=True).environment()
        assert (job["DOCKER_HUB_USER"], job["DOCKER_HUB_TOKEN"]) == (USER, TOKEN)
        chat = app_shell(ctx).environment()
        assert not any(k.startswith("DOCKER_HUB") for k in chat)


@pytest.mark.parametrize(
    "general",
    [
        {},
        {"docker_hub_user": USER},
        {"docker_hub_token": TOKEN},
    ],
)
def test_without_both_user_and_token_the_shell_has_no_login(
    bootstrap: Bootstrap, general: dict[str, str]
) -> None:
    with TestClient(make_app(bootstrap, general=general)) as client:
        env = app_shell(client.app.state.ctx, docker_hub_login=True).environment()  # type: ignore[attr-defined]
    assert not any(k.startswith("DOCKER_HUB") for k in env)


def test_the_token_never_reaches_the_model_the_stored_runs_the_trace_the_logs_or_the_api(
    bootstrap: Bootstrap,
    models: ScriptedModels,
    seen_envs: list[dict[str, str]],
    caplog: pytest.LogCaptureFixture,
) -> None:
    # The default log level. (At "debug" aiosqlite logs every SQL statement with its values,
    # the settings write included: that holds for every secret in Settings, not only this one.)
    caplog.set_level(logging.INFO)
    finding = {
        "severity": "info",
        "title": "Newer image: invented/cache:2",
        "detail": "the registry has another digest",
        "evidence": ["hub user: invented-user"],
    }
    models.script = [
        tool(
            "bash", command='echo "hub token: $DOCKER_HUB_TOKEN"; echo "hub user: $DOCKER_HUB_USER"'
        ),
        finish("ok", "One newer image.", [finding]),
    ]
    app = make_app(bootstrap, **LOGIN)
    with TestClient(app) as client:
        result = run(client, "docker:updates")
        assert result["status"] == "ok", result
        assert seen_envs[0]["DOCKER_HUB_TOKEN"] == TOKEN  # the job's shell has it ...

        # ... and the model never saw it: its requests (prompt, tool result), instructions
        assert models.requests
        for request in models.requests:
            assert TOKEN not in request["text"] and TOKEN not in request["instructions"]
        assert "[redacted]" in models.requests[-1]["text"]
        assert USER in models.requests[-1]["text"]  # the user name is no secret

        # the stored run and the job, through the API
        run_doc = client.get(f"/api/jobs/runs/{result['id']}").json()
        for doc in (result, run_doc, client.get("/api/jobs/docker:updates").json()):
            assert TOKEN not in json.dumps(doc)

        # the settings API shows the user and masks the token
        values = client.get("/api/settings").json()["values"]["general"]
        assert values["docker_hub_user"] == USER and values["docker_hub_token"] == SECRET_MASK
        assert TOKEN not in client.get("/api/settings").text

        # the trace tables and the app log (stored and in memory)
        ctx = client.app.state.ctx  # type: ignore[attr-defined]

        async def dump() -> str:
            await ctx.tracing.flush()
            rows = []
            for table in ("llm_calls", "llm_tool_calls", "app_log", "job_runs"):
                rows += [dict(r) for r in await ctx.db.fetchall(f"SELECT * FROM {table}")]
            return json.dumps(rows, default=str)

        stored = client.portal.call(dump)  # type: ignore[union-attr]
        assert "docker:updates" in stored and TOKEN not in stored
        assert TOKEN not in client.get("/api/logs/app").text
        assert TOKEN not in caplog.text
