"""The notes store (`memory_notes`, `memory_note_domains`).

A note has a unique title (any case), a body, an area (MCP domains: the same ones a job has), and
who last wrote it: the main model in a chat session or in a job run, or the owner on the Memory
page. Nothing here limits how many notes there are, how long they are or how old: no retention
prunes them."""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any, Literal

from ..db import Database

Author = Literal["model", "owner"]


class MemoryError(Exception):
    """A request the store refuses, in words for the model or the owner."""


@dataclass(frozen=True)
class Writer:
    """Where the model wrote from: a chat session (`id` = its id) or a job run (`id` = the run
    id, `label` = the job's slug)."""

    kind: Literal["chat", "run"]
    id: str
    label: str | None = None


_SELECT = (
    "SELECT n.id, n.title, n.body, n.author, n.created_ts AS created, n.updated_ts AS updated, "
    "n.source_kind, n.source_id, n.source_label, "
    "(SELECT json_group_array(name) FROM (SELECT d.name FROM memory_note_domains nd "
    "JOIN mcp_domains d ON d.id = nd.domain_id WHERE nd.note_id = n.id ORDER BY d.name)) "
    "AS domains, "
    "CASE n.source_kind "
    "WHEN 'run' THEN EXISTS (SELECT 1 FROM job_runs r WHERE r.id = CAST(n.source_id AS INTEGER)) "
    "WHEN 'chat' THEN EXISTS (SELECT 1 FROM chat_sessions s WHERE s.id = n.source_id) "
    "END AS source_exists "
    "FROM memory_notes n"
)


def _note(row: Any) -> dict[str, Any]:
    import json

    source = None
    if row["source_kind"]:
        source = {
            "kind": row["source_kind"],
            "id": row["source_id"],
            "label": row["source_label"],
            "exists": bool(row["source_exists"]),
        }
    return {
        "id": row["id"],
        "title": row["title"],
        "body": row["body"],
        "author": row["author"],
        "created": row["created"],
        "updated": row["updated"],
        "domains": json.loads(row["domains"] or "[]"),
        "source": source,
    }


class MemoryStore:
    def __init__(self, db: Database):
        self.db = db

    # ------------------------------------------------------------------ reading

    async def notes(self, domains: list[str] | None = None) -> list[dict[str, Any]]:
        """Every note, newest change first; with `domains`: the notes that have one of them."""
        rows = await self.db.fetchall(f"{_SELECT} ORDER BY n.updated_ts DESC, n.id DESC")
        notes = [_note(r) for r in rows]
        if domains is None:
            return notes
        wanted = set(domains)
        return [n for n in notes if wanted & set(n["domains"])]

    async def by_title(self, title: str) -> dict[str, Any] | None:
        row = await self.db.fetchone(f"{_SELECT} WHERE n.title = ?", (title.strip(),))
        return _note(row) if row else None

    async def by_id(self, note_id: int) -> dict[str, Any] | None:
        row = await self.db.fetchone(f"{_SELECT} WHERE n.id = ?", (note_id,))
        return _note(row) if row else None

    async def domain_names(self) -> list[str]:
        rows = await self.db.fetchall("SELECT name FROM mcp_domains ORDER BY name")
        return [r["name"] for r in rows]

    async def _domain_ids(self, names: list[str]) -> list[int]:
        rows = await self.db.fetchall("SELECT id, name FROM mcp_domains ORDER BY name")
        known = {r["name"]: r["id"] for r in rows}
        unknown = sorted({n for n in names if n not in known})
        if unknown:
            existing = ", ".join(known) or "none yet"
            raise MemoryError(f"unknown domain {', '.join(unknown)} (existing domains: {existing})")
        return sorted({known[n] for n in names})

    # ------------------------------------------------------------------ the model's writes

    async def save(
        self, title: str, body: str, domains: list[str] | None, writer: Writer
    ) -> tuple[dict[str, Any], bool]:
        """The model writes a note: a new one, or the body (and area, when given) of the one with
        this title. Returns (the note, whether it is new). A note the owner edited is theirs: the
        model cannot change it."""
        title, body = title.strip(), body.strip()
        if not title:
            raise MemoryError("a note needs a title")
        if not body:
            raise MemoryError("a note needs a body (to forget a note, delete it)")
        ids = None if domains is None else await self._domain_ids(domains)
        existing = await self.by_title(title)
        now = time.time()
        if existing is not None and existing["author"] == "owner":
            raise MemoryError(
                f"the note {existing['title']!r} was edited by the owner: you can read it but not "
                "change it (write a different note if something is missing)"
            )
        async with self.db.transaction() as conn:
            if existing is None:
                cur = await conn.execute(
                    "INSERT INTO memory_notes (title, body, created_ts, updated_ts, source_kind, "
                    "source_id, source_label) VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (title, body, now, now, writer.kind, writer.id, writer.label),
                )
                note_id = cur.lastrowid
            else:
                note_id = existing["id"]
                await conn.execute(
                    "UPDATE memory_notes SET body = ?, updated_ts = ?, source_kind = ?, "
                    "source_id = ?, source_label = ? WHERE id = ?",
                    (body, now, writer.kind, writer.id, writer.label, note_id),
                )
            if ids is not None:
                await self._set_domains(conn, note_id, ids)
        saved = await self.by_id(int(note_id))
        assert saved is not None
        return saved, existing is None

    async def forget(self, title: str) -> dict[str, Any]:
        """The model deletes a note it may change (not one the owner edited)."""
        note = await self.by_title(title)
        if note is None:
            raise MemoryError(f"no note titled {title.strip()!r} (memory_list shows the titles)")
        if note["author"] == "owner":
            raise MemoryError(
                f"the note {note['title']!r} was edited by the owner: only the owner can delete it"
            )
        await self.db.write("DELETE FROM memory_notes WHERE id = ?", (note["id"],))
        return note

    # ------------------------------------------------------------------ the owner's edits

    async def edit(self, note_id: int, title: str, body: str, domains: list[str]) -> dict[str, Any]:
        """The owner changes a note; it is theirs from then on."""
        title, body = title.strip(), body.strip()
        if not title:
            raise MemoryError("a note needs a title")
        if not body:
            raise MemoryError("a note needs a body")
        ids = await self._domain_ids(domains)
        clash = await self.db.fetchone(
            "SELECT 1 FROM memory_notes WHERE title = ? AND id != ?", (title, note_id)
        )
        if clash:
            raise MemoryError(f"there is already a note titled {title!r}")
        async with self.db.transaction() as conn:
            cur = await conn.execute(
                "UPDATE memory_notes SET title = ?, body = ?, author = 'owner', updated_ts = ? "
                "WHERE id = ?",
                (title, body, time.time(), note_id),
            )
            if cur.rowcount == 0:
                raise MemoryError("no such note")
            await self._set_domains(conn, note_id, ids)
        saved = await self.by_id(note_id)
        assert saved is not None
        return saved

    async def delete(self, note_id: int) -> None:
        if not await self.db.write("DELETE FROM memory_notes WHERE id = ?", (note_id,)):
            raise MemoryError("no such note")

    @staticmethod
    async def _set_domains(conn: Any, note_id: int, domain_ids: list[int]) -> None:
        await conn.execute("DELETE FROM memory_note_domains WHERE note_id = ?", (note_id,))
        await conn.executemany(
            "INSERT INTO memory_note_domains (note_id, domain_id) VALUES (?, ?)",
            [(note_id, d) for d in domain_ids],
        )
