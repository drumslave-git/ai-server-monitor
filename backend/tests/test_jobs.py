"""Scheduled LLM jobs: builtins, runs through the agent loop, the API."""

from __future__ import annotations

import time
from datetime import timedelta
from typing import Any

import httpx
from fastapi.testclient import TestClient

from ai_server_monitor.config import Bootstrap, Settings
from ai_server_monitor.jobs.builtins import BUILTINS, BY_SLUG
from ai_server_monitor.main import create_app

from .conftest import LLM_URL
from .jobfakes import ScriptedModels, finish, tool

FINDING = {"severity": "warn", "title": "/ is 88% full", "detail": "12 GB free"}


def make_app(bootstrap: Bootstrap, **settings: Any) -> Any:
    seed = Settings.model_validate(
        {"llm": {"base_url": LLM_URL, "model": "big"}, "worker": {"model": "small"}, **settings}
    )
    return create_app(
        bootstrap,
        llm_transport=httpx.MockTransport(lambda r: httpx.Response(500)),
        seed_settings=seed,
    )


def run(client: TestClient, slug: str) -> dict[str, Any]:
    """Run a job to its end (as the scheduler and the chat do)."""
    return client.portal.call(client.app.state.ctx.jobs.run, slug, "manual")  # type: ignore[union-attr]


def test_builtins_are_seeded_with_their_schedules(bootstrap: Bootstrap) -> None:
    with TestClient(make_app(bootstrap)) as client:
        items = client.get("/api/jobs").json()["items"]
    by_slug = {j["slug"]: j for j in items}
    assert set(by_slug) == {d.slug for d in BUILTINS}
    assert {s: (j["interval_s"], j["cron"]) for s, j in by_slug.items()} == {
        "storage": (14400, None),
        "journal": (3600, None),
        "resources": (1800, None),
        "docker": (1800, None),
        "systemd": (3600, None),
        "updates": (None, "0 6 * * *"),
        "summary": (3600, None),
    }
    assert by_slug["summary"]["kind"] == "summary" and by_slug["summary"]["model"] == "main"
    assert all(j["model"] == "worker" for s, j in by_slug.items() if s != "summary")
    assert all(j["builtin"] and not j["edited"] and j["enabled"] for j in items)
    assert all(j["created_by"] == "builtin" and j["last_run"] is None for j in items)


def test_a_topic_run_uses_only_bash_and_stores_the_structured_result(
    bootstrap: Bootstrap, models: ScriptedModels, shell: list[str]
) -> None:
    models.script = [
        tool("bash", command="df -h /"),
        finish("warn", "Root is 88% full.", [FINDING]),
    ]
    with TestClient(make_app(bootstrap)) as client:
        sub = client.app.state.ctx.events.subscribe("job")  # type: ignore[attr-defined]
        result = run(client, "storage")
        assert result["status"] == "warn" and result["summary"] == "Root is 88% full."
        assert result["findings"] == [FINDING] and result["error"] is None
        assert result["trigger"] == "manual" and result["steps"] == 2
        assert result["model"] == "small" and result["trace_turn"]
        assert result["finished"] >= result["started"]
        # the topic run saw only bash, its own check, and ran the command in the shell
        assert [r["tools"] for r in models.requests] == [["bash"], ["bash"]]
        assert "Disk space and inodes" in models.requests[0]["instructions"]
        assert shell == ["df -h /"]
        # job commands stay out of the chat audit trail (they are in the run's trace)
        audit = client.get("/api/chat/audit", params={"kind": "shell"}).json()["items"]
        assert audit == []
        # listed with the job, and through the runs API
        job = client.get("/api/jobs/storage").json()
        assert job["last_run"]["id"] == result["id"] and job["last_run"]["status"] == "warn"
        runs = client.get("/api/jobs/storage/runs").json()["items"]
        assert [r["id"] for r in runs] == [result["id"]]
        assert client.get(f"/api/jobs/runs/{result['id']}").json()["findings"] == [FINDING]
        events = []
        while len(events) < 2:
            events.append(client.portal.call(sub.__anext__))  # type: ignore[union-attr]
        assert [e.type for e in events] == ["job.started", "job.finished"]
        assert events[1].data["status"] == "warn"


def test_failed_runs_are_stored_as_error(
    bootstrap: Bootstrap, models: ScriptedModels, shell: list[str]
) -> None:
    def broken(messages: Any, info: Any) -> Any:
        raise RuntimeError("model exploded")

    models.script = [broken]
    app = make_app(bootstrap)
    with TestClient(app) as client:
        failed = run(client, "docker")
        assert failed["status"] == "error" and "model exploded" in failed["error"]
        assert failed["finished"] is not None
        # a run over its time limit
        app.state.ctx.settings.jobs.timeout = timedelta(seconds=0.3)
        models.script = ["<sleep>"]
        slow = run(client, "docker")
        assert slow["status"] == "error" and "timed out" in slow["error"]
        # a model that never calls final_result
        app.state.ctx.settings.jobs.timeout = timedelta(minutes=5)
        models.script = ["I think everything is fine."] * 5
        chatty = run(client, "docker")
        assert chatty["status"] == "error" and chatty["error"]
        statuses = [r["status"] for r in client.get("/api/jobs/docker/runs").json()["items"]]
        assert statuses == ["error", "error", "error"]


