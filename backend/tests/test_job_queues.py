"""Summaries run an ordered queue of jobs (0.10.0): a failed job, overlapping starts, a job in
two summaries, the schedule a job loses when it joins one, what reaches Telegram, and the
upgrade of a 0.9.1 database."""

from __future__ import annotations

import asyncio
import json
import sqlite3
import time
from typing import Any

import pytest
from fastapi.testclient import TestClient
from pydantic_ai.messages import ModelMessage, ToolReturnPart
from pydantic_ai.models.function import AgentInfo

from ai_server_monitor.config import Bootstrap
from ai_server_monitor.db import load_migrations
from ai_server_monitor.jobs.builtins import BUILTINS
from ai_server_monitor.schedule import Schedule

from .jobfakes import ScriptedModels, finish, tool
from .test_jobs import make_app, make_summary, run
from .test_telegram import FakeTelegram, running, settle, wait_for
from .test_telegram import make_app as telegram_app
from .test_telegram import run_job as run_and_wait


def looks_then_reports(status: str = "ok", summary: str = "Fine.") -> Any:
    """A step that fits whichever run asks: a job runs one command, then reports; a summary
    reports at once. For runs whose requests interleave."""

    def step(messages: list[ModelMessage], info: AgentInfo) -> Any:
        final = next((t for t in info.output_tools if t.name == "final_result"), None)
        is_summary = final is not None and "topics" in final.parameters_json_schema.get(
            "properties", {}
        )
        looked = any(
            isinstance(part, ToolReturnPart) for m in messages for part in getattr(m, "parts", [])
        )
        if is_summary or looked:
            return finish(status, summary)(messages, info)
        return tool("bash", command="df -h /")

    return step


def test_a_failed_job_does_not_stop_the_queue_and_the_summary_is_told(
    bootstrap: Bootstrap, models: ScriptedModels, shell: list[str]
) -> None:
    with TestClient(make_app(bootstrap)) as client:
        make_summary(client, "overview", ["docker:status", "storage:usage"])
        models.script = [
            finish("ok", "All healthy."),  # no command: sent back once ...
            finish("ok", "All healthy."),  # ... and still none: the run is an error
            tool("bash", command="df -h /"),
            finish("ok", "Root has room."),
            finish("warn", "The docker check failed; storage is fine."),
        ]
        summary = run(client, "overview")
        assert shell == ["df -h /"]  # the job after the failed one still ran
        text = models.requests[-1]["text"]
        docker = text.split("(docker:status)")[1].split("## ")[0]
        assert "status: error" in docker and "error: ran no command" in docker
        assert "Root has room." in text
        queue = client.get(f"/api/jobs/runs/{summary['id']}").json()["queue"]
        assert [(r["job_slug"], r["status"]) for r in queue] == [
            ("docker:status", "error"),
            ("storage:usage", "ok"),
        ]
        assert summary["status"] == "warn"


async def test_a_start_due_while_the_queue_still_runs_is_skipped() -> None:
    """APScheduler's max_instances=1, as before: the queue is one scheduled run, so the starts
    due while it runs (here at 2 s and 3 s) are skipped, not queued up."""
    starts: list[float] = []

    async def run_job(slug: str) -> None:
        starts.append(time.monotonic())
        await asyncio.sleep(2.5)

    sched = Schedule()
    sched.sync_jobs(
        [{"slug": "summary:health", "enabled": True, "interval_s": 1, "cron": None}], run_job
    )
    sched.start()
    try:
        await asyncio.sleep(3.3)
    finally:
        sched.stop()
    assert len(starts) == 1


