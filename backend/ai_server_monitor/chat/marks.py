"""Marks on the commands an answer shows: `# [read-only] ...` or `# [write] ...` above each.

The chat prompt asks for them; the web UI labels its Run button by them and the run endpoint
picks the shell by them (`RUN_MARK`). A model does not always write them, so after a chat
answer is final the commands of its shell blocks without a valid mark are given to the main
model once more (`classify`, structured output: `read-only` or `write` per command) and the
mark is written into the answer before it is stored (`add_marks`). If that call fails, the
commands stay unmarked: their button says "may change the server".

Blocks are found with markdown-it (CommonMark, as the UI's `marked`); `command_groups` is the
same split as `commandGroups` in `frontend/src/components/RunnableMarkdown.tsx`.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Literal

from markdown_it import MarkdownIt
from pydantic import BaseModel, Field

RUN_MARK = re.compile(r"^\s*#\s*\[(read-only|write)\]", re.IGNORECASE | re.MULTILINE)
"""How an answer marks a command it shows (chat prompt): `# [read-only] ...` or
`# [write] ...` on a comment line above it. The web UI reads the same mark for its label."""

SHELLS = frozenset({"bash", "sh", "shell", "zsh", "console"})
"""Code block tags that get Run buttons in the UI."""

_COMMENT = re.compile(r"^\s*#")
_TAG = re.compile(r"^(\s*#\s*)\[[^\]\n]*\]\s*")
"""A comment that starts with a tag in brackets, valid or not (`# [read] ...`)."""
_CONTINUED = re.compile(r"\\\s*$")

Kind = Literal["read-only", "write"]


def read_only_marked(command: str) -> bool:
    """Only a command the answer explicitly marked read-only (and not also write) goes to the
    read-only shell; a write or unmarked one runs through the change shell."""
    return {m.lower() for m in RUN_MARK.findall(command)} == {"read-only"}


def command_groups(text: str) -> list[list[int]]:
    """The commands of a shell block, as line indexes of `text`: groups split at blank lines
    and before a `#` comment that follows a command (its leading comments belong to the
    command). A line ending in `\\` continues the command."""
    groups: list[list[int]] = []
    current: list[int] = []
    continued = False
    lines = text.rstrip().split("\n")
    for index, line in enumerate(lines):
        if not continued and not line.strip():
            if current:
                groups.append(current)
            current = []
            continue
        if (
            not continued
            and _COMMENT.match(line)
            and current
            and not _COMMENT.match(lines[current[-1]])
        ):
            groups.append(current)
            current = []
        current.append(index)
        continued = bool(_CONTINUED.search(line))
    if current:
        groups.append(current)
    return groups


@dataclass
class Unmarked:
    """A command of an answer without a valid mark: where its lines are in the answer."""

    command: str
    source_lines: list[int]
    """Line numbers in the answer of the command's lines."""
    prefixes: list[str]
    """What stands before each line's code in the answer (list indent, `> `)."""


def unmarked_commands(answer: str) -> list[Unmarked]:
    """The commands in the answer's shell blocks that carry no valid mark."""
    source = answer.split("\n")
    found: list[Unmarked] = []
    for token in MarkdownIt("commonmark").parse(answer):
        if token.type != "fence" or token.map is None:
            continue
        tag = (token.info.strip().split() or [""])[0].lower()
        if tag not in SHELLS:
            continue
        code = token.content.split("\n")
        first = token.map[0] + 1  # the line after the opening fence
        for group in command_groups(token.content):
            text = "\n".join(code[i] for i in group)
            if RUN_MARK.search(text):
                continue
            rows = [first + i for i in group]
            prefixes = []
            for i, row in zip(group, rows, strict=True):
                line = source[row]
                prefixes.append(line[: len(line) - len(code[i])] if line.endswith(code[i]) else "")
            found.append(Unmarked(text, rows, prefixes))
    return found


def add_marks(answer: str, commands: list[Unmarked], kinds: list[Kind]) -> str:
    """The answer with a mark on each command: a tag in brackets on its first comment line is
    replaced (`# [read] x` → `# [read-only] x`), a first comment line without one gets it
    (`# x` → `# [read-only] x`), else a mark line is put above the command."""
    lines = answer.split("\n")
    inserts: list[tuple[int, str]] = []
    for command, kind in zip(commands, kinds, strict=True):
        row, prefix = command.source_lines[0], command.prefixes[0]
        code = lines[row][len(prefix) :]
        if _COMMENT.match(code):
            rest = _TAG.sub(r"\1", code, count=1)
            head, text = rest.split("#", 1)
            lines[row] = f"{prefix}{head}# [{kind}] {text.strip()}".rstrip()
        else:
            indent = code[: len(code) - len(code.lstrip())]
            inserts.append((row, f"{prefix}{indent}# [{kind}]"))
    for row, line in sorted(inserts, reverse=True):
        lines.insert(row, line)
    return "\n".join(lines)


class Marks(BaseModel):
    """For each numbered command, in the same order: read-only or write."""

    kinds: list[Kind] = Field(
        description="One entry per command, in the order given: read-only or write."
    )


MARKS_SYSTEM = (
    "You label shell commands for the owner of a Linux server. For each numbered command say "
    "`write` if running it changes anything: files, packages, services, containers, settings "
    "or any other state (delete, move, copy, write, install, remove, start, stop, restart, "
    "kill, mount, chmod, edit, prune, ...). Say `read-only` if it only shows or reads. "
    "Answer with one entry per command, in the same order."
)


def marks_prompt(commands: list[Unmarked]) -> str:
    parts = [f"Command {n}:\n```bash\n{c.command}\n```" for n, c in enumerate(commands, 1)]
    return f"{len(commands)} commands.\n\n" + "\n\n".join(parts)
