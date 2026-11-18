"""The `jobs` and `job_runs` tables."""

from __future__ import annotations

import json
import re
import time
from typing import Any

from ..db import Database
from .builtins import BUILTINS, BY_SLUG, JobDef

INTERRUPTED = "interrupted (the service stopped)"
"""The error of a run the service stopped in the middle of (it is rerun after the start)."""
SLUG = re.compile(r"^[a-z0-9][a-z0-9_-]{1,39}$")
EDITABLE = ("name", "prompt", "interval_s", "cron", "model", "timeout_s", "max_steps")
"""Fields whose change marks a builtin as edited (enabling/disabling does not)."""
_JOB_COLUMNS = (
    "slug, name, kind, prompt, interval_s, cron, model, enabled, builtin, edited, timeout_s, "
    "max_steps, created_by, created, updated"
)
_DOMAINS = (
    "(SELECT json_group_array(name) FROM (SELECT d.name FROM job_domains jd "
    "JOIN mcp_domains d ON d.id = jd.domain_id WHERE jd.job_id = jobs.id ORDER BY d.name)) "
    "AS domains"
)
"""The names of a job's MCP domains (a JSON array)."""


class JobError(Exception):
    """A request about jobs that cannot be done (unknown job, a builtin cannot be deleted...)."""


class UnknownDomain(JobError):
    """A domain name that does not exist (the message names the existing ones)."""


def job_dict(row: Any) -> dict[str, Any]:
    out = dict(row)
    for key in ("enabled", "builtin", "edited"):
        out[key] = bool(out[key])
    out["domains"] = json.loads(out.get("domains") or "[]")
    return out


def run_dict(row: Any) -> dict[str, Any]:
    out = dict(row)
    for key in ("findings", "topics"):
        raw = out.get(key)
        try:
            out[key] = json.loads(raw) if raw else []
        except ValueError:
            out[key] = []
    out["mcp"] = json.loads(out["mcp"]) if out.get("mcp") else None
    started, finished = out.get("started"), out.get("finished")
    out["duration_s"] = round(finished - started, 1) if finished and started else None
    return out


