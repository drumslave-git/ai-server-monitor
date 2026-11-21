"""The optional Docker Hub login of Settings → General: the image check (the job-only tool
`docker_image_check`, run by the backend) sends it to the Docker daemon as `X-Registry-Auth`, and
it reaches nothing else: not the model, not the stored runs, not the trace, not the logs, not the
settings API, not the environment or the arguments of any process the model's shell spawns.
Invented credentials only."""

from __future__ import annotations

import asyncio
import json
import logging
import os
from typing import Any

import pytest
from fastapi.testclient import TestClient

from ai_server_monitor.config import SECRET_MASK, Bootstrap
from ai_server_monitor.jobs import runner
from ai_server_monitor.jobs.images import registry_auth

from .jobfakes import ScriptedModels, finish, tool
from .test_docker_updates import Daemon
from .test_jobs import make_app, run

USER = "invented-user"
TOKEN = "dckr_pat_inventedtoken0123"
HEADER = registry_auth(USER, TOKEN)
LOGIN = {"general": {"docker_hub_user": USER, "docker_hub_token": TOKEN}}
NEWER = "newer    invented/cache:2 | local: invented/cache@sha256:bbb1 | registry: sha256:ccc2"
FINDING = {
    "severity": "info",
    "title": "Newer image: invented/cache:2",
    "detail": "the registry has another digest",
    "evidence": [NEWER],
}


@pytest.fixture
def daemon(monkeypatch: pytest.MonkeyPatch) -> Daemon:
    """The job's Docker API client talks to a fake daemon that keeps its requests."""
    fake = Daemon(["invented/cache:2", "registry.example.invalid/team/app:1.0"])
    monkeypatch.setattr(runner, "daemon_client", lambda *args: fake.client())
    return fake


def hub_header(daemon: Daemon, image: str) -> str | None:
    [request] = [r for r in daemon.distribution() if f"/distribution/{image}/" in r.url.path]
    return request.headers.get("x-registry-auth")


def test_the_login_goes_to_the_daemon_for_docker_hub_images_only(
    bootstrap: Bootstrap, models: ScriptedModels, daemon: Daemon
) -> None:
    models.script = [tool("docker_image_check"), finish("ok", "One newer image.", [FINDING])]
    with TestClient(make_app(bootstrap, **LOGIN)) as client:
        result = run(client, "docker:updates")
    assert result["status"] == "ok", result
    assert hub_header(daemon, "invented/cache:2") == HEADER
    assert hub_header(daemon, "registry.example.invalid/team/app:1.0") is None
    # the model reads the user name (no secret) and the result lines, nothing of the token
    seen = models.requests[-1]["text"]
    assert f"docker hub: logged in as {USER}" in seen and NEWER in seen


@pytest.mark.parametrize(
    "general",
    [
        {},
        {"docker_hub_user": USER},
        {"docker_hub_token": TOKEN},
    ],
)
def test_without_both_user_and_token_the_daemon_is_asked_anonymously(
    bootstrap: Bootstrap, models: ScriptedModels, daemon: Daemon, general: dict[str, str]
) -> None:
    models.script = [tool("docker_image_check"), finish("ok", "One newer image.", [FINDING])]
    with TestClient(make_app(bootstrap, general=general)) as client:
        assert run(client, "docker:updates")["status"] == "ok"
    assert all("x-registry-auth" not in r.headers for r in daemon.requests)
    assert "docker hub: anonymous" in models.requests[-1]["text"]


def test_only_the_image_update_job_has_the_tool(
    bootstrap: Bootstrap, models: ScriptedModels, daemon: Daemon, shell: list[str]
) -> None:
    models.script = [tool("bash", command="docker ps"), finish("ok", "Fine.", [])]
    with TestClient(make_app(bootstrap, **LOGIN)) as client:
        run(client, "docker:status")
        assert "docker_image_check" not in models.requests[0]["tools"]
        models.script = [tool("docker_image_check"), finish("ok", "One newer image.", [FINDING])]
        run(client, "docker:updates")
    assert {"bash", "docker_image_check"} <= set(models.requests[-1]["tools"])


