"""Prompt cards: the chat model asks the owner questions, its run waits for the answers and
continues when they come (web chat; Telegram has its own tests)."""

from __future__ import annotations

import time
from typing import Any

import pytest
from fastapi.testclient import TestClient

from ai_server_monitor.chat.cards import (
    Answer,
    Question,
    check_answers,
    format_answers,
)
from ai_server_monitor.config import Bootstrap

from .jobfakes import ScriptedModels, finish, tool
from .test_chat import ask
from .test_jobs import make_app

QUESTIONS = [
    {"text": "Which disk?", "kind": "single", "options": ["sda", "sdb"]},
    {"text": "Which checks?", "kind": "multi", "options": ["smart", "fsck", "scrub"]},
    {"text": "Anything else?", "kind": "text"},
]
ANSWERS = [
    {"chosen": ["sdb"]},
    {"chosen": ["smart", "scrub"], "text": "and the cables"},
    {"text": "no"},
]


def wait_for_answer(client: TestClient, session_id: str, text: str) -> list[dict[str, Any]]:
    deadline = time.monotonic() + 10
    while True:
        messages = client.get(f"/api/chat/sessions/{session_id}").json()["messages"]
        if messages[-1]["role"] == "assistant" and messages[-1]["content"] == text:
            return messages
        assert time.monotonic() < deadline, messages[-1]
        time.sleep(0.05)


def ask_owner(client: TestClient, models: ScriptedModels) -> list[dict[str, Any]]:
    models.script = [tool("ask_owner", questions=QUESTIONS)]
    return ask(client, "check my disks")


def test_the_model_asks_a_card_and_the_run_continues_with_the_answers(
    bootstrap: Bootstrap, models: ScriptedModels
) -> None:
    app = make_app(bootstrap)
    with TestClient(app) as client:
        sub = app.state.ctx.events.subscribe("chat")
        events = ask_owner(client, models)
        assert "ask_owner" in models.requests[0]["tools"]
        call = next(e for e in events if e["type"] == "tool_call")
        assert call["name"] == "ask_owner"
        card = next(e for e in events if e["type"] == "card")["card"]
        assert card["status"] == "open" and card["questions"] == [
            {**q, "options": q.get("options", [])} for q in QUESTIONS
        ]
        done = events[-1]
        assert done["text"] == "Waiting for your answer." and done["cards"] == [card["id"]]
        assert len(models.requests) == 1  # the run waits: no second model request
        session = client.get(f"/api/chat/sessions/{done['session_id']}").json()
        assert [c["id"] for c in session["cards"]] == [card["id"]]
        opened = client.portal.call(sub.__anext__)  # type: ignore[union-attr]
        assert opened.type == "chat.card" and opened.data["id"] == card["id"]

        models.script = ["Checking sdb: smart and scrub, and the cables."]
        answered = client.post(f"/api/chat/cards/{card['id']}/answer", json={"answers": ANSWERS})
        assert answered.status_code == 200, answered.text
        body = answered.json()
        assert body["status"] == "answered" and body["answered_via"] == "web"
        assert body["answers"][1] == {"chosen": ["smart", "scrub"], "text": "and the cables"}
        messages = wait_for_answer(
            client, done["session_id"], "Checking sdb: smart and scrub, and the cables."
        )
        # the model read the answers as the result of its call
        text = models.requests[-1]["text"]
        assert "The owner answered:" in text and "Which disk?" in text and "sdb" in text
        assert "smart, scrub, and the cables (own answer)" in text
        # the waiting note was settled, the answered card is kept
        note = next(m for m in messages if m["id"] == done["message_id"])
        assert note["content"] == "Answered." and note["meta"]["cards"] == [card["id"]]
        assert messages[-1]["meta"]["continued"] is True
        kept = client.get(f"/api/chat/sessions/{done['session_id']}").json()["cards"]
        assert kept[0]["status"] == "answered" and kept[0]["answers"][0] == {
            "chosen": ["sdb"],
            "text": "",
        }
        types = []
        while len(types) < 3:
            types.append(client.portal.call(sub.__anext__).type)  # type: ignore[union-attr]
        assert "chat.card.answered" in types and "chat.message.updated" in types
        # no second pending run: answering again is refused
        again = client.post(f"/api/chat/cards/{card['id']}/answer", json={"answers": ANSWERS})
        assert again.status_code == 409


def test_wrong_answers_are_refused_and_the_card_stays_open(
    bootstrap: Bootstrap, models: ScriptedModels
) -> None:
    with TestClient(make_app(bootstrap)) as client:
        card = next(e for e in ask_owner(client, models) if e["type"] == "card")["card"]
        url = f"/api/chat/cards/{card['id']}/answer"
        assert client.post(url, json={"answers": ANSWERS[:2]}).status_code == 422
        bad = [{"chosen": ["sdc"]}, *ANSWERS[1:]]
        assert client.post(url, json={"answers": bad}).status_code == 422
        missing = client.post("/api/chat/cards/999/answer", json={"answers": ANSWERS})
        assert missing.status_code == 404
        [still] = client.get(f"/api/chat/sessions/{card['session_id']}").json()["cards"]
        assert still["status"] == "open"