def test_the_summary_gets_the_topic_results_and_sets_the_overall_status(
    bootstrap: Bootstrap, models: ScriptedModels, shell: list[str]
) -> None:
    with TestClient(make_app(bootstrap)) as client:
        models.script = [finish("crit", "Only 2% memory available.", [FINDING])]
        run(client, "resources")
        client.put("/api/jobs/docker", json={"enabled": False})
        models.script = [finish("crit", "Memory is nearly exhausted.\nDetails follow.", [FINDING])]
        summary = run(client, "summary")
        request = models.requests[-1]
        assert request["tools"] == ["bash", "delegate"]
        assert "Write the overall picture" in request["instructions"]
        text = request["text"]
        assert "Resources (resources, every 30 min)" in text and "Only 2% memory" in text
        assert "Storage (storage" in text and "no run yet" in text
        assert "Docker (docker" not in text  # disabled topics are left out
        assert summary["status"] == "crit" and summary["model"] == "big"
        status = client.get("/api/status").json()
        assert status["overall"]["status"] == "crit"
        assert status["overall"]["headline"] == "Memory is nearly exhausted."
        assert {j["slug"]: j["status"] for j in status["jobs"]}["resources"] == "crit"
        # the next summary sees the previous one
        models.script = [finish("ok", "Recovered.")]
        run(client, "summary")
        assert "Memory is nearly exhausted." in models.requests[-1]["text"]


def test_jobs_crud_through_the_api(bootstrap: Bootstrap) -> None:
    body = {
        "slug": "backups",
        "name": "Backups",
        "prompt": "Check that last night's restic backup finished.",
        "cron": "30  7 * * *",
    }
    with TestClient(make_app(bootstrap)) as client:
        created = client.post("/api/jobs", json=body)
        assert created.status_code == 201, created.text
        job = created.json()
        assert (job["cron"], job["interval_s"], job["kind"], job["model"]) == (
            "30 7 * * *",
            None,
            "topic",
            "worker",
        )
        assert job["created_by"] == "user" and not job["builtin"]
        assert client.post("/api/jobs", json=body).status_code == 409
        for bad in (
            {**body, "slug": "x2", "cron": "61 * * * *"},
            {**body, "slug": "x3", "cron": "* * *"},
            {**body, "slug": "x4", "interval_s": 600},
            {**body, "slug": "x5", "cron": None},
            {**body, "slug": "Bad Slug"},
        ):
            assert client.post("/api/jobs", json=bad).status_code == 422, bad
        moved = client.put("/api/jobs/backups", json={"interval_s": 7200, "enabled": False})
        assert (moved.json()["interval_s"], moved.json()["cron"]) == (7200, None)
        assert moved.json()["enabled"] is False
        assert client.put("/api/jobs/nope", json={"enabled": False}).status_code == 404
        assert client.delete("/api/jobs/backups").status_code == 204
        assert client.get("/api/jobs/backups").status_code == 404
        assert client.delete("/api/jobs/storage").status_code == 409  # builtin


def test_builtins_can_be_edited_and_reset_and_unedited_ones_follow_releases(
    bootstrap: Bootstrap,
) -> None:
    app = make_app(bootstrap)
    with TestClient(app) as client:
        toggled = client.put("/api/jobs/journal", json={"enabled": False}).json()
        assert toggled["enabled"] is False and toggled["edited"] is False  # not an edit
        edited = client.put(
            "/api/jobs/storage", json={"prompt": "Only check /srv please.", "interval_s": 600}
        ).json()
        assert edited["edited"] is True and edited["interval_s"] == 600
        reset = client.post("/api/jobs/storage/reset").json()
        assert reset["edited"] is False and reset["prompt"] == BY_SLUG["storage"].prompt
        assert reset["interval_s"] == BY_SLUG["storage"].interval_s
        assert client.post("/api/jobs/nope/reset").status_code == 404

        # A release changed the prompts: unedited builtins follow, edited ones keep theirs.
        client.put("/api/jobs/docker", json={"prompt": "My own docker check."})
        db = app.state.ctx.db

        async def age_prompts() -> None:
            await db.execute("UPDATE jobs SET prompt = 'old prompt text' WHERE slug = 'systemd'")
            await app.state.ctx.jobs.store.seed_builtins()

        client.portal.call(age_prompts)  # type: ignore[union-attr]
        assert client.get("/api/jobs/systemd").json()["prompt"] == BY_SLUG["systemd"].prompt
        assert client.get("/api/jobs/docker").json()["prompt"] == "My own docker check."
        assert client.get("/api/jobs/journal").json()["enabled"] is False  # kept


def test_run_now_returns_at_once_and_the_run_finishes(
    bootstrap: Bootstrap, models: ScriptedModels, shell: list[str]
) -> None:
    models.script = [finish("ok", "Nothing pending.")]
    with TestClient(make_app(bootstrap)) as client:
        started = client.post("/api/jobs/updates/run")
        assert started.status_code == 202
        run_id = started.json()["run_id"]
        deadline = time.monotonic() + 10
        while client.get(f"/api/jobs/runs/{run_id}").json()["status"] == "running":
            assert time.monotonic() < deadline
            time.sleep(0.05)
        done = client.get(f"/api/jobs/runs/{run_id}").json()
        assert done["status"] == "ok" and done["trigger"] == "manual"
        assert client.post("/api/jobs/nope/run").status_code == 404
        assert client.get("/api/jobs/runs/999").status_code == 404
