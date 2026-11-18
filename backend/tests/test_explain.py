"""The Explain button: a new chat that starts with the source data of a run or a finding."""

from __future__ import annotations

import json
import time
from datetime import datetime
from typing import Any

import pytest
from fastapi.testclient import TestClient

from ai_server_monitor.chat.explain import Command, compose, host_zone, zone_name
from ai_server_monitor.config import Bootstrap
from ai_server_monitor.tracing import codec

from .jobfakes import ScriptedModels, finish, tool
from .test_jobs import FINDING, make_app, run
from .test_telegram import FakeTelegram, press, running, wait_for
from .test_telegram import make_app as telegram_app

EVIDENCE = FINDING["evidence"][0]


def explain(client: TestClient, run_id: int, finding: int | None = None) -> list[dict[str, Any]]:
    response = client.post("/api/chat/explain", json={"run_id": run_id, "finding": finding})
    assert response.status_code == 200, response.text
    events = []
    for frame in response.text.strip().split("\n\n"):
        lines = dict(line.split(": ", 1) for line in frame.splitlines() if ": " in line)
        events.append({"type": lines["event"], **json.loads(lines["data"])})
    return events


def session_of(client: TestClient, events: list[dict[str, Any]]) -> dict[str, Any]:
    session_id = next(e["session_id"] for e in events if e["type"] == "session")
    return client.get(f"/api/chat/sessions/{session_id}").json()


def flush(client: TestClient) -> None:
    client.portal.call(client.app.state.ctx.tracing.flush)  # type: ignore[union-attr]


def test_explain_a_finding_and_a_run_starts_a_chat_with_the_source_data(
    bootstrap: Bootstrap, models: ScriptedModels, shell: list[str]
) -> None:
    models.script = [
        tool("bash", command="df -h /"),
        finish("warn", "Root is 40% full.", [FINDING]),
        tool("bash", command="df -h /"),  # an explain turn looks at the current state first
        "It is the root disk; 60G are free.",
        tool("bash", command="df -h /"),
        "The storage check looked at one disk.",
    ]
    with TestClient(make_app(bootstrap)) as client:
        result = run(client, "storage")
        flush(client)
        events = explain(client, result["id"], 0)
        assert events[0]["type"] == "session" and events[-1]["type"] == "done"
        assert events[0]["explain"]["label"] == "Explain: / is 40% full"
        assert "".join(e["text"] for e in events if e["type"] == "token").startswith("It is the")
        session = session_of(client, events)
        assert session["title"] == "Explain: / is 40% full"  # no extra model call for it
        first = session["messages"][0]
        assert first["role"] == "user"
        assert first["meta"]["explain"] == {
            "label": "Explain: / is 40% full",
            "run_id": result["id"],
            "job": "storage",
            "finding": 0,
        }
        text = first["content"]
        subject = text.index("## Subject: finding 1 of 1")
        assert text.index(EVIDENCE) > subject and "found in the command output" in text
        assert "Disk space and inodes" in text  # what the job checks
        assert "status: warn" in text and "Root is 40% full." in text
        assert "### 1. `df -h /`" in text and 'trust="untrusted"' in text
        assert "/dev/sda1  100G  40G  60G  40% /" in text.split("### 1.")[1]
        assert "Answer in English" in text and "bash_write" in text
        assert "about 300 words" in text and '"Guess:"' in text
        # The model got exactly that message, with the chat's tools.
        request = models.requests[2]
        assert "bash" in request["tools"] and "delegate" in request["tools"]
        assert "## Subject: finding 1 of 1" in request["text"]

        # The run as a whole: no subject, a label with the job and the time.
        events = explain(client, result["id"])
        session = session_of(client, events)
        # times in the host's time zone, named
        zone = host_zone(client.app.state.ctx)  # type: ignore[attr-defined]
        stamp = datetime.fromtimestamp(result["started"], zone).strftime("%Y-%m-%d %H:%M")
        assert session["title"] == f"Explain: Storage run from {stamp}"
        assert f"started {stamp}, finished " in session["messages"][0]["content"]
        assert f"({zone_name(zone)})" in session["messages"][0]["content"]
        assert "about 450 words" in session["messages"][0]["content"]
        assert "## Subject" not in session["messages"][0]["content"]
        assert session["messages"][0]["meta"]["explain"]["finding"] is None
        assert client.get("/api/chat/sessions").json()["total"] == 2


