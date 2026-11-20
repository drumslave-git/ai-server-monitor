"""The `jobs`, `summary_members` and `job_runs` tables.

A summary runs its member jobs one after another and then writes the overall picture. A job in
at least one summary has no schedule of its own; a job in no summary is "detached": its own
schedule (or none: run by hand) and its own Telegram notify."""

from __future__ import annotations

import json
import re
import time
from typing import Any

from ..db import Database
from .builtins import BUILTINS, BY_SLUG, JobDef

INTERRUPTED = "interrupted (the service stopped)"
"""The error of a run the service stopped in the middle of (it is rerun after the start)."""
SLUG = re.compile(r"^[a-z0-9][a-z0-9_-]*(:[a-z0-9][a-z0-9_-]*)*$")
SLUG_MIN, SLUG_MAX = 2, 40
SLUG_RULE = (
    "slug: 2-40 characters, lowercase letters, digits, - and _, parts joined by ':' "
    "(e.g. docker:status)"
)
NOTIFY = ("always", "changed", "warn_crit", "never")
SUMMARY_NOTIFY_KEY = "jobs.summary_notify"
"""kv: the notify the builtin summaries get when they are seeded (left by the upgrade from
0.9.1, which mapped `outputs.telegram.send_summary` onto it); removed after seeding."""
EDITABLE = ("name", "prompt", "interval_s", "cron", "model", "timeout_s", "max_steps")
"""Fields whose change marks a builtin as edited (enabling/disabling and notify do not; a
summary's member list does)."""
_JOB_COLUMNS = (
    "slug, name, kind, prompt, interval_s, cron, model, enabled, builtin, edited, timeout_s, "
    "max_steps, created_by, created, updated, notify"
)
_DOMAINS = (
    "(SELECT json_group_array(name) FROM (SELECT d.name FROM job_domains jd "
    "JOIN mcp_domains d ON d.id = jd.domain_id WHERE jd.job_id = jobs.id ORDER BY d.name)) "
    "AS domains"
)
"""The names of a job's MCP domains (a JSON array)."""
_MEMBERS = (
    "(SELECT json_group_array(slug) FROM (SELECT j.slug FROM summary_members m "
    "JOIN jobs j ON j.id = m.job_id WHERE m.summary_id = jobs.id ORDER BY m.position)) "
    "AS members"
)
"""A summary's jobs in the order they run (a JSON array)."""
_SUMMARIES = (
    "(SELECT json_group_array(slug) FROM (SELECT s.slug FROM summary_members m "
    "JOIN jobs s ON s.id = m.summary_id WHERE m.job_id = jobs.id ORDER BY s.slug)) "
    "AS summaries"
)
"""The summaries a job is in (a JSON array)."""
_SELECT = f"SELECT {_JOB_COLUMNS}, retired, {_DOMAINS}, {_MEMBERS}, {_SUMMARIES} FROM jobs"


class JobError(Exception):
    """A request about jobs that cannot be done (unknown job, a builtin cannot be deleted...)."""


class UnknownDomain(JobError):
    """A domain name that does not exist (the message names the existing ones)."""


def valid_slug(slug: str) -> bool:
    return SLUG_MIN <= len(slug) <= SLUG_MAX and bool(SLUG.match(slug))


def job_dict(row: Any) -> dict[str, Any]:
    out = dict(row)
    for key in ("enabled", "builtin", "edited", "retired"):
        out[key] = bool(out[key])
    for key in ("domains", "members", "summaries"):
        out[key] = json.loads(out.get(key) or "[]")
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


def _schedule_words(job: dict[str, Any]) -> str:
    if job.get("interval_s"):
        return f"every {job['interval_s']} s"
    return f"cron {job['cron']}"


