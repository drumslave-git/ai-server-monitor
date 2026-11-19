"""Marks on the commands of chat answers: commands of shell blocks without a valid
`# [read-only]` / `# [write]` mark are labelled by one structured call of the main model and
the mark is written into the answer before it is stored (`chat.marks`)."""

from __future__ import annotations

import logging

import pytest
from fastapi.testclient import TestClient

from ai_server_monitor.chat.marks import (
    MARKS_SYSTEM,
    add_marks,
    clean_stray_marks,
    command_groups,
    read_only_marked,
    unmarked_commands,
)
from ai_server_monitor.config import Bootstrap

from .jobfakes import ScriptedModels
from .test_chat_run import ask, frames, press
from .test_jobs import make_app

# The owner's case (live 0.7.3, turn 2881b097b22e): no comment at all, `\(` inside the line.
FIND = (
    'find "/mnt/disks/disk1TB_1/appdata/comfyui/ComfyUI/models" -type f \\( -name "RealVisXL*" '
    '-o -name "*flux-2-klein*" \\) 2>/dev/null | sort -h'
)
# Live turn c20fa89b9685: an invalid tag.
READ_TAG = '# [read] List all models we discussed (for verification)\nfind /models -name "*.st"'
# Live turn 01f9a1c07eea: a marked command over several lines.
RM = "# [write] Remove all target files (~76 GB)\nrm -rf /models/a \\\n       /models/b"


def answer(*blocks: str) -> str:
    return "Let's verify:\n\n" + "\n\n".join(f"```bash\n{b}\n```" for b in blocks) + "\n\nDone."


def test_groups_follow_the_web_ui() -> None:
    text = "# [read-only] a\ndf -h\ndu -sh /\n# [read-only] b\nls \\\n  /tmp\n\nuptime"
    assert command_groups(text) == [[0, 1, 2], [3, 4, 5], [7]]


def test_only_commands_without_a_valid_mark_are_found() -> None:
    found = unmarked_commands(answer(FIND, READ_TAG, RM) + "\n\n```python\nprint(1)\n```")
    assert [c.command for c in found] == [FIND, READ_TAG]


def test_marks_are_written_into_the_answer() -> None:
    text = answer(FIND, READ_TAG, "# Check disk usage\ndf -hT /", RM)
    found = unmarked_commands(text)
    marked = add_marks(text, found, ["read-only", "read-only", "read-only"])
    assert f"```bash\n# [read-only]\n{FIND}\n```" in marked
    assert "# [read-only] List all models we discussed (for verification)\nfind" in marked
    assert "# [read-only] Check disk usage\ndf -hT /" in marked
    assert RM in marked and unmarked_commands(marked) == []
    assert read_only_marked(f"# [read-only]\n{FIND}")


def test_marks_keep_the_indent_of_a_block_in_a_list() -> None:
    text = "1. First:\n\n   ```sh\n   systemctl restart nginx\n   ```\n"
    found = unmarked_commands(text)
    assert add_marks(text, found, ["write"]) == (
        "1. First:\n\n   ```sh\n   # [write]\n   systemctl restart nginx\n   ```\n"
    )


def test_an_unmarked_answer_is_stored_with_the_marks_of_the_extra_call(
    bootstrap: Bootstrap, models: ScriptedModels
) -> None:
    models.script = [
        answer(FIND, READ_TAG, "rm /tmp/x"),
        ("final_result", {"kinds": ["read-only", "read-only", "write"]}),
    ]
    with TestClient(make_app(bootstrap)) as client:
        response = client.post("/api/chat", json={"message": "are they gone?"})
        done = frames(response.text)[-1]
        session_id = done["session_id"]
        stored = client.get(f"/api/chat/sessions/{session_id}").json()["messages"][-1]
    assert done["type"] == "done" and done["text"] == stored["content"]
    assert f"# [read-only]\n{FIND}" in stored["content"]
    assert "# [read-only] List all models" in stored["content"]
    assert "# [write]\nrm /tmp/x" in stored["content"]
    # The chat turn has no marking rule; the extra call got the commands and what is write.
    assert "[read-only]" not in models.requests[0]["instructions"]
    assert models.requests[1]["instructions"] == MARKS_SYSTEM
    assert "Command 3:" in models.requests[1]["text"] and "RealVisXL" in models.requests[1]["text"]
    assert len(models.requests) == 2