def test_a_malformed_card_goes_back_to_the_model(
    bootstrap: Bootstrap, models: ScriptedModels
) -> None:
    with TestClient(make_app(bootstrap)) as client:
        models.script = [
            tool("ask_owner", questions=[{"text": "Pick", "kind": "single"}]),
            "Sorry, no card.",
        ]
        events = ask(client, "pick one")
        assert not any(e["type"] == "card" for e in events)
        assert "needs options" in models.requests[-1]["text"]
        assert events[-1]["text"] == "Sorry, no card."


def test_the_open_card_and_the_waiting_run_survive_a_restart(
    bootstrap: Bootstrap, models: ScriptedModels
) -> None:
    with TestClient(make_app(bootstrap)) as client:
        events = ask_owner(client, models)
        card = next(e for e in events if e["type"] == "card")["card"]
        session_id = events[-1]["session_id"]
    models.script = ["Went on after the restart."]
    with TestClient(make_app(bootstrap)) as client:
        [open_card] = client.get(f"/api/chat/sessions/{session_id}").json()["cards"]
        assert open_card["id"] == card["id"] and open_card["status"] == "open"
        assert models.script == ["Went on after the restart."]  # nothing resumed by itself
        client.post(f"/api/chat/cards/{card['id']}/answer", json={"answers": ANSWERS})
        wait_for_answer(client, session_id, "Went on after the restart.")
        assert "The owner answered:" in models.requests[-1]["text"]


def test_a_run_with_a_card_and_an_approval_waits_for_both(
    bootstrap: Bootstrap, models: ScriptedModels, root_shell: list[str]
) -> None:
    command = "systemctl restart nginx"
    with TestClient(make_app(bootstrap)) as client:
        sure = [{"text": "Sure?", "kind": "single", "options": ["yes", "no"]}]
        models.script = [
            [
                tool("ask_owner", questions=sure),
                tool("bash_write", command=command, reason="nginx hangs"),
            ],
            "Restarted after your yes.",
        ]
        events = ask(client, "restart nginx if I agree")
        done = events[-1]
        card = next(e for e in events if e["type"] == "card")["card"]
        assert "Waiting for approval #" in done["text"]
        assert "Waiting for your answer." in done["text"]
        # the answer alone does not continue the run: the command still waits
        answered = client.post(
            f"/api/chat/cards/{card['id']}/answer", json={"answers": [{"chosen": ["yes"]}]}
        )
        assert answered.status_code == 200
        time.sleep(0.3)
        assert len(models.requests) == 1 and root_shell == []
        [batch] = client.get("/api/approvals").json()["items"]
        client.post(f"/api/approvals/{batch['id']}", json={"decision": "approve"})
        wait_for_answer(client, done["session_id"], "Restarted after your yes.")
        assert root_shell == [command]
        text = models.requests[-1]["text"]
        assert "The owner answered:" in text and "exit code 0" in text


def test_jobs_and_the_worker_never_get_the_tool(
    bootstrap: Bootstrap, models: ScriptedModels
) -> None:
    app = make_app(bootstrap)
    with TestClient(app) as client:
        body = {"slug": "night", "kind": "summary", "name": "Night", "prompt": "Sum it up."}
        assert client.post("/api/jobs", json=body).status_code == 201
        models.script = [finish("ok", "Fine.")]
        client.portal.call(app.state.ctx.jobs.run, "night", "manual")  # type: ignore[union-attr]
        assert "ask_owner" not in models.requests[-1]["tools"]
        models.script = [tool("delegate", task="look around"), "found nothing", "All fine."]
        ask(client, "have a look")
        main, worker, _ = models.requests[-3:]
        assert "ask_owner" in main["tools"] and "ask_owner" not in worker["tools"]


def test_check_answers_and_the_text_the_model_reads() -> None:
    questions = [Question.model_validate(q) for q in QUESTIONS]
    answers = check_answers(questions, ANSWERS)
    assert answers[2] == Answer(text="no")
    text = format_answers(questions, answers)
    assert text.splitlines()[0] == "The owner answered:"
    assert "2. Which checks?\n   smart, scrub, and the cables (own answer)" in text
    assert "3. Anything else?\n   no" in text  # a text answer is not "own"
    with pytest.raises(ValueError, match="one answer expected"):
        check_answers(questions, [{"chosen": ["sda"], "text": "also sdb"}, *ANSWERS[1:]])
    with pytest.raises(ValueError, match="is not answered"):
        check_answers(questions, [*ANSWERS[:2], {"text": "  "}])
    with pytest.raises(ValueError, match="text question"):
        check_answers(questions, [*ANSWERS[:2], {"chosen": ["x"]}])
    with pytest.raises(ValueError, match="chosen twice"):
        check_answers(questions, [ANSWERS[0], {"chosen": ["fsck", "fsck"]}, ANSWERS[2]])
    with pytest.raises(ValueError, match="question 1"):
        check_answers(questions, [{"chosen": "sda"}, *ANSWERS[1:]])