class JobStore:
    def __init__(self, db: Database):
        self.db = db

    # ------------------------------------------------------------------ builtins

    async def seed_builtins(self) -> None:
        """Insert missing builtins; bring unedited ones up to the definitions in code. A new
        builtin that came out of a 0.9.1 builtin gets that one's MCP domains."""
        now = time.time()
        summary_notify = await self.db.kv_get(SUMMARY_NOTIFY_KEY)
        for d in BUILTINS:
            row = await self.db.fetchone(
                "SELECT edited, builtin FROM jobs WHERE slug = ?", (d.slug,)
            )
            if row is None:
                await self.db.execute(
                    f"INSERT INTO jobs ({_JOB_COLUMNS}) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, 1, 1, 0, NULL, ?, 'builtin', ?, ?, ?)",
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
                        summary_notify if d.kind == "summary" and summary_notify else d.notify,
                    ),
                )
                if d.origin:
                    await self.db.execute(
                        "INSERT INTO job_domains (job_id, domain_id) "
                        "SELECT (SELECT id FROM jobs WHERE slug = ?), domain_id FROM job_domains "
                        "WHERE job_id = (SELECT id FROM jobs WHERE slug = ?)",
                        (d.slug, d.origin),
                    )
                if d.kind == "summary":
                    await self._set_members(d.slug, list(d.members))
            elif row["builtin"] and not row["edited"]:
                await self._apply_definition(d, now, only_if_changed=True)
                if d.kind == "summary":
                    current = await self._member_slugs(d.slug)
                    if current != list(d.members):
                        await self._set_members(d.slug, list(d.members))
        if summary_notify is not None:
            await self.db.execute("DELETE FROM kv WHERE key = ?", (SUMMARY_NOTIFY_KEY,))

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

    # ------------------------------------------------------------------ jobs

    async def list(self) -> list[dict[str, Any]]:
        """The jobs and summaries (not the retired ones): summaries first, then jobs."""
        rows = await self.db.fetchall(
            f"{_SELECT} WHERE retired = 0 ORDER BY kind = 'summary' DESC, builtin DESC, slug"
        )
        return [job_dict(r) for r in rows]

    async def retired(self) -> list[dict[str, Any]]:
        """Old builtins a release replaced: no longer run, their runs are kept."""
        rows = await self.db.fetchall(f"{_SELECT} WHERE retired = 1 ORDER BY slug")
        return [job_dict(r) for r in rows]

    async def get(self, slug: str, *, retired: bool = False) -> dict[str, Any] | None:
        """A job by slug; a retired one only when `retired` is set."""
        where = "slug = ?" if retired else "slug = ? AND retired = 0"
        row = await self.db.fetchone(f"{_SELECT} WHERE {where}", (slug,))
        return job_dict(row) if row else None

    async def require(self, slug: str) -> dict[str, Any]:
        job = await self.get(slug)
        if job is None:
            raise JobError(f"no job {slug!r}")
        return job

    async def members(self, slug: str) -> list[dict[str, Any]]:
        """A summary's jobs in the order they run."""
        rows = await self.db.fetchall(
            f"{_SELECT} WHERE id IN (SELECT job_id FROM summary_members WHERE summary_id = "
            "(SELECT id FROM jobs WHERE slug = ?))",
            (slug,),
        )
        jobs = {r["slug"]: job_dict(r) for r in rows}
        return [jobs[s] for s in await self._member_slugs(slug) if s in jobs]

    async def create(self, data: dict[str, Any], created_by: str) -> dict[str, Any]:
        """A new job (kind topic) or summary. Returns it; `notice` says in plain words what
        else changed (a schedule cleared because the job joined a summary)."""
        slug = data["slug"]
        if not valid_slug(slug):
            raise JobError(SLUG_RULE)
        existing = await self.get(slug, retired=True)
        if existing is not None:
            retired = " (an old builtin this release replaced; its runs are kept)"
            raise JobError(f"a job {slug!r} exists already{retired if existing['retired'] else ''}")
        kind = data.get("kind") or "topic"
        notify = data.get("notify") or ("changed" if kind == "summary" else "warn_crit")
        if kind == "summary" and data.get("summaries"):
            raise JobError("a summary cannot be a member of another summary")
        if kind == "topic" and data.get("members"):
            raise JobError("only a summary has members")
        domain_ids = await self.domain_ids(data.get("domains") or [])
        members = await self._check_members(slug, data.get("members") or [])
        summaries = await self._check_summaries(data.get("summaries") or [])
        now = time.time()
        await self.db.execute(
            f"INSERT INTO jobs ({_JOB_COLUMNS}) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, 0, 0, ?, ?, ?, ?, ?, ?)",
            (
                slug,
                data["name"],
                kind,
                data["prompt"],
                data.get("interval_s"),
                data.get("cron"),
                data.get("model") or ("main" if kind == "summary" else "worker"),
                int(data.get("enabled", True)),
                data.get("timeout_s"),
                data.get("max_steps"),
                created_by,
                now,
                now,
                notify,
            ),
        )
        if domain_ids:
            await self._set_domains(slug, domain_ids)
        notices: list[str] = []
        if kind == "summary":
            notices += await self._set_members(slug, members)
        for summary in summaries:
            notices += await self._join(summary, slug)
        return await self._with_notice(slug, notices)

    async def update(self, slug: str, changes: dict[str, Any]) -> dict[str, Any]:
        """Apply the given fields (a schedule change sets interval_s/cron together). `members`
        (a summary) and `summaries` (a job) change the membership; a job that joins a summary
        loses its own schedule, and the returned `notice` says so."""
        job = await self.require(slug)
        notices: list[str] = []
        sets_schedule = bool(changes.get("interval_s") or changes.get("cron"))
        if "members" in changes and changes["members"] is not None:
            if job["kind"] != "summary":
                raise JobError(f"{slug!r} is a job, not a summary: only a summary has members")
            members = await self._check_members(slug, changes["members"])
            if members != job["members"]:
                notices += await self._set_members(slug, members)
                if job["builtin"]:
                    await self._mark_edited(slug)
        if "summaries" in changes and changes["summaries"] is not None:
            if job["kind"] == "summary":
                raise JobError("a summary cannot be a member of another summary")
            wanted = await self._check_summaries(changes["summaries"])
            if wanted and sets_schedule:
                raise JobError(self._no_schedule(slug, wanted))
            for summary in job["summaries"]:
                if summary not in wanted:
                    await self._leave(summary, slug)
            for summary in wanted:
                if summary not in job["summaries"]:
                    notices += await self._join(summary, slug)
        job = await self.require(slug)
        if sets_schedule and job["summaries"]:
            raise JobError(self._no_schedule(slug, job["summaries"]))
        domains = changes.get("domains")
        if domains is not None and sorted(set(domains)) != job["domains"]:
            await self._set_domains(slug, await self.domain_ids(domains))
            if job["builtin"]:
                await self._mark_edited(slug)
            job = await self.require(slug)
        if changes.get("notify") is not None and changes["notify"] not in NOTIFY:
            raise JobError(f"notify: one of {', '.join(NOTIFY)}")
        fields = {k: v for k, v in changes.items() if k in (*EDITABLE, "enabled", "notify")}
        if fields.get("notify") is None:
            fields.pop("notify", None)
        if "interval_s" in fields or "cron" in fields:
            fields.setdefault("interval_s", None)
            fields.setdefault("cron", None)
        if fields:
            edited = job["builtin"] and any(k in EDITABLE and fields[k] != job[k] for k in fields)
            sets = [f"{k} = ?" for k in fields]
            params: list[Any] = [int(v) if k == "enabled" else v for k, v in fields.items()]
            if edited:
                sets.append("edited = 1")
            sets.append("updated = ?")
            await self.db.execute(
                f"UPDATE jobs SET {', '.join(sets)} WHERE slug = ?", (*params, time.time(), slug)
            )
        return await self._with_notice(slug, notices)

    async def delete(self, slug: str) -> None:
        """Delete an own job or summary and its runs. A summary's member jobs stay (detached
        when they are in no other summary, without a schedule: run by hand)."""
        job = await self.require(slug)
        if job["builtin"]:
            raise JobError(f"{slug!r} is a builtin {self._noun(job)}: disable it instead")
        async with self.db.transaction() as conn:
            await conn.execute("DELETE FROM job_runs WHERE job_slug = ?", (slug,))
            await conn.execute("DELETE FROM jobs WHERE slug = ?", (slug,))

    async def reset(self, slug: str) -> dict[str, Any]:
        """A builtin back to its definition (a summary: its members too). Notify and enabled
        stay as they are."""
        job = await self.require(slug)
        definition = BY_SLUG.get(slug)
        if not job["builtin"] or definition is None:
            raise JobError(f"{slug!r} is not a builtin")
        notices: list[str] = []
        await self._apply_definition(definition, time.time(), only_if_changed=False)
        await self._set_domains(slug, [])  # builtins ship without domains
        if definition.kind == "summary":
            notices += await self._set_members(slug, list(definition.members))
        return await self._with_notice(slug, notices)

    # ------------------------------------------------------------------ membership

    async def _member_slugs(self, summary: str) -> list[str]:
        rows = await self.db.fetchall(
            "SELECT j.slug FROM summary_members m JOIN jobs j ON j.id = m.job_id "
            "WHERE m.summary_id = (SELECT id FROM jobs WHERE slug = ?) ORDER BY m.position",
            (summary,),
        )
        return [r["slug"] for r in rows]

    async def _check_members(self, summary: str, slugs: list[str]) -> list[str]:
        """Members must be existing jobs (not summaries), each once."""
        if len(set(slugs)) != len(slugs):
            twice = sorted({s for s in slugs if slugs.count(s) > 1})
            raise JobError(f"a job can be in a summary only once: {', '.join(twice)}")
        for member in slugs:
            if member == summary:
                raise JobError("a summary cannot be its own member")
            job = await self.get(member)
            if job is None:
                raise JobError(f"no job {member!r}")
            if job["kind"] == "summary":
                raise JobError(f"{member!r} is a summary: a summary cannot be a member")
        return list(slugs)

    async def _check_summaries(self, slugs: list[str]) -> list[str]:
        out: list[str] = []
        for summary in slugs:
            job = await self.get(summary)
            if job is None:
                raise JobError(f"no summary {summary!r}")
            if job["kind"] != "summary":
                raise JobError(f"{summary!r} is a job, not a summary")
            if summary not in out:
                out.append(summary)
        return out

    async def _set_members(self, summary: str, members: list[str]) -> list[str]:
        """Replace a summary's member list; members with a schedule lose it (returned as
        notices)."""
        notices = [n for m in members if (n := await self._clear_schedule(m, summary))]
        async with self.db.transaction() as conn:
            async with conn.execute("SELECT id FROM jobs WHERE slug = ?", (summary,)) as cur:
                row = await cur.fetchone()
            assert row is not None
            await conn.execute("DELETE FROM summary_members WHERE summary_id = ?", (row["id"],))
            await conn.executemany(
                "INSERT INTO summary_members (summary_id, job_id, position) "
                "VALUES (?, (SELECT id FROM jobs WHERE slug = ?), ?)",
                [(row["id"], m, i) for i, m in enumerate(members)],
            )
        return notices

    async def _join(self, summary: str, slug: str) -> list[str]:
        """Add a job at the end of a summary."""
        current = await self._member_slugs(summary)
        if slug in current:
            return []
        notices = await self._set_members(summary, [*current, slug])
        await self._mark_edited(summary)
        return notices

    async def _leave(self, summary: str, slug: str) -> None:
        current = await self._member_slugs(summary)
        await self._set_members(summary, [s for s in current if s != slug])
        await self._mark_edited(summary)

    async def _clear_schedule(self, slug: str, summary: str) -> str | None:
        job = await self.get(slug)
        if job is None or not (job["interval_s"] or job["cron"]):
            return None
        await self.db.execute(
            "UPDATE jobs SET interval_s = NULL, cron = NULL, updated = ? WHERE slug = ?",
            (time.time(), slug),
        )
        name = (await self.require(summary))["name"]
        return (
            f"{slug} was scheduled ({_schedule_words(job)}). It is now in the summary "
            f"{name}, which runs it, so its own schedule was removed."
        )

    async def _mark_edited(self, slug: str) -> None:
        await self.db.execute(
            "UPDATE jobs SET edited = 1, updated = ? WHERE slug = ? AND builtin = 1",
            (time.time(), slug),
        )

    @staticmethod
    def _no_schedule(slug: str, summaries: list[str]) -> str:
        return (
            f"{slug} is in the summary {', '.join(summaries)}, which runs it: a job in a "
            "summary has no schedule of its own. Take it out of the summary first."
        )

    @staticmethod
    def _noun(job: dict[str, Any]) -> str:
        return "summary" if job["kind"] == "summary" else "job"

    async def _with_notice(self, slug: str, notices: list[str]) -> dict[str, Any]:
        job = await self.require(slug)
        job["notice"] = " ".join(notices) or None
        return job

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

    async def start_run(
        self, slug: str, trigger: str, model: str | None, queue_run_id: int | None = None
    ) -> int:
        async with self.db.transaction() as conn:
            cur = await conn.execute(
                "INSERT INTO job_runs (job_slug, trigger, started, status, model, queue_run_id) "
                "VALUES (?, ?, ?, 'running', ?, ?)",
                (slug, trigger, time.time(), model, queue_run_id),
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

    async def queue_runs(self, run_id: int) -> list[dict[str, Any]]:
        """The job runs of one summary run, in the order they ran."""
        rows = await self.db.fetchall(
            "SELECT * FROM job_runs WHERE queue_run_id = ? ORDER BY id", (run_id,)
        )
        return [run_dict(r) for r in rows]

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
        """slug -> id of its newest run in progress (a job in two summaries running at once
        has a second run waiting for the first)."""
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