class JobStore:
    def __init__(self, db: Database):
        self.db = db

    # ------------------------------------------------------------------ jobs

    async def seed_builtins(self) -> None:
        """Insert missing builtins; bring unedited ones up to the definitions in code."""
        now = time.time()
        for d in BUILTINS:
            row = await self.db.fetchone("SELECT edited FROM jobs WHERE slug = ?", (d.slug,))
            if row is None:
                await self.db.execute(
                    f"INSERT INTO jobs ({_JOB_COLUMNS}) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, 1, 1, 0, NULL, ?, 'builtin', ?, ?)",
                    (
                        d.slug,
                        d.name,
                        d.kind,
                        d.prompt,
                        d.interval_s,
                        d.cron,
                        d.model,
                        d.max_steps,
                        now,
                        now,
                    ),
                )
            elif not row["edited"]:
                await self._apply_definition(d, now, only_if_changed=True)

    async def _apply_definition(self, d: JobDef, now: float, *, only_if_changed: bool) -> None:
        values = (d.name, d.prompt, d.interval_s, d.cron, d.model, None, d.max_steps)
        where = "slug = ? AND builtin = 1"
        if only_if_changed:
            where += (
                " AND (name IS NOT ? OR prompt IS NOT ? OR interval_s IS NOT ? OR cron IS NOT ?"
                " OR model IS NOT ? OR timeout_s IS NOT ? OR max_steps IS NOT ?)"
            )
        await self.db.execute(
            "UPDATE jobs SET name = ?, prompt = ?, interval_s = ?, cron = ?, model = ?, "
            f"timeout_s = ?, max_steps = ?, edited = 0, updated = ? WHERE {where}",
            (*values, now, d.slug, *(values if only_if_changed else ())),
        )

    async def list(self) -> list[dict[str, Any]]:
        rows = await self.db.fetchall(
            f"SELECT {_JOB_COLUMNS}, {_DOMAINS} FROM jobs ORDER BY builtin DESC, kind DESC, slug"
        )
        return [job_dict(r) for r in rows]

    async def get(self, slug: str) -> dict[str, Any] | None:
        row = await self.db.fetchone(
            f"SELECT {_JOB_COLUMNS}, {_DOMAINS} FROM jobs WHERE slug = ?", (slug,)
        )
        return job_dict(row) if row else None

    async def require(self, slug: str) -> dict[str, Any]:
        job = await self.get(slug)
        if job is None:
            raise JobError(f"no job {slug!r}")
        return job

    async def create(self, data: dict[str, Any], created_by: str) -> dict[str, Any]:
        slug = data["slug"]
        if not SLUG.match(slug):
            raise JobError("slug: 2-40 characters, lowercase letters, digits, - and _")
        if await self.get(slug) is not None:
            raise JobError(f"a job {slug!r} exists already")
        domain_ids = await self.domain_ids(data.get("domains") or [])
        now = time.time()
        await self.db.execute(
            f"INSERT INTO jobs ({_JOB_COLUMNS}) "
            "VALUES (?, ?, 'topic', ?, ?, ?, ?, ?, 0, 0, ?, ?, ?, ?, ?)",
            (
                slug,
                data["name"],
                data["prompt"],
                data.get("interval_s"),
                data.get("cron"),
                data.get("model", "worker"),
                int(data.get("enabled", True)),
                data.get("timeout_s"),
                data.get("max_steps"),
                created_by,
                now,
                now,
            ),
        )
        if domain_ids:
            await self._set_domains(slug, domain_ids)
        return await self.require(slug)

    async def update(self, slug: str, changes: dict[str, Any]) -> dict[str, Any]:
        """Apply the given fields (a schedule change sets interval_s/cron together)."""
        job = await self.require(slug)
        domains = changes.get("domains")
        if domains is not None and sorted(set(domains)) != job["domains"]:
            await self._set_domains(slug, await self.domain_ids(domains))
            if job["builtin"]:
                await self.db.execute("UPDATE jobs SET edited = 1 WHERE slug = ?", (slug,))
            job = await self.require(slug)
        changes = {k: v for k, v in changes.items() if k in (*EDITABLE, "enabled")}
        if "interval_s" in changes or "cron" in changes:
            changes.setdefault("interval_s", None)
            changes.setdefault("cron", None)
        if not changes:
            return job
        edited = job["builtin"] and any(k in EDITABLE and changes[k] != job[k] for k in changes)
        sets = [f"{k} = ?" for k in changes]
        params: list[Any] = [int(v) if k == "enabled" else v for k, v in changes.items()]
        if edited:
            sets.append("edited = 1")
        sets.append("updated = ?")
        await self.db.execute(
            f"UPDATE jobs SET {', '.join(sets)} WHERE slug = ?", (*params, time.time(), slug)
        )
        return await self.require(slug)

    async def delete(self, slug: str) -> None:
        job = await self.require(slug)
        if job["builtin"]:
            raise JobError(f"{slug!r} is a builtin job: disable it instead")
        await self.db.execute("DELETE FROM jobs WHERE slug = ?", (slug,))

    async def reset(self, slug: str) -> dict[str, Any]:
        job = await self.require(slug)
        definition = BY_SLUG.get(slug)
        if not job["builtin"] or definition is None:
            raise JobError(f"{slug!r} is not a builtin job")
        await self._apply_definition(definition, time.time(), only_if_changed=False)
        await self._set_domains(slug, [])  # builtins ship without domains
        return await self.require(slug)

    # ------------------------------------------------------------------ MCP domains

    async def domain_ids(self, names: list[str]) -> list[int]:
        """The ids of the named domains; an unknown name raises `UnknownDomain`."""
        rows = await self.db.fetchall("SELECT id, name FROM mcp_domains ORDER BY name")
        known = {r["name"]: r["id"] for r in rows}
        unknown = sorted({n for n in names if n not in known})
        if unknown:
            existing = ", ".join(known) or "none yet"
            raise UnknownDomain(
                f"unknown domain {', '.join(unknown)} (existing domains: {existing})"
            )
        return sorted({known[n] for n in names})

    async def _set_domains(self, slug: str, domain_ids: list[int]) -> None:
        async with self.db.transaction() as conn:
            async with conn.execute("SELECT id FROM jobs WHERE slug = ?", (slug,)) as cur:
                row = await cur.fetchone()
            assert row is not None
            await conn.execute("DELETE FROM job_domains WHERE job_id = ?", (row["id"],))
            await conn.executemany(
                "INSERT INTO job_domains (job_id, domain_id) VALUES (?, ?)",
                [(row["id"], d) for d in domain_ids],
            )

    # ------------------------------------------------------------------ runs

    async def start_run(self, slug: str, trigger: str, model: str | None) -> int:
        async with self.db.transaction() as conn:
            cur = await conn.execute(
                "INSERT INTO job_runs (job_slug, trigger, started, status, model) "
                "VALUES (?, ?, ?, 'running', ?)",
                (slug, trigger, time.time(), model),
            )
            return int(cur.lastrowid or 0)

    async def finish_run(self, run_id: int, **fields: Any) -> None:
        for key in ("findings", "topics", "mcp"):
            if key in fields and fields[key] is not None:
                fields[key] = json.dumps(fields[key], ensure_ascii=False)
        fields["finished"] = time.time()
        sets = ", ".join(f"{k} = ?" for k in fields)
        await self.db.execute(
            f"UPDATE job_runs SET {sets} WHERE id = ?", (*fields.values(), run_id)
        )

    async def get_run(self, run_id: int) -> dict[str, Any] | None:
        row = await self.db.fetchone("SELECT * FROM job_runs WHERE id = ?", (run_id,))
        return run_dict(row) if row else None

    async def runs(self, slug: str, limit: int = 20) -> list[dict[str, Any]]:
        rows = await self.db.fetchall(
            "SELECT * FROM job_runs WHERE job_slug = ? ORDER BY started DESC, id DESC LIMIT ?",
            (slug, limit),
        )
        return [run_dict(r) for r in rows]

    async def latest_runs(self, *, finished_only: bool = True) -> dict[str, dict[str, Any]]:
        """The newest (finished) run of every job, by slug. A run interrupted by a stop is
        skipped when the job has a real result: it says nothing about the host."""
        status = "AND status != 'running'" if finished_only else ""
        rows = await self.db.fetchall(
            "SELECT r.* FROM job_runs r JOIN ("
            "  SELECT job_slug, COALESCE(MAX(CASE WHEN error IS NOT ? THEN id END), MAX(id)) AS id"
            f"  FROM job_runs WHERE 1 {status} GROUP BY job_slug"
            ") last ON last.id = r.id",
            (INTERRUPTED,),
        )
        return {r["job_slug"]: run_dict(r) for r in rows}

    async def interrupted_slugs(self) -> list[str]:
        """Jobs whose newest run was interrupted by a stop (they are rerun after the start)."""
        rows = await self.db.fetchall(
            "SELECT job_slug FROM job_runs WHERE id IN "
            "(SELECT MAX(id) FROM job_runs GROUP BY job_slug) AND error = ? ORDER BY job_slug",
            (INTERRUPTED,),
        )
        return [r["job_slug"] for r in rows]

    async def running_runs(self) -> dict[str, int]:
        """slug -> id of its run in progress (at most one per job: the runner holds a lock)."""
        rows = await self.db.fetchall(
            "SELECT job_slug, MAX(id) AS id FROM job_runs WHERE status = 'running' "
            "GROUP BY job_slug"
        )
        return {r["job_slug"]: int(r["id"]) for r in rows}

    async def mark_interrupted(self) -> int:
        """Runs still 'running' from before a restart: stored as error, never left dangling.
        Returns how many (a row count: `Database.execute` returns the last inserted id)."""
        return await self.db.write(
            "UPDATE job_runs SET status = 'error', error = ?, finished = ? "
            "WHERE status = 'running'",
            (INTERRUPTED, time.time()),
        )