def test_a_job_in_two_summaries_runs_in_each_one_after_the_other(
    bootstrap: Bootstrap, models: ScriptedModels, shell: list[str]
) -> None:
    with TestClient(make_app(bootstrap)) as client:
        make_summary(client, "morning", ["storage:usage"])
        made = make_summary(client, "evening", ["storage:usage", "journal:errors"])
        assert made["members"] == ["storage:usage", "journal:errors"]
        assert client.get("/api/jobs/storage:usage").json()["summaries"] == [
            "evening",
            "morning",
            "summary:health",
        ]
        storage = next(d.prompt for d in BUILTINS if d.slug == "storage:usage")
        inside: list[int] = []  # storage:usage runs between their command and their result

        def watched(messages: list[ModelMessage], info: AgentInfo) -> Any:
            step = looks_then_reports()(messages, info)
            if storage in (info.instructions or ""):
                if isinstance(step, tuple) and step[0] == "bash":
                    inside.append(1)
                    assert len(inside) == 1, "two storage:usage runs at once"
                else:
                    inside.pop()
            return step

        models.script = [watched for _ in range(20)]
        jobs = client.app.state.ctx.jobs  # type: ignore[union-attr]
        first = client.portal.call(jobs.start, "morning", "manual")  # type: ignore[union-attr]
        second = client.portal.call(jobs.start, "evening", "manual")  # type: ignore[union-attr]
        wait_for(
            lambda: all(
                client.get(f"/api/jobs/runs/{i}").json()["status"] != "running"
                for i in (first, second)
            ),
            timeout=15,
        )
        runs = client.get("/api/jobs/storage:usage/runs").json()["items"]
        assert sorted(r["queue_run_id"] for r in runs) == sorted([first, second])
        assert inside == [] and shell.count("df -h /") == 3  # 2 x storage:usage, journal:errors
        for run_id in (first, second):
            stored = client.get(f"/api/jobs/runs/{run_id}").json()
            assert stored["status"] == "ok" and stored["queue"]


def test_a_job_that_joins_a_summary_loses_its_schedule_and_is_told(
    bootstrap: Bootstrap,
) -> None:
    body = {"slug": "backups:restic", "name": "Backups", "prompt": "Check the restic backup."}
    with TestClient(make_app(bootstrap)) as client:
        made = client.post("/api/jobs", json={**body, "interval_s": 3600}).json()
        assert made["next_run"] is not None and made["notice"] is None
        # joining through the job
        joined = client.put("/api/jobs/backups:restic", json={"summaries": ["summary:health"]})
        assert joined.status_code == 200
        job = joined.json()
        assert job["notice"] == (
            "backups:restic was scheduled (every hour). It is now in the summary Health, "
            "which runs it, so its own schedule was removed."
        )
        assert (job["interval_s"], job["cron"], job["next_run"]) == (None, None, None)
        assert job["summaries"] == ["summary:health"]
        health = client.get("/api/jobs/summary:health").json()
        assert health["members"][-1] == "backups:restic" and health["edited"]
        # a member gets no schedule of its own
        refused = client.put("/api/jobs/backups:restic", json={"interval_s": 600})
        assert refused.status_code == 409
        assert "a job in a summary has no schedule of its own" in refused.json()["detail"]
        # out of the summary, it may have one again
        left = client.put("/api/jobs/backups:restic", json={"summaries": []}).json()
        assert left["summaries"] == [] and left["notice"] is None
        rescheduled = client.put("/api/jobs/backups:restic", json={"cron": "30 7 * * *"}).json()
        assert rescheduled["next_run"] is not None
        # joining through the summary's member list
        night = make_summary(client, "night", ["backups:restic"])
        assert "backups:restic was scheduled (at 07:30)" in night["notice"]
        assert "in the summary Night" in night["notice"]
        assert client.get("/api/jobs/backups:restic").json()["cron"] is None
        # a schedule given with the summaries on create is dropped the same way
        both = client.post(
            "/api/jobs",
            json={**body, "slug": "backups:borg", "interval_s": 3600, "summaries": ["night"]},
        ).json()
        assert both["interval_s"] is None and "was scheduled (every hour)" in both["notice"]


