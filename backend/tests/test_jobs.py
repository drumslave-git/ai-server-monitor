"""Scheduled LLM jobs: builtins, runs through the agent loop, the API."""

from __future__ import annotations

import json
import time
from datetime import timedelta
from typing import Any

import httpx
from fastapi.testclient import TestClient

from ai_server_monitor.config import Bootstrap, Settings
from ai_server_monitor.jobs.builtins import BUILTINS, BY_SLUG, DAILY, TOPICS
from ai_server_monitor.main import create_app

from .conftest import LLM_URL
from .jobfakes import ScriptedModels, finish, tool, topics_of

FINDING = {
    "severity": "warn",
    "title": "/ is 40% full",
    "detail": "60G free",
    "evidence": ["/dev/sda1  100G  40G  60G  40% /"],
}
SUMMARY_PROMPT = "Sum up the checks below for the owner in two lines."


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


def make_summary(client: TestClient, slug: str, members: list[str], **fields: Any) -> dict:
    body = {"slug": slug, "kind": "summary", "name": slug.title(), "prompt": SUMMARY_PROMPT}
    created = client.post("/api/jobs", json={**body, "members": members, **fields})
    assert created.status_code == 201, created.text
    return created.json()


def test_builtins_are_seeded_with_their_schedules(bootstrap: Bootstrap) -> None:
    with TestClient(make_app(bootstrap)) as client:
        listed = client.get("/api/jobs").json()
    items = listed["items"]
    by_slug = {j["slug"]: j for j in items}
    assert [j["slug"] for j in items][:2] == ["summary:daily", "summary:health"]
    assert set(by_slug) == {d.slug for d in BUILTINS} and len(TOPICS) == 14
    assert listed["retired"] == []
    health, daily = by_slug["summary:health"], by_slug["summary:daily"]
    assert (health["interval_s"], health["cron"]) == (3600, None)
    assert (daily["interval_s"], daily["cron"]) == (None, "0 6 * * *")
    assert daily["members"] == list(DAILY)
    assert health["members"] == [t.slug for t in TOPICS if t.slug not in DAILY]
    assert health["kind"] == daily["kind"] == "summary" and health["model"] == "main"
    for topic in TOPICS:
        job = by_slug[topic.slug]
        assert (job["interval_s"], job["cron"], job["model"]) == (None, None, "worker")
        assert job["summaries"] == (
            ["summary:daily"] if topic.slug in DAILY else ["summary:health"]
        )
        assert job["next_run"] is None  # its summary runs it
    assert {j["slug"]: j["notify"] for j in items if j["notify"] != "warn_crit"} == {
        "summary:daily": "changed",
        "summary:health": "changed",
    }
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
        result = run(client, "storage:usage")
        assert result["status"] == "warn" and result["summary"] == "Root is 88% full."
        assert result["findings"] == [{**FINDING, "verified": True}] and result["error"] is None
        assert result["trigger"] == "manual" and result["steps"] == 2
        assert result["model"] == "small" and result["trace_turn"]
        assert result["finished"] >= result["started"] and result["queue_run_id"] is None
        # the topic run saw only bash, its own check, and ran the command in the shell
        assert [r["tools"] for r in models.requests] == [["bash"], ["bash"]]
        assert "Disk space and inodes" in models.requests[0]["instructions"]
        assert shell == ["df -h /"]
        # job commands stay out of the chat audit trail (they are in the run's trace)
        audit = client.get("/api/chat/audit", params={"kind": "shell"}).json()["items"]
        assert audit == []
        # listed with the job, and through the runs API
        job = client.get("/api/jobs/storage:usage").json()
        assert job["last_run"]["id"] == result["id"] and job["last_run"]["status"] == "warn"
        runs = client.get("/api/jobs/storage:usage/runs").json()["items"]
        assert [r["id"] for r in runs] == [result["id"]]
        assert client.get(f"/api/jobs/runs/{result['id']}").json()["findings"] == [
            {**FINDING, "verified": True}
        ]
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
        failed = run(client, "docker:status")
        assert failed["status"] == "error" and "model exploded" in failed["error"]
        assert failed["finished"] is not None
        # a run over its time limit
        app.state.ctx.settings.jobs.timeout = timedelta(seconds=0.3)
        models.script = ["<sleep>"]
        slow = run(client, "docker:status")
        assert slow["status"] == "error" and "timed out" in slow["error"]
        # a model that never calls final_result
        app.state.ctx.settings.jobs.timeout = timedelta(minutes=5)
        models.script = ["I think everything is fine."] * 5
        chatty = run(client, "docker:status")
        assert chatty["status"] == "error" and chatty["error"]
        runs = client.get("/api/jobs/docker:status/runs").json()["items"]
        assert [r["status"] for r in runs] == ["error", "error", "error"]


