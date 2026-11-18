"""The chat's job tools: jobs_list, job_results, job_save, job_delete, job_run."""

from __future__ import annotations

from typing import Any

from fastapi.testclient import TestClient

from ai_server_monitor.config import Bootstrap

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
        assert listed["ok"] and '"slug":"docker"' in listed["preview"]
        offered = set(models.requests[0]["tools"])
        assert {"bash", "delegate", "jobs_list", "job_results", "job_save", "job_delete",
                "job_run"} <= offered  # fmt: skip

        # create (as the model) and change a builtin
        models.script = [
            tool(
                "job_save",
                slug="backups",
                name="Backups",
                prompt="Check that last night's restic backup finished.",
                cron="30 7 * * *",
            ),
            tool("job_save", slug="storage", enabled=False),
            "Done: backups are checked daily at 7:30, storage is off.",
        ]
        events = ask(client, "add a daily backup check and stop the storage check")
        created, disabled = results(events)
        assert created["ok"] and '"saved":"created"' in created["preview"]
        assert disabled["ok"] and '"saved":"updated"' in disabled["preview"]
        job = client.get("/api/jobs/backups").json()
        assert job["created_by"] == "llm" and job["cron"] == "30 7 * * *"
        assert job["next_run"] is not None
        assert client.get("/api/jobs/storage").json()["enabled"] is False
        call = next(e for e in events if e["type"] == "tool_call")
        assert call["kind"] == "write"

        # a bad job is explained back, nothing is saved
        models.script = [tool("job_save", slug="x1", name="X", prompt="short"), "Sorry."]
        [bad] = results(ask(client, "add a job x1"))
        assert not bad["ok"] and "invalid job" in bad["preview"]
        assert client.get("/api/jobs/x1").status_code == 404

        # run now: the finished result comes back in the same reply
        models.script = [
            tool("job_run", slug="docker"),
            tool("bash", command="docker ps"),
            finish("warn", "One container restarts often.", []),  # the job's own model turn
            "Docker: one container restarts often.",
        ]
        events = ask(client, "check docker now")
        [ran] = results(events)
        assert ran["ok"] and '"status":"warn"' in ran["preview"]
        assert '"trigger":"llm"' in ran["preview"]
        [run] = client.get("/api/jobs/docker/runs").json()["items"]
        assert run["trigger"] == "llm" and run["status"] == "warn"
        assert events[-1]["type"] == "done"

        # results: the job's, and the summary by default
        models.script = [tool("job_results", slug="docker"), tool("job_results"), "ok"]
        docker, summary = results(ask(client, "latest results?"))
        assert '"status":"warn"' in docker["preview"]
        assert "summary has not run yet" in summary["preview"]

        # delete: builtins never
        models.script = [
            tool("job_delete", slug="storage"),
            tool("job_delete", slug="backups"),
            "Removed the backup check; storage is a builtin and stays (disabled).",
        ]
        builtin, custom = results(ask(client, "delete storage and backups"))
        assert not builtin["ok"] and "builtin" in builtin["preview"]
        assert custom["ok"] and client.get("/api/jobs/backups").status_code == 404

        audit = client.get("/api/chat/audit", params={"kind": "jobs"}).json()["items"]
        assert [(a["tool"], a["slug"]) for a in reversed(audit)] == [
            ("job_save", "backups"),
            ("job_save", "storage"),
            ("job_run", "docker"),
            ("job_delete", "backups"),
        ]
