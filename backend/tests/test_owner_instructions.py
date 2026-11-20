"""The owner's instructions (Settings -> General -> instructions): one text, added as a section
of its own after the built-in instructions of the chat, its worker, topic jobs and summaries;
not to the utility calls (command marks, domain classification, MCP tool sorting)."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from ai_server_monitor.agent.prompts import chat_prompt, worker_prompt
from ai_server_monitor.chat.marks import MARKS_SYSTEM
from ai_server_monitor.config import Bootstrap, Settings
from ai_server_monitor.jobs.prompts import summary_instructions, topic_instructions

from .jobfakes import ScriptedModels, finish, tool
from .test_chat_run import frames
from .test_jobs import make_app, run

OWNER = (
    "Отвечай коротко.\n\n"
    "The disks under /mnt/disks are an Unraid array: never suggest `rm` there.\n"
    "Use {braces} and 100 % as written."
)
HEADING = "## The owner's instructions"

SECTION = f"<owner_instructions>\n{OWNER}\n</owner_instructions>"


def follows(instructions: str, built_in: str) -> bool:
    """The section is in the run's instructions after the built-in text (only the run's own
    dynamic "Now: ..." line may come after it)."""
    return instructions.index(built_in) < instructions.index(HEADING) < instructions.index(SECTION)


PROMPTS = {
    "chat": lambda owner: chat_prompt("box", "en", owner=owner),
    "worker": lambda owner: worker_prompt("box", "en", owner=owner),
    "topic": lambda owner: topic_instructions("box", "en", "Check the disks.", owner),
    "summary": lambda owner: summary_instructions("box", "en", "Write the picture.", owner),
}


@pytest.mark.parametrize("name", PROMPTS)
def test_the_text_is_a_delimited_section_at_the_end(name: str) -> None:
    built_in = PROMPTS[name]("")
    with_owner = PROMPTS[name](OWNER)
    assert with_owner.startswith(built_in)
    section = with_owner[len(built_in) :]
    assert section.startswith(f"\n\n{HEADING}\n")
    assert section.endswith(f"<owner_instructions>\n{OWNER}\n</owner_instructions>")


@pytest.mark.parametrize("name", PROMPTS)
@pytest.mark.parametrize("empty", ["", "  \n\n "])
def test_no_text_adds_nothing(name: str, empty: str) -> None:
    assert PROMPTS[name](empty) == PROMPTS[name]("")
    assert "owner_instructions" not in PROMPTS[name](empty)


def test_the_job_prompt_stays_before_the_owner_section() -> None:
    text = topic_instructions("box", "en", "Check the disks.", OWNER)
    assert text.index("The check:\nCheck the disks.") < text.index(HEADING)


def test_the_turn_notes_come_before_the_owner_section() -> None:
    text = chat_prompt("box", "en", ["NOTE ONE", "NOTE TWO"], owner=OWNER)
    assert text.startswith(chat_prompt("box", "en") + "\nNOTE ONE\nNOTE TWO\n\n" + HEADING)


def test_the_setting_is_a_multiline_text_that_starts_empty() -> None:
    assert Settings().general.instructions == ""
    schema = Settings.model_json_schema(mode="serialization")
    field = schema["$defs"]["GeneralSettings"]["properties"]["instructions"]
    assert field["type"] == "string" and field["format"] == "multiline"
    assert "maxLength" not in field


def test_chat_worker_get_the_text_and_the_marks_call_does_not(
    bootstrap: Bootstrap, models: ScriptedModels, shell: list[str]
) -> None:
    models.script = [
        tool("delegate", task="How full is /?"),  # the chat turn
        tool("bash", command="df -h /"),  # the worker
        "/ is 41 % full.",  # the worker's answer
        "It is 41 % full. Check it with:\n\n```bash\ndf -h /\n```",  # the chat's answer
        ("final_result", {"kinds": ["read-only"]}),  # the marks call
    ]
    with TestClient(make_app(bootstrap, general={"instructions": OWNER})) as client:
        response = client.post("/api/chat", json={"message": "how full is the disk?"})
        assert frames(response.text)[-1]["type"] == "done"
    chat, worker, _, answer, marks = (r["instructions"] for r in models.requests)
    assert follows(chat, "Answer in English.") and follows(answer, "Answer in English.")
    assert follows(worker, "nobody can answer them. Answer in English.")
    assert marks == MARKS_SYSTEM


def test_chat_without_the_text_has_no_section(bootstrap: Bootstrap, models: ScriptedModels) -> None:
    models.script = ["Hello."]
    with TestClient(make_app(bootstrap)) as client:
        client.post("/api/chat", json={"message": "hi"})
    assert "owner_instructions" not in models.requests[0]["instructions"]


@pytest.mark.parametrize("owner", [OWNER, ""])
def test_topic_and_summary_runs(
    bootstrap: Bootstrap, models: ScriptedModels, shell: list[str], owner: str
) -> None:
    with TestClient(make_app(bootstrap, general={"instructions": owner})) as client:
        models.script = [tool("bash", command="df -h /"), finish("ok", "Fine.")]
        run(client, "storage")
        topic = models.requests[-1]["instructions"]
        models.script = [finish("ok", "Fine.")]
        run(client, "summary")
        summary = models.requests[-1]["instructions"]
    assert "Disk space and inodes" in topic and "Write the overall picture" in summary
    if owner:
        assert follows(topic, "Disk space and inodes") and follows(summary, "overall picture")
    else:
        assert "owner_instructions" not in topic + summary