def test_a_failed_summary_has_a_plain_headline_and_keeps_the_raw_error(
    bootstrap: Bootstrap, models: ScriptedModels, shell: list[str]
) -> None:
    from pydantic_ai.exceptions import ModelConnectionError

    from ai_server_monitor.jobs.status import headline

    def unreachable(messages: Any, info: Any) -> Any:
        raise ModelConnectionError(model_name="big", message="Connection error.")

    models.script = [unreachable]
    with TestClient(make_app(bootstrap)) as client:
        make_summary(client, "night", [])
        failed = run(client, "night")
        assert failed["error"] == "ModelConnectionError: Connection error."
        overall = client.get("/api/status").json()["overall"]
        assert overall["status"] == "error"
        assert overall["headline"] == "Summary failed: the model could not be reached"
    for error, words in [
        ("timed out after 900s (limit 900s)", "it took longer than its time limit"),
        ("no result: I think all is fine", "the model gave no result"),
        ("KeyError: 'x'", "an internal error (see the run)"),
    ]:
        assert headline({"status": "error", "error": error}) == f"Summary failed: {words}"


def test_the_summary_runs_its_jobs_in_order_and_reads_their_results(
    bootstrap: Bootstrap, models: ScriptedModels, shell: list[str]
) -> None:
    with TestClient(make_app(bootstrap)) as client:
        make_summary(client, "overview", ["resources:load", "docker:status", "storage:usage"])
        client.put("/api/jobs/docker:status", json={"enabled": False})
        models.script = [
            tool("bash", command="free -m"),
            finish("crit", "Only 2% memory available.", [FINDING]),
            tool("bash", command="df -h /"),
            finish("ok", "Root has room."),
            finish("crit", "Memory is nearly exhausted.\nDetails follow.", [FINDING]),
        ]
        summary = run(client, "overview")
        assert shell == ["free -m", "df -h /"]  # the jobs ran first, in their order
        request = models.requests[-1]
        assert request["tools"] == ["bash", "delegate"]
        assert SUMMARY_PROMPT in request["instructions"]
        text = request["text"]
        load, disk = text.index("Resources: load (resources:load)"), text.index("(storage:usage)")
        assert load < disk and "Only 2% memory" in text and "Root has room." in text
        assert "docker:status" not in text  # disabled jobs are left out
        assert summary["status"] == "crit" and summary["model"] == "big"
        # each job's run is its own run, linked to the summary's
        stored = client.get(f"/api/jobs/runs/{summary['id']}").json()
        queue = stored["queue"]
        assert [r["job_slug"] for r in queue] == ["resources:load", "storage:usage"]
        assert all(r["queue_run_id"] == summary["id"] for r in queue)
        assert [r["status"] for r in queue] == ["crit", "ok"]
        assert queue[0]["finished"] <= queue[1]["started"]
        assert queue[1]["finished"] <= stored["finished"]
        listed = client.get("/api/jobs/overview").json()["last_run"]
        assert [r["id"] for r in listed["queue"]] == [r["id"] for r in queue]
        status = client.get("/api/status").json()
        assert status["overall"]["status"] == "crit"
        assert status["overall"]["headline"] == "Memory is nearly exhausted."
        assert {j["slug"]: j["status"] for j in status["jobs"]}["resources:load"] == "crit"
        # the next summary sees the previous one
        models.script = [
            tool("bash", command="free -m"),
            finish("ok", "Memory fine."),
            tool("bash", command="df -h /"),
            finish("ok", "Root has room."),
            finish("ok", "Recovered."),
        ]
        run(client, "overview")
        assert "Memory is nearly exhausted." in models.requests[-1]["text"]