def test_notify_decides_what_reaches_telegram_and_a_queue_job_sends_nothing(
    bootstrap: Bootstrap, models: ScriptedModels, shell: list[str]
) -> None:
    tg = FakeTelegram()
    app = telegram_app(bootstrap, tg)
    with running(app, tg) as client:
        for slug in ("summary:daily", "summary:health"):
            client.put(f"/api/jobs/{slug}", json={"enabled": False})
        notifies = (
            ("disk:a", "always"),
            ("disk:b", "warn_crit"),
            ("disk:c", "never"),
            ("disk:d", "changed"),
        )
        for slug, notify in notifies:
            made = client.post(
                "/api/jobs",
                json={
                    "slug": slug,
                    "name": slug.title(),
                    "prompt": "Look at the disk.",
                    "notify": notify,
                },
            )
            assert made.status_code == 201, made.text
        models.script = [tool("bash", command="df -h /"), finish("ok", "Fine.")]
        run_and_wait(client, "disk:a")
        assert len(tg.texts()) == 1 and tg.texts()[0].startswith("✅ Disk:A: OK — Fine.")
        models.script = [tool("bash", command="df -h /"), finish("ok", "Fine.")]
        run_and_wait(client, "disk:b")
        assert len(tg.texts()) == 1  # ok is not sent for warn_crit
        models.script = [tool("bash", command="df -h /"), finish("warn", "Filling up.")]
        run_and_wait(client, "disk:b")
        assert len(tg.texts()) == 2 and tg.texts()[1].startswith("⚠️ Disk:B: Warning")
        models.script = [tool("bash", command="df -h /"), finish("crit", "Full.")]
        run_and_wait(client, "disk:c")
        assert len(tg.texts()) == 2  # never
        for status, sent in (("ok", 3), ("ok", 3), ("warn", 4), ("warn", 4), ("ok", 5)):
            models.script = [tool("bash", command="df -h /"), finish(status, "Disk.")]
            run_and_wait(client, "disk:d")  # changed: the first run, then each change
            assert len(tg.texts()) == sent, status
        assert tg.texts()[4].startswith("✅ Disk:D: OK")  # back to OK is news
        # in a summary: only the summary speaks, whatever the job's own notify says
        make_summary(client, "night", ["disk:a"], notify="always")
        models.script = [
            tool("bash", command="df -h /"),
            finish("crit", "Full."),
            finish("crit", "The disk is full."),
        ]
        run_and_wait(client, "night")
        settle()
        assert len(tg.texts()) == 6 and tg.texts()[5].startswith("🔴 Night: Critical")
        # run alone, a job that is in a summary sends nothing either
        models.script = [tool("bash", command="df -h /"), finish("crit", "Full.")]
        run_and_wait(client, "disk:a")
        assert len(tg.texts()) == 6


# --------------------------------------------------------------------------- upgrade

OLD_BUILTINS = ("docker", "journal", "resources", "storage", "systemd", "updates", "summary")


def _database_of_0_9_1(path: str, send_summary: str | None = None) -> None:
    """A 0.9.1 database (schema 21): the old builtins, storage and summary edited by the
    owner, an own job, one MCP domain on docker and storage, and a run of docker and storage.
    `send_summary`: stored in the settings and a settings backup (None: never set)."""
    conn = sqlite3.connect(path)
    for version, _name, sql in load_migrations()[:21]:
        conn.executescript(f"BEGIN;\n{sql}\n;PRAGMA user_version = {version};\nCOMMIT;")
    now = time.time()
    if send_summary is not None:
        stored = json.dumps({"outputs": {"telegram": {"send_summary": send_summary}}})
        conn.executemany(
            "INSERT INTO kv (key, value, updated) VALUES (?, ?, ?)",
            [("settings", stored, now), ("settings.backup.1700000000", stored, now)],
        )
    rows = []
    for slug in OLD_BUILTINS:
        edited = slug in ("storage", "summary")
        kind = "summary" if slug == "summary" else "topic"
        prompt = f"My own {slug} check." if edited else f"The {slug} check of 0.9.1."
        interval = 7200 if slug == "storage" else 3600
        model = "main" if kind == "summary" else "worker"
        rows.append((slug, slug.title(), kind, prompt, interval, None, model, 1, int(edited)))
    rows.append(("backups", "Backups", "topic", "Look at the backups.", None, "30 7 * * *",
                 "worker", 0, 0))  # fmt: skip
    conn.executemany(
        "INSERT INTO jobs (slug, name, kind, prompt, interval_s, cron, model, enabled, builtin, "
        "edited, created_by, created, updated) VALUES (?, ?, ?, ?, ?, ?, ?, 1, ?, ?, ?, ?, ?)",
        [(*r[:7], r[7], r[8], "builtin" if r[7] else "user", now, now) for r in rows],
    )
    conn.execute(
        "INSERT INTO mcp_domains (name, description, set_by, updated_ts) "
        "VALUES ('climate', 'Room temperatures', 'owner', ?)",
        (now,),
    )
    conn.execute(
        "INSERT INTO job_domains (job_id, domain_id) SELECT j.id, d.id FROM jobs j, mcp_domains d "
        "WHERE j.slug IN ('docker', 'storage')"
    )
    conn.executemany(
        "INSERT INTO job_runs (job_slug, trigger, started, finished, status, summary, findings) "
        "VALUES (?, 'schedule', ?, ?, 'ok', ?, '[]')",
        [("docker", now - 60, now - 50, "All 12 containers up."), ("storage", now - 40, now - 30,
          "Root has room.")],
    )  # fmt: skip
    conn.commit()
    conn.close()