def test_an_explain_answer_without_a_look_goes_back_once(
    bootstrap: Bootstrap, models: ScriptedModels, shell: list[str]
) -> None:
    """Live: both explanations ran no command. An answer before any bash or delegate call is
    sent back with the request to look first; the second answer is kept either way."""
    models.script = [
        "It is probably the Telegram downloads.",  # no look: sent back
        tool("bash", command="df -h /"),
        "Now: / is at 40%, unchanged since the run.",
        "Without a look again.",  # a follow-up question in the same chat: no look required
    ]
    with TestClient(make_app(bootstrap)) as client:
        run_id = seed_run(client, "storage", status="warn", findings=[FINDING])
        events = explain(client, run_id, 0)
        assert events[-1]["type"] == "done"
        assert events[-1]["text"] == "Now: / is at 40%, unchanged since the run."
        assert shell == ["df -h /"]
        assert "run at least one read-only command" in models.requests[1]["text"]
        session_id = events[0]["session_id"]
        follow = client.post("/api/chat", json={"message": "and?", "session_id": session_id})
        assert "Without a look again." in follow.text


def seed_run(client: TestClient, slug: str, turn: str | None = None, **fields: Any) -> int:
    store = client.app.state.ctx.jobs.store  # type: ignore[union-attr]
    run_id = client.portal.call(store.start_run, slug, "schedule", "big")  # type: ignore[union-attr]

    async def finish_run() -> None:
        await store.finish_run(run_id, trace_turn=turn, **fields)

    client.portal.call(finish_run)  # type: ignore[union-attr]
    return int(run_id)


def test_an_error_run_is_explained_from_its_error(
    bootstrap: Bootstrap, models: ScriptedModels
) -> None:
    models.script = ["The run timed out because du walked a big disk."]
    with TestClient(make_app(bootstrap)) as client:
        run_id = seed_run(
            client, "journal", "t-err", status="error", error="timed out after 900s (limit 900s)"
        )
        events = explain(client, run_id)
        assert events[-1]["type"] == "done"
        text = session_of(client, events)["messages"][0]["content"]
        assert "status: error" in text and "error: timed out after 900s" in text
        assert "(it ended with an error)" in text
        assert "none recorded (the run ran none, or its trace has been pruned)" in text


def test_a_summary_run_also_gets_the_topic_results_it_was_given(
    bootstrap: Bootstrap, models: ScriptedModels
) -> None:
    models.script = ["Explained."]
    given = "Latest results of the topic checks (now 2026-11-18 10:00):\n## Storage\nwarn 88%"
    with TestClient(make_app(bootstrap)) as client:
        run_id = seed_run(client, "summary", "t-sum", status="ok", summary="All fine.", findings=[])
        messages = [{"role": "system", "content": "You write"}, {"role": "user", "content": given}]
        bash = {"command": "uptime"}

        async def trace() -> None:
            db = client.app.state.ctx.db  # type: ignore[union-attr]
            await db.execute(
                "INSERT INTO llm_calls (ts, purpose, turn, status, messages) "
                "VALUES (?, 'job', 't-sum', 'ok', ?)",
                (time.time(), codec.pack(json.dumps(messages))),
            )
            await db.execute(
                "INSERT INTO llm_tool_calls (ts, turn, tool, ok, args, result) "
                "VALUES (?, 't-sum', 'worker:bash', 1, ?, ?)",
                (time.time(), codec.pack(json.dumps(bash)), codec.pack("up 3 days")),
            )

        client.portal.call(trace)  # type: ignore[union-attr]
        events = explain(client, run_id)
        text = session_of(client, events)["messages"][0]["content"]
        section = text.split("## Topic results this summary was given")[1]
        assert "warn 88%" in section.split("## Commands")[0]
        assert "### 1. `uptime` (by the worker model)" in text and "up 3 days" in text