def test_jobs_crud_through_the_api(bootstrap: Bootstrap) -> None:
    body = {
        "slug": "backups:restic",
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
        assert job["summaries"] == [] and job["notify"] == "warn_crit" and job["notice"] is None
        assert client.post("/api/jobs", json=body).status_code == 409
        for bad in (
            {**body, "slug": "x2", "cron": "61 * * * *"},
            {**body, "slug": "x3", "cron": "* * *"},
            {**body, "slug": "x4", "interval_s": 600},
            {**body, "slug": "Bad Slug"},
            {**body, "slug": "x5:"},
            {**body, "slug": ":x5"},
            {**body, "slug": "x5::y"},
            {**body, "slug": "x6", "notify": "sometimes"},
        ):
            assert client.post("/api/jobs", json=bad).status_code == 422, bad
        # no schedule: run by hand only
        by_hand = client.post("/api/jobs", json={**body, "slug": "x7", "cron": None})
        assert by_hand.status_code == 201 and by_hand.json()["next_run"] is None
        moved = client.put("/api/jobs/backups:restic", json={"interval_s": 7200, "enabled": False})
        assert (moved.json()["interval_s"], moved.json()["cron"]) == (7200, None)
        assert moved.json()["enabled"] is False
        assert client.put("/api/jobs/nope", json={"enabled": False}).status_code == 404
        assert client.delete("/api/jobs/backups:restic").status_code == 204
        assert client.get("/api/jobs/backups:restic").status_code == 404
        assert client.delete("/api/jobs/storage:usage").status_code == 409  # builtin
        assert client.delete("/api/jobs/summary:health").status_code == 409  # builtin


def test_builtins_can_be_edited_and_reset_and_unedited_ones_follow_releases(
    bootstrap: Bootstrap,
) -> None:
    app = make_app(bootstrap)
    with TestClient(app) as client:
        toggled = client.put("/api/jobs/journal:errors", json={"enabled": False}).json()
        assert toggled["enabled"] is False and toggled["edited"] is False  # not an edit
        noisy = client.put("/api/jobs/journal:kernel", json={"notify": "always"}).json()
        assert noisy["notify"] == "always" and noisy["edited"] is False
        edited = client.put(
            "/api/jobs/storage:usage", json={"prompt": "Only check /srv please.", "max_steps": 4}
        ).json()
        assert edited["edited"] is True and edited["max_steps"] == 4
        reset = client.post("/api/jobs/storage:usage/reset").json()
        assert reset["edited"] is False and reset["prompt"] == BY_SLUG["storage:usage"].prompt
        assert reset["max_steps"] == BY_SLUG["storage:usage"].max_steps
        assert client.post("/api/jobs/nope/reset").status_code == 404

        # A builtin summary: its member list is part of it.
        health = BY_SLUG["summary:health"].members
        shorter = client.put("/api/jobs/summary:health", json={"members": list(health[:3])})
        assert shorter.json()["edited"] is True and shorter.json()["members"] == list(health[:3])
        detached = client.get(f"/api/jobs/{health[5]}").json()
        assert detached["summaries"] == [] and detached["interval_s"] is None
        back = client.post("/api/jobs/summary:health/reset").json()
        assert back["members"] == list(health) and back["edited"] is False

        # A release changed the prompts: unedited builtins follow, edited ones keep theirs.
        client.put("/api/jobs/docker:status", json={"prompt": "My own docker check."})
        db = app.state.ctx.db

        async def age_prompts() -> None:
            await db.execute(
                "UPDATE jobs SET prompt = 'old prompt text' WHERE slug = 'systemd:failed'"
            )
            await app.state.ctx.jobs.store.seed_builtins()

        client.portal.call(age_prompts)  # type: ignore[union-attr]
        systemd = client.get("/api/jobs/systemd:failed").json()
        assert systemd["prompt"] == BY_SLUG["systemd:failed"].prompt
        assert client.get("/api/jobs/docker:status").json()["prompt"] == "My own docker check."
        assert client.get("/api/jobs/journal:errors").json()["enabled"] is False  # kept
        assert client.get("/api/jobs/journal:kernel").json()["notify"] == "always"  # kept


def test_run_now_returns_at_once_and_the_run_finishes(
    bootstrap: Bootstrap, models: ScriptedModels, shell: list[str]
) -> None:
    models.script = [tool("bash", command="apt list --upgradable"), finish("ok", "No updates.")]
    with TestClient(make_app(bootstrap)) as client:
        started = client.post("/api/jobs/updates:packages/run")
        assert started.status_code == 202
        run_id = started.json()["run_id"]
        deadline = time.monotonic() + 10
        while client.get(f"/api/jobs/runs/{run_id}").json()["status"] == "running":
            assert time.monotonic() < deadline
            time.sleep(0.05)
        done = client.get(f"/api/jobs/runs/{run_id}").json()
        # a job in a summary run by hand runs alone: no summary follows
        assert done["status"] == "ok" and done["trigger"] == "manual"
        assert done["queue_run_id"] is None and len(models.requests) == 2
        assert client.get("/api/jobs/summary:daily").json()["last_run"] is None
        assert client.post("/api/jobs/nope/run").status_code == 404
        assert client.get("/api/jobs/runs/999").status_code == 404


def test_a_run_in_progress_is_shown_on_the_job_and_in_status(bootstrap: Bootstrap) -> None:
    with TestClient(make_app(bootstrap)) as client:
        store = client.app.state.ctx.jobs.store  # type: ignore[attr-defined]
        assert client.get("/api/jobs/storage:usage").json()["running_run_id"] is None
        run_id = client.portal.call(store.start_run, "storage:usage", "manual", "small")
        jobs = {j["slug"]: j for j in client.get("/api/jobs").json()["items"]}
        assert jobs["storage:usage"]["running_run_id"] == run_id
        assert jobs["docker:status"]["running_run_id"] is None
        assert client.get("/api/jobs/storage:usage").json()["running_run_id"] == run_id
        status = {j["slug"]: j for j in client.get("/api/status").json()["jobs"]}
        assert status["storage:usage"]["running_run_id"] == run_id
        client.portal.call(lambda: store.finish_run(run_id, status="ok", finished=time.time()))
        assert client.get("/api/jobs/storage:usage").json()["running_run_id"] is None


def test_an_interrupted_run_is_rerun_soon_and_does_not_hide_the_last_result(
    bootstrap: Bootstrap, models: ScriptedModels, shell: list[str]
) -> None:
    from ai_server_monitor.jobs.store import INTERRUPTED

    models.script = [
        tool("bash", command="df -h /"),
        finish("warn", "Root is 40% full.", [FINDING]),
    ]
    body = {"slug": "disk:srv", "name": "Disk", "prompt": "Run df -h /srv.", "interval_s": 14400}
    with TestClient(make_app(bootstrap)) as client:
        assert client.post("/api/jobs", json=body).status_code == 201
        real = run(client, "disk:srv")
        store = client.app.state.ctx.jobs.store  # type: ignore[attr-defined]
        # a run in flight when the process dies (no clean stop)
        client.portal.call(store.start_run, "disk:srv", "schedule", "small")
    with TestClient(make_app(bootstrap)) as client:
        runs = client.get("/api/jobs/disk:srv/runs").json()["items"]
        assert [r["error"] for r in runs] == [INTERRUPTED, None]
        job = client.get("/api/jobs/disk:srv").json()
        assert job["last_run"]["id"] == real["id"]
        status = {j["slug"]: j for j in client.get("/api/status").json()["jobs"]}
        assert status["disk:srv"]["status"] == "warn" and status["disk:srv"]["run_id"] == real["id"]
        # rerun about a minute after the start, not in 4 hours; other jobs keep their times
        assert job["next_run"] - time.time() < 120
        assert client.get("/api/jobs/summary:health").json()["next_run"] - time.time() > 600


def test_marking_interrupted_runs_counts_rows_not_ids(bootstrap: Bootstrap) -> None:
    with TestClient(make_app(bootstrap)) as client:
        store = client.app.state.ctx.jobs.store  # type: ignore[attr-defined]
        # a fresh install: the builtins were just inserted, no run was interrupted
        assert client.portal.call(store.mark_interrupted) == 0  # type: ignore[union-attr]
        for slug in ("storage:usage", "docker:status"):
            client.portal.call(store.start_run, slug, "schedule", "small")  # type: ignore[union-attr]
        assert client.portal.call(store.mark_interrupted) == 2  # type: ignore[union-attr]


def test_a_model_out_of_steps_never_crashes_the_run(
    bootstrap: Bootstrap, models: ScriptedModels, shell: list[str]
) -> None:
    """Run 4 of the live systemd job: at the step limit only final_result is offered, the small
    model calls bash again, and the run died with "Tool 'bash' exceeded max retries count"."""
    with TestClient(make_app(bootstrap)) as client:
        client.put("/api/jobs/systemd:failed", json={"max_steps": 2})
        # keeps calling bash after the limit: a plain error, the trace is kept
        models.script = [tool("bash", command="systemctl --failed")] * 12
        stuck = run(client, "systemd:failed")
        assert stuck["status"] == "error"
        steps = stuck["steps"]
        assert stuck["error"] == f"The model did not finish within {steps} steps (the limit is 2)."
        assert stuck["steps"] > 2 and stuck["prompt_tokens"] and stuck["completion_tokens"]
        assert stuck["trace_turn"] and stuck["steps"]
        past = [r for r in models.requests if r["tools"] == []]
        assert past and "out of steps" in past[0]["instructions"]
        assert "call final_result now" in past[0]["instructions"]
        overall = {j["slug"]: j for j in client.get("/api/status").json()["jobs"]}
        assert overall["systemd:failed"]["status"] == "error"
        # strays once at the limit, then gives its result: the result is stored
        models.script = [
            tool("bash", command="systemctl --failed"),
            tool("bash", command="journalctl -p err"),
            tool("bash", command="systemctl list-units"),
            finish("warn", "Could not check: systemctl is not available."),
        ]
        late = run(client, "systemd:failed")
        assert late["status"] == "warn" and late["error"] is None


def test_findings_must_quote_command_output(
    bootstrap: Bootstrap, models: ScriptedModels, shell: list[str]
) -> None:
    """Live storage run 2 reported "/appdata at 896G" where du said 208G: evidence that is not
    in the command output gets one retry naming it, and is stored as not confirmed."""
    wrong = {**FINDING, "detail": "90G used", "evidence": ["/dev/sda1  100G  90G  10G  90% /"]}
    bare = {k: v for k, v in FINDING.items() if k != "evidence"}
    with TestClient(make_app(bootstrap)) as client:
        # a wrong quote, repeated after the retry: kept, marked not confirmed
        models.script = [
            tool("bash", command="df -h /"),
            finish("warn", "Root is 90% full.", [wrong]),
            finish("warn", "Root is 90% full.", [wrong]),
        ]
        result = run(client, "storage:usage")
        retry = models.requests[-1]["text"]
        assert "Evidence check failed" in retry and "/dev/sda1 100G 90G 10G 90% /" in retry
        assert result["status"] == "warn" and result["findings"][0]["verified"] is False
        # warn without evidence: retried; the corrected answer (re-spaced) is confirmed
        respaced = {**FINDING, "evidence": ["`/dev/sda1 100G 40G 60G 40% /`"]}
        models.script = [
            tool("bash", command="df -h /"),
            finish("warn", "Root is 40% full.", [bare]),
            finish("warn", "Root is 40% full.", [respaced]),
        ]
        fixed = run(client, "storage:usage")
        assert "has no evidence" in models.requests[-1]["text"]
        assert fixed["findings"][0]["verified"] is True
        # an info finding needs no evidence
        models.script = [
            tool("bash", command="df -h /"),
            finish("ok", "Fine.", [{"severity": "info", "title": "note"}]),
        ]
        assert run(client, "storage:usage")["findings"][0]["verified"] is True


def test_lists_sent_as_json_strings_are_accepted(
    bootstrap: Bootstrap, models: ScriptedModels, shell: list[str]
) -> None:
    """Small models send `findings` (and `evidence`) as a JSON string instead of an array:
    the result is taken as that list, not rejected and lost."""
    as_string = {**FINDING, "evidence": json.dumps(FINDING["evidence"])}
    one_line = {**FINDING, "title": "one line", "evidence": FINDING["evidence"][0]}
    models.script = [
        tool("bash", command="df -h /"),
        tool(
            "final_result",
            status="warn",
            summary="Root is 40% full.",
            findings=json.dumps([as_string, one_line]),
        ),
    ]
    with TestClient(make_app(bootstrap)) as client:
        result = run(client, "storage:usage")
    assert result["status"] == "warn" and result["error"] is None
    assert result["steps"] == 2  # accepted on the first try: no validation retry
    assert [f["evidence"] for f in result["findings"]] == [FINDING["evidence"]] * 2
    assert all(f["verified"] for f in result["findings"])


def test_a_list_with_trailing_garbage_is_read_and_an_unreadable_one_is_explained(
    bootstrap: Bootstrap, models: ScriptedModels, shell: list[str]
) -> None:
    """Live run 26: `findings` as a JSON string with one stray `}` at the end was refused four
    times with "Input should be a valid array"."""
    models.script = [
        tool("bash", command="df -h /"),
        tool("final_result", status="warn", summary="Root.", findings=json.dumps([FINDING]) + "}"),
    ]
    with TestClient(make_app(bootstrap)) as client:
        result = run(client, "storage:usage")
        assert result["status"] == "warn" and result["steps"] == 2
        assert [f["title"] for f in result["findings"]] == [FINDING["title"]]
        # Not readable at all: the model is told what it sent, what is expected, an example.
        models.script = [
            tool("bash", command="df -h /"),
            tool("final_result", status="warn", summary="Root.", findings="see above"),
            finish("warn", "Root.", [FINDING]),
        ]
        result = run(client, "storage:usage")
        assert result["status"] == "warn" and result["findings"][0]["verified"]
        told = models.requests[-1]["text"]
        assert "`findings` must be a JSON array (a list), not a string" in told
        assert "'see above'" in told and '"severity": "warn"' in told


def summary_without(slug: str) -> Any:
    """A summary result whose topic list leaves `slug` out."""

    def step(messages: Any, info: Any) -> tuple[str, dict[str, Any]]:
        topics = [t for t in topics_of(messages) if t["slug"] != slug]
        return ("final_result", {"status": "warn", "summary": "Disk.", "topics": topics})

    return step


def test_the_summary_lists_every_topic_with_its_real_status(
    bootstrap: Bootstrap, models: ScriptedModels, shell: list[str]
) -> None:
    """Live: the summary left out updates (38 pending) and resources. Its result has one entry
    per job of its queue; a missing one is sent back once, then stored with the real status."""
    jobs = [
        tool("bash", command="df -h /"),
        finish("warn", "Root is 40% full.", [FINDING]),
        tool("bash", command="docker ps -a"),
        finish("ok", "All containers up."),
    ]
    models.script = [
        *jobs,
        summary_without("storage:usage"),  # sent back once, naming it
        finish("warn", "Disk."),
    ]
    with TestClient(make_app(bootstrap)) as client:
        make_summary(client, "night", ["storage:usage", "docker:status"])
        result = run(client, "night")
        assert result["status"] == "warn"
        topics = {t["slug"]: t for t in result["topics"]}
        assert list(topics) == ["storage:usage", "docker:status"]
        assert topics["storage:usage"]["status"] == "warn"
        assert topics["docker:status"]["status"] == "ok"
        assert "storage:usage is missing" in models.requests[-1]["text"]
        stored = client.get(f"/api/jobs/runs/{result['id']}").json()
        assert stored["topics"] == result["topics"]
        # Left out again after the retry: stored all the same, with the real status.
        models.script = [
            *jobs,
            summary_without("storage:usage"),
            summary_without("storage:usage"),
        ]
        again = run(client, "night")
        storage = next(t for t in again["topics"] if t["slug"] == "storage:usage")
        assert again["status"] == "warn"
        assert storage == {
            "slug": "storage:usage",
            "status": "warn",
            "clause": "(not in the summary)",
        }


def test_the_telegram_summary_shows_one_line_per_topic() -> None:
    from ai_server_monitor.outputs.telegram import summary_message

    text = summary_message(
        {
            "status": "warn",
            "summary": "Root disk at 91%",
            "findings": [],
            "topics": [
                {"slug": "storage:usage", "status": "warn", "clause": "/ at 91%"},
                {"slug": "updates:packages", "status": "ok", "clause": "38 pending"},
            ],
        }
    )
    # (Markdown: the slug's ":" escaped, shown as written)
    assert text.splitlines()[1:] == [
        "⚠️ storage\\:usage: / at 91%",
        "✅ updates\\:packages: 38 pending",
    ]


def test_a_topic_run_must_run_a_command(
    bootstrap: Bootstrap, models: ScriptedModels, shell: list[str]
) -> None:
    """Live 0.7.3: the docker job on the worker model ran no command and said "all healthy".
    A result before any command is sent back once; a run that still ran none is an error.
    The summary reads the topic results and needs no command."""
    with TestClient(make_app(bootstrap)) as client:
        models.script = [finish("ok", "All healthy."), finish("ok", "All healthy.")]
        lazy = run(client, "docker:status")
        assert lazy["status"] == "error" and lazy["error"] == "ran no command"
        assert "You have not run any command yet" in models.requests[-1]["text"]
        assert shell == []
        # sent back, it looks, and the result counts
        models.script = [
            finish("ok", "All healthy."),
            tool("bash", command="docker ps -a"),
            finish("ok", "All containers up."),
        ]
        looked = run(client, "docker:status")
        assert looked["status"] == "ok" and looked["error"] is None
        assert shell == ["docker ps -a"]
        # the summary is exempt
        make_summary(client, "night", [])
        models.script = [finish("ok", "Quiet.")]
        summary = run(client, "night")
        assert summary["status"] == "ok" and "You have not run" not in models.requests[-1]["text"]