def everything_stored(client: TestClient) -> str:
    ctx = client.app.state.ctx  # type: ignore[attr-defined]

    async def dump() -> str:
        await ctx.tracing.flush()
        rows = []
        for table in ("llm_calls", "llm_tool_calls", "app_log", "job_runs"):
            rows += [dict(r) for r in await ctx.db.fetchall(f"SELECT * FROM {table}")]
        return json.dumps(rows, default=str)

    return client.portal.call(dump)  # type: ignore[union-attr,no-any-return]


def assert_nowhere(text: str) -> None:
    for secret in (TOKEN, HEADER):
        assert secret not in text


def test_the_token_never_reaches_the_model_the_stored_runs_the_trace_the_logs_or_the_api(
    bootstrap: Bootstrap,
    models: ScriptedModels,
    daemon: Daemon,
    caplog: pytest.LogCaptureFixture,
) -> None:
    # At the "debug" level: the aiosqlite logger would print every SQL statement with its
    # values, the settings write (every secret in clear) included; db.py keeps it quiet.
    caplog.set_level(logging.DEBUG)
    models.script = [tool("docker_image_check"), finish("ok", "One newer image.", [FINDING])]
    with TestClient(make_app(bootstrap, **LOGIN)) as client:
        result = run(client, "docker:updates")
        assert result["status"] == "ok", result
        assert hub_header(daemon, "invented/cache:2") == HEADER  # the daemon got it ...

        # ... and the model never saw it: its requests (prompt, tool result), instructions
        assert models.requests
        for request in models.requests:
            assert_nowhere(request["text"] + request["instructions"])

        # the stored run and the job, through the API
        run_doc = client.get(f"/api/jobs/runs/{result['id']}").json()
        for doc in (result, run_doc, client.get("/api/jobs/docker:updates").json()):
            assert_nowhere(json.dumps(doc))

        # the settings API shows the user and masks the token
        values = client.get("/api/settings").json()["values"]["general"]
        assert values["docker_hub_user"] == USER and values["docker_hub_token"] == SECRET_MASK
        assert_nowhere(client.get("/api/settings").text)

        # the trace tables and the app log (stored and in memory)
        stored = everything_stored(client)
        assert "docker:updates" in stored
        assert_nowhere(stored)
        assert_nowhere(client.get("/api/logs/app").text)
    assert_nowhere(caplog.text)
    assert "aiosqlite" not in {r.name for r in caplog.records}


@pytest.mark.skipif(
    os.name != "posix" or os.geteuid() == 0, reason="runs the model's bash directly, as non-root"
)
def test_the_models_shell_and_every_process_it_spawns_have_no_trace_of_the_login(
    bootstrap: Bootstrap,
    models: ScriptedModels,
    daemon: Daemon,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The model asks the real bash for its environment and the process list; the arguments and
    the environment of every process spawned during the run are recorded and searched too."""
    spawned: list[str] = []
    real_spawn = asyncio.create_subprocess_exec

    async def spawn(*args: Any, **kwargs: Any) -> Any:
        spawned.append(json.dumps([args, kwargs.get("env")], default=str))
        return await real_spawn(*args, **kwargs)

    monkeypatch.setattr(asyncio, "create_subprocess_exec", spawn)
    models.script = [
        tool("docker_image_check"),
        tool("bash", command="env; ps -eo args 2>/dev/null; cat /proc/$$/environ | tr '\\0' '\\n'"),
        finish("ok", "One newer image.", [FINDING]),
    ]
    with TestClient(make_app(bootstrap, **LOGIN)) as client:
        result = run(client, "docker:updates")
    assert result["status"] == "ok", result
    assert hub_header(daemon, "invented/cache:2") == HEADER
    assert spawned, "the model's bash was never started"
    for process in spawned:
        assert_nowhere(process)
        assert "DOCKER_HUB" not in process
    # what the model's bash printed (env, process list) came back to it: no trace there either
    shell_output = next(r["text"] for r in models.requests if "PATH=" in r["text"])
    assert_nowhere(shell_output)
    assert "DOCKER_HUB" not in shell_output
