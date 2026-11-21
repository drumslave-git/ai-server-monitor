"""HTTP API of the Memory page: the notes, edited and deleted by the owner (the model writes
them through its tools)."""

from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, HTTPException, Request, Response
from pydantic import BaseModel, Field

from ..context import AppContext
from .store import MemoryError

router = APIRouter()


class NoteSource(BaseModel):
    """What the model last wrote the note in."""

    kind: Literal["chat", "run"]
    id: str = Field(description="The chat session's id, or the job run's id.")
    label: str | None = Field(default=None, description="A run: the job's slug.")
    exists: bool = Field(description="False once the chat or the run was deleted or pruned.")


class Note(BaseModel):
    id: int
    title: str
    body: str
    author: Literal["model", "owner"] = Field(
        description="owner: the owner edited it, the model can read it but not change it."
    )
    created: float
    updated: float
    domains: list[str]
    source: NoteSource | None = None


class NoteList(BaseModel):
    items: list[Note]
    domains: list[str] = Field(description="Every MCP domain, for the area of a note.")


class NoteEdit(BaseModel):
    title: str
    body: str
    domains: list[str] = Field(default_factory=list)


def _ctx(request: Request) -> AppContext:
    return request.app.state.ctx


@router.get("/api/memory")
async def list_notes(request: Request) -> NoteList:
    """Every note, the newest change first, and the domains a note can be given."""
    store = _ctx(request).memory
    return NoteList(
        items=[Note.model_validate(n) for n in await store.notes()],
        domains=await store.domain_names(),
    )


@router.put("/api/memory/{note_id}")
async def edit_note(request: Request, note_id: int, body: NoteEdit) -> Note:
    """The owner changes a note (title, text, domains). It is the owner's from then on: the model
    can read it but not change or delete it."""
    ctx = _ctx(request)
    try:
        note = await ctx.memory.edit(note_id, body.title, body.body, body.domains)
    except MemoryError as exc:
        status = 404 if str(exc) == "no such note" else 422
        raise HTTPException(status_code=status, detail=str(exc)) from None
    ctx.events.publish("memory.changed", {"title": note["title"]})
    return Note.model_validate(note)


@router.delete("/api/memory/{note_id}", status_code=204)
async def delete_note(request: Request, note_id: int) -> Response:
    ctx = _ctx(request)
    try:
        await ctx.memory.delete(note_id)
    except MemoryError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from None
    ctx.events.publish("memory.changed", {})
    return Response(status_code=204)
