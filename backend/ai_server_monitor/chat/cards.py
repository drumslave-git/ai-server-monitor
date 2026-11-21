"""Prompt cards: the chat model asks the owner questions (web chat or Telegram) and its run
waits for the answer. The tool raises Pydantic AI's `CallDeferred(kind="question")`; the stopped
run is stored (`chat_pending_runs`, see `ChatEngine`) and the card here is its open question."""

from __future__ import annotations

import json
import time
from typing import TYPE_CHECKING, Any, Literal

from pydantic import BaseModel, Field, ValidationError, model_validator
from pydantic_ai import CallDeferred

from ..agent.runtime import AgentTool

if TYPE_CHECKING:
    from ..db import Database

MAX_QUESTIONS = 4

ASK_DESCRIPTION = (
    "Ask the owner one to four questions as a card and wait for the answers: use it when you "
    "need a decision or a detail only the owner can give (which of several options, a value "
    "you cannot find out yourself). Each question is `single` (one of the options), `multi` "
    "(any of the options) or `text` (free text, no options); the owner can always write an "
    "own answer to a choice question too. The answers come back as this tool's result."
)


class Question(BaseModel):
    text: str = Field(min_length=1, description="The question, a short sentence.")
    kind: Literal["single", "multi", "text"] = Field(
        "single", description="single: one option; multi: any options; text: free text."
    )
    options: list[str] = Field(
        default_factory=list,
        description="The choices of a single or multi question (empty for a text question).",
    )

    @model_validator(mode="after")
    def _check(self) -> Question:
        if self.kind == "text" and self.options:
            raise ValueError("a text question has no options")
        if self.kind != "text" and not self.options:
            raise ValueError(f"a {self.kind} question needs options")
        if len(set(self.options)) != len(self.options):
            raise ValueError("the options of a question differ")
        return self


class Card(BaseModel):
    questions: list[Question] = Field(min_length=1, max_length=MAX_QUESTIONS)


class Answer(BaseModel):
    chosen: list[str] = Field(default_factory=list)
    """The options chosen (a choice question)."""
    text: str = ""
    """The free text: the answer of a text question, or the own answer of a choice question."""


def ask_tool() -> AgentTool:
    async def handler(args: dict[str, Any]) -> Any:
        Card.model_validate(args)  # a malformed card goes back to the model as the error text
        raise CallDeferred(kind="question")

    return AgentTool(
        name="ask_owner",
        description=ASK_DESCRIPTION,
        parameters=Card.model_json_schema(),
        handler=handler,
        asks=True,
    )


def check_answers(questions: list[Question], raw: list[dict[str, Any]]) -> list[Answer]:
    """The answers of a card validated against its questions; ValueError says what is wrong."""
    if len(raw) != len(questions):
        raise ValueError(f"{len(questions)} answers expected, got {len(raw)}")
    answers: list[Answer] = []
    for number, (question, item) in enumerate(zip(questions, raw, strict=True), start=1):
        try:
            answer = Answer.model_validate(item)
        except ValidationError as exc:
            raise ValueError(f"question {number}: {exc.errors()[0]['msg']}") from exc
        answer.text = answer.text.strip()
        if question.kind == "text":
            if answer.chosen:
                raise ValueError(f"question {number} is a text question")
        else:
            unknown = [c for c in answer.chosen if c not in question.options]
            if unknown:
                raise ValueError(f"question {number}: unknown option {unknown[0]!r}")
            if len(set(answer.chosen)) != len(answer.chosen):
                raise ValueError(f"question {number}: an option is chosen twice")
            if question.kind == "single" and len(answer.chosen) + bool(answer.text) > 1:
                raise ValueError(f"question {number}: one answer expected")
        if not answer.chosen and not answer.text:
            raise ValueError(f"question {number} is not answered")
        answers.append(answer)
    return answers


def answer_line(answer: Answer) -> str:
    """One answer in words: the chosen options, the own text."""
    parts = list(answer.chosen)
    if answer.text:
        parts.append(answer.text)
    return ", ".join(parts)


def format_answers(questions: list[Question], answers: list[Answer]) -> str:
    """What the model reads as the result of its `ask_owner` call."""
    lines = ["The owner answered:"]
    for number, (question, answer) in enumerate(zip(questions, answers, strict=True), start=1):
        own = " (own answer)" if answer.text and question.kind != "text" else ""
        lines.append(f"{number}. {question.text}\n   {answer_line(answer)}{own}")
    return "\n".join(lines)


def card_view(row: Any) -> dict[str, Any]:
    """A `chat_cards` row as the API and the events carry it."""
    return {
        "id": int(row["id"]),
        "session_id": row["session_id"],
        "call_id": row["call_id"],
        "status": row["status"],
        "questions": json.loads(row["questions"]),
        "answers": json.loads(row["answers"]) if row["answers"] else None,
        "created": row["created"],
        "answered": row["answered"],
        "answered_via": row["answered_via"],
        "telegram_error": row["telegram_error"],
    }


class Cards:
    """The `chat_cards` table."""

    def __init__(self, db: Database) -> None:
        self.db = db

    async def get(self, card_id: int) -> dict[str, Any] | None:
        row = await self.db.fetchone("SELECT * FROM chat_cards WHERE id = ?", (card_id,))
        return card_view(row) if row is not None else None

    async def run_of(self, card_id: int) -> int | None:
        """The stopped run the card holds (None: it was resumed already)."""
        row = await self.db.fetchone("SELECT run_id FROM chat_cards WHERE id = ?", (card_id,))
        return int(row["run_id"]) if row is not None and row["run_id"] is not None else None

    async def of_session(self, session_id: str) -> list[dict[str, Any]]:
        rows = await self.db.fetchall(
            "SELECT * FROM chat_cards WHERE session_id = ? ORDER BY id", (session_id,)
        )
        return [card_view(r) for r in rows]

    async def of_run(self, run_id: int) -> list[dict[str, Any]]:
        rows = await self.db.fetchall(
            "SELECT * FROM chat_cards WHERE run_id = ? ORDER BY id", (run_id,)
        )
        return [card_view(r) for r in rows]

    async def set_telegram_error(self, card_id: int, error: str | None) -> bool:
        """Record why the card could not be sent to Telegram (None: it was sent). True when the
        stored text changed."""
        changed = await self.db.write(
            "UPDATE chat_cards SET telegram_error = ? WHERE id = ? AND telegram_error IS NOT ?",
            (error, card_id, error),
        )
        return changed > 0

    async def answer(self, card_id: int, answers: list[Answer], via: str) -> bool:
        """Close an open card with its answers; False when it was not open (answered already:
        in the other place, perhaps)."""
        changed = await self.db.write(
            "UPDATE chat_cards SET status = 'answered', answers = ?, answered = ?, "
            "answered_via = ? WHERE id = ? AND status = 'open'",
            (
                json.dumps([a.model_dump() for a in answers], ensure_ascii=False),
                time.time(),
                via,
                card_id,
            ),
        )
        return changed > 0