def test_missing_runs_and_findings_are_404_and_a_running_run_409(
    bootstrap: Bootstrap, models: ScriptedModels
) -> None:
    with TestClient(make_app(bootstrap)) as client:
        missing = client.post("/api/chat/explain", json={"run_id": 999})
        assert missing.status_code == 404 and "no longer exists" in missing.json()["detail"]
        run_id = seed_run(client, "storage", status="warn", findings=[FINDING])
        bad = client.post("/api/chat/explain", json={"run_id": run_id, "finding": 1})
        assert bad.status_code == 404 and "no finding 1" in bad.json()["detail"]
        negative = client.post("/api/chat/explain", json={"run_id": run_id, "finding": -1})
        assert negative.status_code == 422
        store = client.app.state.ctx.jobs.store  # type: ignore[union-attr]
        running_id = client.portal.call(store.start_run, "docker", "manual", "small")  # type: ignore[union-attr]
        busy = client.post("/api/chat/explain", json={"run_id": running_id})
        assert busy.status_code == 409
        assert client.get("/api/chat/sessions").json()["total"] == 0


def test_the_context_budget_keeps_the_subject_and_its_evidence_and_cuts_outputs() -> None:
    noise = "".join(f"line {i} of a long unrelated listing\n" for i in range(3000))
    quoted = "head\n" + "x" * 30000 + f"\n{EVIDENCE}\n" + "y" * 30000
    commands = [
        Command("journalctl -n 5000", noise),  # the oldest, unrelated: left out first
        Command("df -h /", quoted),  # holds the evidence line
        Command("ls /var", "a\nb\n"),
    ]
    job = {"slug": "storage", "name": "Storage", "kind": "topic", "prompt": "Check disks."}
    finding = {**FINDING, "verified": True}
    run_row = {"id": 7, "status": "warn", "summary": "Root.", "findings": [finding]}
    # A tight budget: every output is cut in the middle to a fair share first.
    cut = compose(job, run_row, 0, commands, None, 12000, "en")
    assert len(cut) < 13000 and "left out" not in cut
    assert cut.count("characters cut here") == 2 and "a\nb" in cut
    # Tighter still: the oldest output the finding does not quote is left out entirely.
    text = compose(job, run_row, 0, commands, None, 2000, "ru")
    assert len(text) < 5000
    subject = text.split("## Job:")[0]
    assert EVIDENCE in subject and "60G free" in subject  # the subject stays whole
    assert "(output left out to fit the context)" in text.split("### 2.")[0]
    assert "outputs of 1 command(s) (1) were left out" in text
    df = text.split("### 2. `df -h /`")[1].split("### 3.")[0]
    assert "head" in df and "characters cut here" in df
    assert "a\nb" in text.split("### 3. `ls /var`")[1]
    assert "Answer in Russian" in text
    # Enough room: nothing is cut.
    whole = compose(job, run_row, 0, commands, None, 10_000_000, "en")
    assert noise in whole and quoted in whole and "left out" not in whole


@pytest.mark.usefixtures("skip_evidence")
def test_telegram_summary_has_an_explain_button_that_answers_in_the_chat(
    bootstrap: Bootstrap, models: ScriptedModels, shell: list[str]
) -> None:
    models.script = [
        finish("warn", "Disk is filling up.", [FINDING]),
        tool("bash", command="df -h /"),
        "The disk explanation.",
    ]
    tg = FakeTelegram()
    app = telegram_app(bootstrap, tg)
    with running(app, tg) as client:
        run_id = client.post("/api/jobs/summary/run").json()["run_id"]
        sent = wait_for(lambda: tg.of("sendMessage"))
        keyboard = sent[0]["reply_markup"]["inline_keyboard"]
        assert [[b["text"] for b in row] for row in keyboard] == [["Explain"]]
        data = keyboard[0][0]["callback_data"]
        assert data.startswith(f"x:{run_id}:")
        tg.feed(press(data[:-1] + ("0" if data[-1] != "0" else "1"), 1001))  # forged
        wait_for(lambda: tg.of("answerCallbackQuery"))
        assert tg.of("answerCallbackQuery")[0]["text"] == "Invalid button."
        tg.feed(press(data, 1001))
        wait_for(lambda: len(tg.texts()) >= 3)
        label, answer = tg.texts()[1:]
        assert label.startswith("Explain: Summary run from ") and answer == "The disk explanation."
        sessions = client.get("/api/chat/sessions").json()["items"]
        assert len(sessions) == 1 and sessions[0]["channel"] == "telegram"
        assert sessions[0]["title"].startswith("Explain: Summary run from ")