def test_upgrade_from_0_9_1_retires_the_old_builtins_and_keeps_the_owners_jobs(
    bootstrap: Bootstrap,
) -> None:
    bootstrap.data_dir.mkdir(parents=True, exist_ok=True)
    _database_of_0_9_1(str(bootstrap.db_path))
    with TestClient(make_app(bootstrap)) as client:
        listed = client.get("/api/jobs").json()
        by_slug = {j["slug"]: j for j in listed["items"]}
        # unedited old builtins: retired, out of the list, their runs readable
        retired = {j["slug"] for j in listed["retired"]}
        assert retired == {"docker", "journal", "resources", "systemd", "updates"}
        assert not retired & set(by_slug)
        old_runs = client.get("/api/jobs/docker/runs").json()["items"]
        assert [r["summary"] for r in old_runs] == ["All 12 containers up."]
        # the new builtins
        assert {d.slug for d in BUILTINS} <= set(by_slug)
        # the edited storage: the owner's own detached job, schedule and run kept
        storage = by_slug["storage"]
        assert (storage["builtin"], storage["created_by"], storage["interval_s"]) == (
            False,
            "user",
            7200,
        )
        assert storage["prompt"] == "My own storage check." and storage["enabled"]
        assert storage["summaries"] == [] and storage["next_run"] is not None
        assert storage["last_run"]["summary"] == "Root has room."
        # the edited summary: an own summary, off, with no jobs in it
        summary = by_slug["summary"]
        assert (summary["kind"], summary["builtin"], summary["enabled"]) == (
            "summary",
            False,
            False,
        )
        assert summary["members"] == [] and summary["prompt"] == "My own summary check."
        # the owner's own job, as it was
        backups = by_slug["backups"]
        assert backups["cron"] == "30 7 * * *" and backups["next_run"] is not None
        # the MCP domains of docker and storage went to every job that came out of them
        for d in BUILTINS:
            want = ["climate"] if d.origin in ("docker", "storage") else []
            assert by_slug[d.slug]["domains"] == want, d.slug
        # the scheduler has the summaries and the detached jobs with a schedule, nothing old
        names = {s["name"] for s in client.app.state.ctx.schedule.status()}  # type: ignore[union-attr]
        jobs = {n for n in names if n.startswith("job:")}
        assert jobs == {"job:summary:health", "job:summary:daily", "job:storage", "job:backups"}
        # a retired slug is not free again
        again = client.post(
            "/api/jobs", json={"slug": "docker", "name": "Docker", "prompt": "Look at docker."}
        )
        assert again.status_code == 409 and "old builtin" in again.json()["detail"]


@pytest.mark.parametrize(
    ("send_summary", "notify"),
    [("every", "always"), ("changed", "changed"), ("never", "never"), (None, "changed")],
)
def test_upgrade_maps_send_summary_onto_the_summaries_and_removes_it(
    bootstrap: Bootstrap, send_summary: str | None, notify: str
) -> None:
    bootstrap.data_dir.mkdir(parents=True, exist_ok=True)
    _database_of_0_9_1(str(bootstrap.db_path), send_summary)
    with TestClient(make_app(bootstrap)) as client:
        by_slug = {j["slug"]: j for j in client.get("/api/jobs").json()["items"]}
        for slug in ("summary:health", "summary:daily", "summary"):
            assert by_slug[slug]["notify"] == notify, slug
        assert by_slug["storage"]["notify"] == "never"  # kept jobs did not broadcast in 0.9.1
        assert by_slug["backups"]["notify"] == "never"
        telegram = client.get("/api/settings").json()["values"]["outputs"]["telegram"]
        assert "send_summary" not in telegram
    conn = sqlite3.connect(str(bootstrap.db_path))
    rows = dict(conn.execute("SELECT key, value FROM kv"))
    conn.close()
    assert "jobs.summary_notify" not in rows  # read once by the seeding
    for key in ("settings", "settings.backup.1700000000"):
        assert "send_summary" not in rows.get(key, ""), key


def test_the_schedule_in_a_notice_is_worded_as_the_jobs_form_words_it() -> None:
    from ai_server_monitor.jobs.store import _schedule_words

    assert _schedule_words({"interval_s": 21600}) == "every 6 hours"
    assert _schedule_words({"interval_s": 86400}) == "every day"
    assert _schedule_words({"interval_s": 1800}) == "every 30 minutes"
    assert _schedule_words({"interval_s": 90}) == "every 90 seconds"
    assert _schedule_words({"cron": "0 6 * * 1-5"}) == "at 06:00, Monday through Friday"