def test_a_run_follow_up_is_marked_too(bootstrap: Bootstrap, models: ScriptedModels) -> None:
    models.script = [
        "ok",
        "Exit code 0. Let's verify they're gone:\n\n```bash\n" + FIND + "\n```",
        ("final_result", {"kinds": ["read-only"]}),
    ]
    with TestClient(make_app(bootstrap)) as client:
        session_id = ask(client, "hi")
        events = press(client, session_id, "# [read-only] list\nls /")
        stored = client.get(f"/api/chat/sessions/{session_id}").json()["messages"][-1]
    assert events[-1]["type"] == "done"
    assert f"```bash\n# [read-only]\n{FIND}\n```" in stored["content"]
    assert "[read-only]" not in models.requests[1]["instructions"]


@pytest.mark.parametrize(
    "reply",
    [
        ["not a tool call", "still text"],  # no structured result: the runtime's retry, then fails
        [("final_result", {"kinds": ["read-only", "write"]})],  # wrong length
    ],
)
def test_a_failed_extra_call_leaves_the_commands_unmarked_and_logs_it(
    bootstrap: Bootstrap,
    models: ScriptedModels,
    caplog: pytest.LogCaptureFixture,
    reply: list,
) -> None:
    text = answer(FIND)
    models.script = [text, *reply]
    with caplog.at_level(logging.WARNING), TestClient(make_app(bootstrap)) as client:
        response = client.post("/api/chat", json={"message": "are they gone?"})
        events = frames(response.text)
        session_id = events[0]["session_id"]
        stored = client.get(f"/api/chat/sessions/{session_id}").json()["messages"][-1]
    assert stored["content"] == text
    assert any("left unmarked" in r.getMessage() for r in caplog.records)
    # stored with the answer (and sent with `done`) for the line under it
    assert stored["meta"]["unmarked"]["commands"] == 1
    assert stored["meta"]["unmarked"]["error"]
    assert events[-1]["type"] == "done" and events[-1]["unmarked"] == stored["meta"]["unmarked"]


def test_an_answer_with_marked_commands_makes_no_extra_call(
    bootstrap: Bootstrap, models: ScriptedModels
) -> None:
    models.script = [answer(RM, "# [read-only] how full\ndf -h /")]
    with TestClient(make_app(bootstrap)) as client:
        client.post("/api/chat", json={"message": "x"})
    assert len(models.requests) == 1


# Live 0.7.5 (LLM calls 9893, 9903): marks written outside the code block render as headings.
STRAY = (
    "# [read-only] Commands to find the largest directories\n\n"
    "First, check the partitions:\n\n```bash\n# Show all filesystems\ndf -h\n```\n\n"
    "**How to confirm:**\n# [read-only] Verify disk usage in detail\n"
    "```bash\ndu -xh --max-depth=1 /mnt/disks/disk14TB | sort -hr | head -20\n```\n\n"
    "## Storage\n# [write]\nDone."
)


def test_stray_marks_outside_code_blocks_become_plain_text() -> None:
    cleaned = clean_stray_marks(STRAY)
    assert cleaned.startswith("Commands to find the largest directories\n\nFirst,")
    assert "**How to confirm:**\nVerify disk usage in detail\n```bash" in cleaned
    assert "## Storage\nDone." in cleaned  # a bare mark line goes
    assert "```bash\n# Show all filesystems\ndf -h\n```" in cleaned  # code blocks untouched
    assert clean_stray_marks("# Title\n\ntext") == "# Title\n\ntext"


def test_a_stray_mark_heading_is_cleaned_and_each_command_gets_one_mark(
    bootstrap: Bootstrap, models: ScriptedModels
) -> None:
    models.script = [STRAY, ("final_result", {"kinds": ["read-only", "read-only"]})]
    with TestClient(make_app(bootstrap)) as client:
        response = client.post("/api/chat", json={"message": "which disk is full?"})
        session_id = frames(response.text)[0]["session_id"]
        stored = client.get(f"/api/chat/sessions/{session_id}").json()["messages"][-1]["content"]
    assert stored.startswith("Commands to find the largest directories\n")
    assert "\nVerify disk usage in detail\n```bash\n# [read-only]\ndu -xh" in stored
    assert "```bash\n# [read-only] Show all filesystems\ndf -h\n```" in stored
    assert stored.count("[read-only]") == 2 and "[write]" not in stored


def test_prompts_carry_no_marking_rule() -> None:
    from ai_server_monitor.agent.prompts import chat_prompt
    from ai_server_monitor.chat.explain import REQUEST

    for text in (chat_prompt("host", "English"), REQUEST):
        assert "[read-only]" not in text and "[write]" not in text
