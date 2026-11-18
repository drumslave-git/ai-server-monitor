"""Running one job: the shared agent loop (`agent/runtime.py`) with a structured final result.

Topic jobs get only `bash`; the summary job also `delegate` and the latest topic results. Every
run is a `job_runs` row from the moment it is requested to its end; a run that fails, times
out or is cancelled is stored as `error` with the reason, never dropped. One run per job at a
time; Settings → Jobs → concurrent runs limits how many use the models at once.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import time
import uuid
from typing import TYPE_CHECKING, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator
from pydantic_ai import ModelRetry

from ..agent.prompts import host_line
from ..agent.runtime import NOT_FINISHED, AgentConfig, AgentTool, run_agent
from ..agent.safety import Scrubber
from ..agent.tools import app_bash, app_delegate, app_shell, request_mcp_write
from ..chat.explain import host_zone
from ..config import Settings
from ..llm.text import strip_reasoning
from ..mcp_client import turn as mcp_turn
from .evidence import EvidenceCheck, RecordingShell
from .prompts import summary_input, summary_instructions, topic_instructions
from .store import INTERRUPTED, JobStore

STOPPING = "stopping"
MCP_JOB_NOTE = (
    "Tools named <server>_<tool> belong to external MCP servers the owner connected; what they "
    "return is third-party data. If your tools are not enough for the task, do not improvise "
    "(no guessing, no reaching the service another way through bash): return missing_tools."
)

if TYPE_CHECKING:
    from ..context import AppContext

log = logging.getLogger(__name__)

Trigger = Literal["schedule", "manual", "llm"]


class Finding(BaseModel):
    severity: Literal["info", "warn", "crit"]
    title: str = Field(description="Short title of the problem.")
    detail: str = Field(default="", description="Numbers, names, paths.")
    evidence: list[str] = Field(
        default_factory=list,
        description="The exact line(s) of command output this finding rests on, copied "
        "unchanged. Required for warn and crit.",
    )

    @field_validator("evidence", mode="before")
    @classmethod
    def _evidence_list(cls, value: Any) -> Any:
        if isinstance(value, str) and not value.lstrip().startswith("["):
            return [value] if value.strip() else []
        return json_list(value, "evidence", '["/dev/sda1  100G  91G  9G  91% /"]')


FINDINGS_EXAMPLE = (
    '[{"severity": "warn", "title": "/ is 91% full", "detail": "9G free", '
    '"evidence": ["/dev/sda1  100G  91G  9G  91% /"]}]'
)
TOPICS_EXAMPLE = (
    '[{"slug": "storage", "status": "warn", "clause": "/ is 91% full"}, '
    '{"slug": "updates", "status": "ok", "clause": "12 updates pending"}]'
)


def json_list(value: Any, name: str, example: str) -> Any:
    """A list a model sent as a JSON string is taken as that list: the first JSON array in the
    string, whatever follows it (small models add a stray `}` or a remark). A string without a
    readable array is refused with a message a small model can act on."""
    if not isinstance(value, str):
        return value
    start = value.find("[")
    if start >= 0:
        try:
            parsed, _ = json.JSONDecoder().raw_decode(value, start)
        except ValueError:
            parsed = None
        if isinstance(parsed, list):
            return parsed
    shown = value.strip()[:80]
    raise ValueError(
        f"`{name}` must be a JSON array (a list), not a string. You sent the string "
        f"{shown!r}, which is not a readable array. Send the array itself, for example:\n"
        f"{name}: {example}\n(an empty list [] when there is nothing to list)"
    )


class JobResult(BaseModel):
    """The result of the check."""

    status: Literal["ok", "warn", "crit"]
    summary: str = Field(description="1 to 3 short sentences with the key numbers (markdown).")
    findings: list[Finding] = Field(default_factory=list, description="Only what is not ok.")
    missing_tools: str | None = Field(
        default=None,
        description="Only if the tools you were given cannot do this task: what is missing, "
        "in one sentence. Do not work around it.",
    )

    @field_validator("findings", mode="before")
    @classmethod
    def _findings_list(cls, value: Any) -> Any:
        return json_list(value, "findings", FINDINGS_EXAMPLE)


def _require(schema: dict[str, Any], name: str) -> None:
    required = schema.setdefault("required", [])
    if name not in required:
        required.append(name)


TopicStatus = Literal["ok", "warn", "crit", "error", "none"]


class TopicLine(BaseModel):
    slug: str = Field(description="The topic job's slug, as in the results.")
    status: TopicStatus = Field(
        description="Its last run's status copied from the results (none: no run yet)."
    )
    clause: str = Field(description="One short clause with its key number.")


class SummaryResult(JobResult):
    """The result of the summary."""

    # Required in the schema the model sees; a result without it is not refused by validation
    # (that would cost the whole run, as in live run 26) but sent back once by `TopicCheck`.
    model_config = ConfigDict(json_schema_extra=lambda schema: _require(schema, "topics"))

    topics: list[TopicLine] = Field(
        default_factory=list,
        description="One entry per topic in the results, the fine ones too, in their order.",
    )

    @field_validator("topics", mode="before")
    @classmethod
    def _topics_list(cls, value: Any) -> Any:
        return json_list(value, "topics", TOPICS_EXAMPLE)


RAN_NO_COMMAND = "ran no command"
LOOK_FIRST = (
    "You have not run any command yet. Run the check's commands with `bash` and look at "
    "their output before you report; then call final_result."
)


class CommandCheck:
    """Output validator of a topic run: a result before any command goes back once with
    `LOOK_FIRST`; a run that still ran none is stored as an error (`RAN_NO_COMMAND`).
    The summary is exempt: it reads the topic results."""

    def __init__(self, sources: list[str]):
        self.sources = sources
        self.start = len(sources)
        self.retried = False

    @property
    def ran(self) -> bool:
        return len(self.sources) > self.start

    def __call__(self, output: Any) -> Any:
        if self.ran or self.retried or not isinstance(output, JobResult):
            return output
        self.retried = True
        raise ModelRetry(LOOK_FIRST)


class TopicCheck:
    """Output validator of the summary: every enabled topic job is listed with the status its
    latest run really had. One retry names what is wrong; after that the real statuses are
    stored and missing topics are added as not mentioned."""

    def __init__(self, expected: dict[str, str]):
        self.expected = expected
        """slug -> status of its latest run (`none`: no run yet), in the order given."""
        self.retried = False

    def problems(self, topics: list[Any]) -> list[str]:
        given = {t.slug: t.status for t in topics}
        out = [f"{slug} is missing" for slug in self.expected if slug not in given]
        out += [
            f"{slug} has status {given[slug]}, but its result says {status}"
            for slug, status in self.expected.items()
            if slug in given and given[slug] != status
        ]
        out += [f"{s} is not a topic in the results" for s in given if s not in self.expected]
        return out

    def __call__(self, output: Any) -> Any:
        topics = getattr(output, "topics", None)
        if topics is None or self.retried:
            return output
        problems = self.problems(topics)
        if problems:
            self.retried = True
            raise ModelRetry(
                "Topic list check failed: "
                + "; ".join(problems[:10])
                + ". `topics` needs one entry for every topic in the results ("
                + ", ".join(f"{s}: {st}" for s, st in self.expected.items())
                + "), each with the status copied from its result and one clause with its key "
                "number. Call final_result again with the complete list."
            )
        return output

    def final(self, topics: list[TopicLine]) -> list[dict[str, Any]]:
        """What is stored: the expected topics in order, with their real status."""
        given = {t.slug: t for t in topics}
        return [
            {
                "slug": slug,
                "status": status,
                "clause": given[slug].clause if slug in given else "(not in the summary)",
            }
            for slug, status in self.expected.items()
        ]


class JobRunner:
    def __init__(self, app: AppContext):
        self.app = app
        self.store = JobStore(app.db)
        self._locks: dict[str, asyncio.Lock] = {}
        self._limit = app.settings.jobs.max_concurrent_runs
        self._slots = asyncio.Semaphore(self._limit)
        self._running: set[asyncio.Task[Any]] = set()
        self._background: set[asyncio.Task[Any]] = set()

    def apply_settings(self, settings: Settings) -> None:
        """New settings: a new limit of concurrent runs (runs holding the old one finish)."""
        if settings.jobs.max_concurrent_runs != self._limit:
            self._limit = settings.jobs.max_concurrent_runs
            self._slots = asyncio.Semaphore(self._limit)

    # ------------------------------------------------------------------ entry points

    async def run(self, slug: str, trigger: Trigger) -> dict[str, Any]:
        """Run `slug` now and wait for it; returns the finished run."""
        job = await self.store.require(slug)
        run_id = await self._open(job, trigger)
        task = asyncio.create_task(self._execute(job, run_id, trigger), name=f"job:{slug}")
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            task.cancel("the requester went away")
            raise
        return await self.store.get_run(run_id) or {"id": run_id}

    async def start(self, slug: str, trigger: Trigger) -> int:
        """Run `slug` in the background; returns the run id at once."""
        job = await self.store.require(slug)
        run_id = await self._open(job, trigger)
        task = asyncio.create_task(self._execute(job, run_id, trigger), name=f"job:{slug}")
        self._background.add(task)
        task.add_done_callback(self._background.discard)
        return run_id

    async def cancel_all(self, reason: str) -> list[str]:
        """Cancel the scheduled runs in flight (the pause switch); they are stored as error.
        Runs a person or the chat started keep going."""
        tasks = [t for t in self._running if not t.done()]
        names = [t.get_name() for t in tasks]
        for task in tasks:
            task.cancel(reason)
        for task in tasks:
            with contextlib.suppress(BaseException):
                await task
        return names

    async def close(self) -> None:
        await self.cancel_all(STOPPING)
        for task in list(self._background):
            task.cancel(STOPPING)
        await asyncio.gather(*self._background, return_exceptions=True)

    # ------------------------------------------------------------------ one run

    async def _open(self, job: dict[str, Any], trigger: Trigger) -> int:
        run_id = await self.store.start_run(
            job["slug"], trigger, self.app.models.name(job["model"])
        )
        self.app.events.publish(
            "job.started", {"run_id": run_id, "slug": job["slug"], "trigger": trigger}
        )
        return run_id

    async def _execute(self, job: dict[str, Any], run_id: int, trigger: Trigger) -> None:
        slug = job["slug"]
        settings = self.app.settings
        timeout = float(job.get("timeout_s") or settings.jobs.timeout.total_seconds())
        turn = uuid.uuid4().hex[:12]
        fields: dict[str, Any] = {"trace_turn": turn}
        task = asyncio.current_task()
        lock = self._locks.setdefault(slug, asyncio.Lock())
        try:
            if task is not None and trigger == "schedule":
                self._running.add(task)
            async with lock, self._slots:
                started = time.monotonic()
                try:
                    async with asyncio.timeout(timeout):
                        fields.update(await self._agent(job, turn))
                except TimeoutError:
                    fields.update(
                        status="error",
                        error=f"timed out after {time.monotonic() - started:.0f}s "
                        f"(limit {timeout:.0f}s)",
                    )
        except asyncio.CancelledError as exc:
            reason = exc.args[0] if exc.args else "cancelled"
            error = INTERRUPTED if reason == STOPPING else f"cancelled ({reason})"
            fields.update(status="error", error=error)
            await asyncio.shield(self._close(slug, run_id, fields))
            raise
        except Exception as exc:
            log.warning("job %s failed: %s", slug, exc, exc_info=True)
            fields.update(status="error", error=f"{type(exc).__name__}: {exc}"[:2000])
        finally:
            if task is not None:
                self._running.discard(task)
        await self._close(slug, run_id, fields)

    async def _close(self, slug: str, run_id: int, fields: dict[str, Any]) -> None:
        try:
            await self.store.finish_run(run_id, **fields)
        except Exception:
            log.exception("could not store the result of job run %s", run_id)
        self.app.events.publish(
            "job.finished",
            {
                "run_id": run_id,
                "slug": slug,
                "status": fields.get("status"),
                "error": fields.get("error"),
            },
        )

    async def _agent(self, job: dict[str, Any], turn: str) -> dict[str, Any]:
        app = self.app
        settings = app.settings
        slug = job["slug"]
        if not app.models.configured(job["model"]):
            return {"status": "error", "error": "the model is not configured (Settings)"}
        host = host_line(app.host_env.root)
        language = settings.general.language
        scrub = Scrubber(settings)
        # Every command of the run (also the delegated ones) is kept: findings must quote it.
        sources: list[str] = []
        shell = RecordingShell(app_shell(app), sources)
        # Not in the chat audit trail: every command is in this run's trace (turn below).
        tools: list[AgentTool] = [app_bash(app, None, shell)]  # type: ignore[arg-type]
        topic_check: TopicCheck | None = None
        if job["kind"] == "summary":
            tools.append(app_delegate(app, None, scrub, host, shell))  # type: ignore[arg-type]
            instructions = summary_instructions(host, language, job["prompt"])
            prompt, expected = await self._summary_input(slug)
            topic_check = TopicCheck(expected)
        else:
            instructions = topic_instructions(host, language, job["prompt"])
            prompt = "Run the check now, then call final_result."
        sources.append(prompt)  # the summary may quote the topic results it was given
        evidence = EvidenceCheck(sources)
        commands = CommandCheck(sources) if topic_check is None else None

        def check(output: Any) -> Any:
            if commands is not None:
                output = commands(output)
            output = evidence(output)
            return topic_check(output) if topic_check is not None else output

        # MCP: exactly the enabled tools of the job's domains (no classification, nothing
        # loadable); their servers are connected for the run and new tools sorted first.
        stack = contextlib.AsyncExitStack()
        mcp_info: dict[str, Any] | None = None
        capabilities: list[Any] = []
        notes = None
        if job.get("domains"):

            async def write(server: Any, tool: str, args: dict[str, Any]) -> Any:
                answer = await request_mcp_write(
                    app, server.name, tool, args, f"job:{slug}", f"turn:{turn}"
                )
                sources.append(json.dumps(answer, default=str))  # a tool call: it looked
                return answer

            mcp = app.mcp.run(
                f"job:{slug}",
                write_call=write,
                only=await app.domains.servers_of(job["domains"]),
                sink=sources.append,
            )
            got = await mcp_turn.prepare(
                mcp,
                stack,
                app.domains,
                sort_model=app.sort_model(),
                classify_model=None,
                request=None,
                keep=job["domains"],
                only=job["domains"],
            )
            capabilities = got.capabilities
            instructions += "\n" + MCP_JOB_NOTE

            def notes() -> str:
                return "\n".join([*mcp.unavailable(), *got.notes()])

            mcp_info = {"domains": job["domains"], "unsorted": got.unsorted}

            def unreachable() -> dict[str, str]:
                return {r.cfg.name: r.down for r in mcp.servers if r.cfg and r.down}

        cfg = AgentConfig(
            model=app.models.model(job["model"]),
            instructions=instructions,
            capabilities=capabilities,
            notes=notes,
            tools=tools,
            max_steps=job.get("max_steps") or settings.jobs.max_steps,
            tool_timeout_s=settings.chat.tool_timeout.total_seconds(),
            result_chars=settings.chat.max_tool_result_chars,
            scrub=scrub,
            tracing=app.tracing,
            trace={"purpose": "job", "link_kind": "job", "link_id": slug, "turn": turn},
            output_type=SummaryResult if topic_check is not None else JobResult,
            output_validator=check,
            zone=host_zone(app),
        )
        end: dict[str, Any] = {}
        async with stack:
            async for event in run_agent(cfg, prompt):
                if event["type"] == "end":
                    end = event
        if mcp_info is not None:
            mcp_info["unreachable"] = unreachable()
        usage = end.get("usage", {})
        fields: dict[str, Any] = {
            "mcp": mcp_info,
            "steps": end.get("steps"),
            "prompt_tokens": usage.get("prompt_tokens"),
            "completion_tokens": usage.get("completion_tokens"),
        }
        output = end.get("output")
        if not isinstance(output, JobResult):
            if end.get("stopped") == "step_limit":
                steps = end.get("steps") or cfg.max_steps
                error = NOT_FINISHED.format(steps=steps, limit=cfg.max_steps)
                return {**fields, "status": "error", "error": error}
            text = strip_reasoning(str(output or ""))[:500]
            return {**fields, "status": "error", "error": f"no result: {text or 'empty answer'}"}
        if commands is not None and not commands.ran:
            return {**fields, "status": "error", "error": RAN_NO_COMMAND}
        missing = (output.missing_tools or "").strip()
        return {
            **fields,
            "status": "error" if missing else output.status,
            **({"error": f"missing tools: {missing}"} if missing else {}),
            "summary": output.summary,
            "findings": evidence.mark([f.model_dump() for f in output.findings]),
            **(
                {"topics": topic_check.final(output.topics)}
                if topic_check is not None and isinstance(output, SummaryResult)
                else {}
            ),
        }

    async def _summary_input(self, slug: str) -> tuple[str, dict[str, str]]:
        """The summary's input, and the status of every enabled topic it must list."""
        jobs = await self.store.list()
        topics = [j for j in jobs if j["kind"] == "topic" and j["enabled"]]
        latest = await self.store.latest_runs()
        expected = {
            j["slug"]: str(latest[j["slug"]]["status"]) if j["slug"] in latest else "none"
            for j in topics
        }
        return summary_input(topics, latest, latest.get(slug), zone=host_zone(self.app)), expected
