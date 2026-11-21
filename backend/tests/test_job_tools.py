"""The chat's job tools: jobs_list, job_results, job_save, job_delete, summary_member, job_run."""

from __future__ import annotations

from typing import Any

from fastapi.testclient import TestClient

from ai_server_monitor.config import Bootstrap
from ai_server_monitor.jobs.tools import _job_line

from .jobfakes import ScriptedModels, finish, tool
from .test_chat import ask
from .test_jobs import make_app


def results(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [e for e in events if e["type"] == "tool_result"]


def test_the_chat_lists_creates_changes_runs_and_deletes_jobs(
    bootstrap: Bootstrap, models: ScriptedModels, shell: list[str]
) -> None:
    with TestClient(make_app(bootstrap)) as client:
        # list
        models.script = [tool("jobs_list"), "Seven checks are scheduled."]
        events = ask(client, "what do you check regularly?")
        [listed] = results(events)
        assert listed["ok"] and '"slug":"summary:daily"' in listed["preview"]
        offered = set(models.requests[0]["tools"])
        assert {"bash", "delegate", "jobs_list", "job_results", "job_save", "job_delete",
                "summary_member", "job_run"} <= offered  # fmt: skip

        # create (as the model) and change a builtin
        models.script = [
            tool(
                "job_save",
                slug="backups",
                name="Backups",
                prompt="Check that last night's restic backup finished.",
                cron="30 7 * * *",
            ),
            tool("job_save", slug="storage:usage", enabled=False),
            "Done: backups are checked daily at 7:30, storage is off.",
        ]
        events = ask(client, "add a daily backup check and stop the storage check")
        created, disabled = results(events)
        assert created["ok"] and '"saved":"created"' in created["preview"]
        assert disabled["ok"] and '"saved":"updated"' in disabled["preview"]
        job = client.get("/api/jobs/backups").json()
        assert job["created_by"] == "llm" and job["cron"] == "30 7 * * *"
        assert job["next_run"] is not None
        assert client.get("/api/jobs/storage:usage").json()["enabled"] is False
        call = next(e for e in events if e["type"] == "tool_call")
        assert call["kind"] == "write"

        # a bad job is explained back, nothing is saved
        models.script = [tool("job_save", slug="x1", name="X", prompt="short"), "Sorry."]
        [bad] = results(ask(client, "add a job x1"))
        assert not bad["ok"] and "invalid job" in bad["preview"]
        assert client.get("/api/jobs/x1").status_code == 404

        # run now: the finished result comes back in the same reply
        models.script = [
            tool("job_run", slug="docker:status"),
            tool("bash", command="docker ps"),
            finish("warn", "One container restarts often.", []),  # the job's own model turn
            "Docker: one container restarts often.",
        ]
        events = ask(client, "check docker now")
        [ran] = results(events)
        assert ran["ok"] and '"status":"warn"' in ran["preview"]
        assert '"trigger":"llm"' in ran["preview"]
        [run] = client.get("/api/jobs/docker:status/runs").json()["items"]
        assert run["trigger"] == "llm" and run["status"] == "warn"
        assert events[-1]["type"] == "done"

        # results: the job's, and the summary by default
        models.script = [tool("job_results", slug="docker:status"), tool("job_results"), "ok"]
        docker, summary = results(ask(client, "latest results?"))
        assert '"status":"warn"' in docker["preview"]
        assert '"summary:health":"not run yet"' in summary["preview"]

        # delete: builtins never
        models.script = [
            tool("job_delete", slug="storage:usage"),
            tool("job_delete", slug="backups"),
            "Removed the backup check; storage is a builtin and stays (disabled).",
        ]
        builtin, custom = results(ask(client, "delete storage and backups"))
        assert not builtin["ok"] and "builtin" in builtin["preview"]
        assert custom["ok"] and client.get("/api/jobs/backups").status_code == 404

        audit = client.get("/api/chat/audit", params={"kind": "jobs"}).json()["items"]
        assert [(a["tool"], a["slug"]) for a in reversed(audit)] == [
            ("job_save", "backups"),
            ("job_save", "storage:usage"),
            ("job_run", "docker:status"),
            ("job_delete", "backups"),
        ]


def test_the_chat_puts_a_job_into_a_summary_after_another_in_one_call(
    bootstrap: Bootstrap, models: ScriptedModels
) -> None:
    with TestClient(make_app(bootstrap)) as client:
        before = client.get("/api/jobs/summary:health").json()["members"]
        anchor = before[1]
        models.script = [
            tool(
                "job_save",
                slug="backups",
                name="Backups",
                prompt="Check that last night's backup run finished without errors.",
                interval_s=3600,
            ),
            tool("summary_member", summary="summary:health", job="backups", after=anchor),
            "Backups now run in Health, right after the second check.",
        ]
        events = ask(client, f"put backups into the health summary after {anchor}")
        _, joined = results(events)
        assert joined["ok"] and '"backups":"added"' in joined["preview"]
        assert "its own schedule was removed" in joined["preview"]  # the notice is repeated
        assert [e["kind"] for e in events if e["type"] == "tool_call"] == ["write", "write"]
        health = client.get("/api/jobs/summary:health").json()
        assert health["members"] == [*before[:2], "backups", *before[2:]]
        backups = client.get("/api/jobs/backups").json()
        assert backups["interval_s"] is None and backups["summaries"] == ["summary:health"]

        # moving it to the front (before the first member), then out again
        models.script = [
            tool("summary_member", summary="summary:health", job="backups", before=before[0]),
            tool("summary_member", summary="summary:health", job="backups", action="remove"),
            "Moved, then removed.",
        ]
        moved, removed = results(ask(client, "move backups first, then take it out"))
        assert moved["ok"] and '"backups":"moved"' in moved["preview"]
        assert f'"members":["backups","{before[0]}"' in moved["preview"]
        assert removed["ok"] and '"backups":"removed"' in removed["preview"]
        assert "in no summary now and has no schedule" in removed["preview"]
        assert client.get("/api/jobs/summary:health").json()["members"] == before

        # mistakes are explained back and change nothing
        models.script = [
            tool("summary_member", summary="summary:health", job="backups", after="nope"),
            tool("summary_member", summary="docker:status", job="backups"),
            tool("summary_member", summary="summary:health", job="backups", action="remove"),
            "Sorry.",
        ]
        bad_anchor, not_summary, not_member = results(ask(client, "do it wrong"))
        assert not bad_anchor["ok"] and "is not another member" in bad_anchor["preview"]
        assert not not_summary["ok"] and "a job, not a summary" in not_summary["preview"]
        assert not not_member["ok"] and "is not in summary:health" in not_member["preview"]
        assert client.get("/api/jobs/summary:health").json()["members"] == before

        audit = client.get("/api/chat/audit", params={"kind": "jobs"}).json()["items"]
        assert [(a["tool"], a.get("action")) for a in reversed(audit)] == [
            ("job_save", "created"),
            ("summary_member", "added"),
            ("summary_member", "moved"),
            ("summary_member", "removed"),
        ]


def test_the_chat_creates_changes_and_deletes_a_summary(
    bootstrap: Bootstrap, models: ScriptedModels
) -> None:
    with TestClient(make_app(bootstrap)) as client:
        models.script = [
            tool(
                "job_save",
                slug="summary:weekly",
                kind="summary",
                name="Weekly",
                prompt="Sum up the weekly checks in a few short lines.",
                cron="0 8 * * 1",
                members=["docker:status", "storage:usage"],
                notify="always",
            ),
            tool("job_save", slug="summary:weekly", notify="warn_crit"),
            tool("job_save", slug="summary:weekly", kind="job"),
            tool("job_delete", slug="summary:weekly"),
            tool("job_delete", slug="summary:daily"),
            "Done.",
        ]
        events = ask(client, "make a weekly summary of docker and storage, then drop it")
        created, notify, kind, deleted, builtin = results(events)
        assert created["ok"] and '"saved":"created"' in created["preview"]
        assert '"kind":"summary"' in created["preview"]
        assert '"members":["docker:status","storage:usage"]' in created["preview"]
        assert notify["ok"] and '"notify":"warn_crit"' in notify["preview"]
        assert not kind["ok"] and "the kind cannot change" in kind["preview"]
        assert deleted["ok"] and client.get("/api/jobs/summary:weekly").status_code == 404
        assert not builtin["ok"] and "builtin summary" in builtin["preview"]
        assert "disable it instead" in builtin["preview"]
        # the jobs of a deleted summary stay
        assert client.get("/api/jobs/docker:status").status_code == 200

        # jobs_list names kinds, members and notify plainly
        store = client.app.state.ctx.jobs.store  # type: ignore[union-attr]
        member = _job_line(client.portal.call(store.get, "docker:status"), None)  # type: ignore[union-attr]
        assert member["kind"] == "job" and member["summaries"] == [
            "summary:daily",
            "summary:health",
        ]
        assert member["notify"] == "nothing itself (its summary reports)"
        health = _job_line(client.portal.call(store.get, "summary:health"), None)  # type: ignore[union-attr]
        assert health["kind"] == "summary" and "docker:status" in health["members"]
