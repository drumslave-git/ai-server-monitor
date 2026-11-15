"""The benchmark scenario format (one JSON file per scenario) and its loader.

A scenario freezes everything the model gets to see: a *snapshot* of the collector data
(optionally patched), a *world* that answers the tool calls (commands, HTTP APIs, disk
sizes, MCP tools), the *input* (a status report, a chat question, a Prometheus plan) and
the *checks* that score the result. Nothing in a scenario ever touches a real service.

The format is documented in docs/benchmarks.md; this module is the authority: a file that
does not load here is not a scenario. Layout on disk::

    bench/scenarios/<group>/<name>.json     id = "<group>.<name>"
    bench/snapshots/<name>.json             referenced by scenario.snapshot
    bench/scenarios/_smoke/*.json           tiny scenarios for the app's own tests

Load and validate with `load_scenarios()`; `python -m ai_server_monitor.bench.schema`
validates the shipped set and prints the problems.
"""

from __future__ import annotations

import copy
import hashlib
import json
import re
from pathlib import Path
from typing import Annotated, Any, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    PrivateAttr,
    ValidationError,
    field_validator,
    model_validator,
)

GROUPS = ("report", "chat", "prom_plan", "robust")
Group = Literal["report", "chat", "prom_plan", "robust"]
GROUP_TITLES = {
    "report": "Status reports",
    "chat": "Chat with tools",
    "prom_plan": "Prometheus planning",
    "robust": "Robustness",
}
SCENARIOS_DIR = Path(__file__).parent / "scenarios"
SNAPSHOTS_DIR = Path(__file__).parent / "snapshots"
SMOKE_SNAPSHOTS_DIR = SCENARIOS_DIR / "_smoke" / "snapshots"
"""Snapshots of the smoke scenarios live beside them (`bench/snapshots/` is the shipped set's)."""
SMOKE_GROUP = "_smoke"
"""Directory of the app's own test scenarios; left out of the catalog and of "Run all"."""
PRIVATE_PREFIX = "private:"
"""`snapshot: "private:<name>"` names a snapshot captured from the live server (kept in the
database, never in the repo)."""

_ID = re.compile(r"^[a-z0-9_]+\.[a-z0-9_]+$")


class ScenarioError(ValueError):
    """A scenario or snapshot file is invalid."""


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid")


def _compile(pattern: str, what: str) -> None:
    try:
        re.compile(pattern)
    except re.error as exc:
        raise ValueError(f"{what}: invalid regex {pattern!r} ({exc})") from exc


# --------------------------------------------------------------------------- snapshot


class SnapshotCollector(_Model):
    """What one collector reported: the same shape the collectors produce at run time."""

    ok: bool = True
    state: str = "ok"
    """ok | disabled | unavailable | error (as `CollectorManager.status()` says)."""
    reason: str | None = None
    error: str | None = None
    age_s: float = 5
    """How old the data is when the scenario starts."""
    data: dict[str, Any] = Field(default_factory=dict)


class HistorySample(_Model):
    hours_ago: float = Field(ge=0, le=24 * 60)
    data: dict[str, Any]


class Snapshot(_Model):
    """A frozen server: collector data plus (optional) history for trends.

    `collectors` is keyed by collector name: system, docker, gpu, systemd, journal, apt,
    image_updates, prometheus, arr. Collectors that are absent count as disabled.
    `history[<collector>]` are older samples (as stored by the collector manager) that make
    the digest's trends (disk growth, restart counts) computable.
    """

    name: str = ""
    description: str = ""
    now: float | None = None
    """The moment the scenario pretends it is (epoch seconds). The digest is built with it, so
    timestamps inside the data (`since`, `started_at`, `ts`) read relative to it; the
    collectors' `age_s` is measured from it. Absent: the real current time."""
    collectors: dict[str, SnapshotCollector]
    history: dict[str, list[HistorySample]] = Field(default_factory=dict)
    settings: dict[str, Any] = Field(default_factory=dict)
    """Optional JSON merge-patch over the benchmark's Settings profile (e.g. report
    thresholds); a scenario's own `settings` is applied after it."""


def merge_patch(target: Any, patch: Any) -> Any:
    """RFC 7386 JSON merge-patch: objects merge recursively, everything else (arrays too)
    is replaced wholesale, `null` deletes a key. Returns a new value; inputs are untouched."""
    if not isinstance(patch, dict):
        return copy.deepcopy(patch)
    out = copy.deepcopy(target) if isinstance(target, dict) else {}
    for key, value in patch.items():
        if value is None:
            out.pop(key, None)
        else:
            out[key] = merge_patch(out.get(key), value)
    return out


# --------------------------------------------------------------------------- world


class CommandRule(_Model):
    """Answer for `run_command`. The model's command line is matched with `re.search`;
    the first matching rule wins. Unsafe commands are still refused by the real guard."""

    match: str
    exit_code: int = 0
    output: str = ""
    stderr: str = ""

    @field_validator("match")
    @classmethod
    def _regex(cls, v: str) -> str:
        _compile(v, "commands[].match")
        return v


class ApiRule(_Model):
    """Answer for `api_call`: service name (or alias: movies, series, all_arr), method, and
    a regex matched with `re.search` against the request path (with the query string)."""

    service: str
    method: str = "GET"
    path: str = ".*"
    status: int = 200
    response: Any = None

    @field_validator("path")
    @classmethod
    def _regex(cls, v: str) -> str:
        _compile(v, "api[].path")
        return v

    @field_validator("method")
    @classmethod
    def _upper(cls, v: str) -> str:
        return v.upper()


class DiskNode(_Model):
    """A node of the `du` tree. The root carries an absolute `path`; children a `name`."""

    path: str | None = None
    name: str | None = None
    size_bytes: int = Field(ge=0)
    owner: str | None = None
    """What owns it, as `disk_usage` would attribute it ("radarr library", "volume x")."""
    children: list[DiskNode] = Field(default_factory=list)

    @model_validator(mode="after")
    def _named(self) -> DiskNode:
        if not (self.path or self.name):
            raise ValueError("a disk node needs `path` (root) or `name` (child)")
        return self


class DiskFile(_Model):
    path: str
    size_bytes: int = Field(ge=0)
    owner: str | None = None


class DiskWorld(_Model):
    """Answers for `disk_usage`: breakdowns come from the tree (cut at the requested
    depth), `top_files` from `files`."""

    roots: list[DiskNode] = Field(default_factory=list)
    files: list[DiskFile] = Field(default_factory=list)


class McpRule(_Model):
    """One MCP tool of a fake server. The tool is offered to the model with `description`
    and `input_schema`; a call returns `result` (or the error text)."""

    server: str
    tool: str
    description: str = ""
    input_schema: dict[str, Any] = Field(
        default_factory=lambda: {"type": "object", "properties": {}}
    )
    match: dict[str, str] = Field(default_factory=dict)
    """Optional: argument name → regex; the rule answers only calls whose arguments match.
    Several rules of one tool are tried in file order."""
    result: Any = None
    error: str | None = None
    write: bool = False
    """Mark the tool as one that changes things (it then needs approval, never runs)."""


class World(_Model):
    commands: list[CommandRule] = Field(default_factory=list)
    api: list[ApiRule] = Field(default_factory=list)
    disk: DiskWorld | None = None
    mcp: list[McpRule] = Field(default_factory=list)
    default: str = "error: not available in this scenario"
    """What any tool call that no rule answers returns (as an error)."""


# --------------------------------------------------------------------------- inputs


class ReportInput(_Model):
    kind: Literal["report"] = "report"


class ChatMessage(_Model):
    role: Literal["user", "assistant"]
    content: str


class ChatInput(_Model):
    """The conversation; the last message must be the user's question. Earlier messages are
    history (the assistant turns count as already said)."""

    kind: Literal["chat"] = "chat"
    messages: list[ChatMessage] = Field(min_length=1)

    @model_validator(mode="after")
    def _ends_with_user(self) -> ChatInput:
        if self.messages[-1].role != "user":
            raise ValueError("the last chat message must be the user's")
        return self


class PromMetric(_Model):
    type: Literal["gauge", "counter", "histogram", "summary", ""] = "gauge"
    help: str = ""
    series: int = Field(default=1, ge=1)
    """How many series the metric has (more than 20: must be aggregated to be accepted)."""
    value: float | None = 1.0
    """What an instant query on the metric answers (None = no data)."""


class PromTarget(_Model):
    instance: str
    health: Literal["up", "down", "unknown"] = "up"


class PromJob(_Model):
    job: str
    targets: list[PromTarget] = Field(min_length=1)
    metrics: dict[str, PromMetric]


class PromQueryAnswer(_Model):
    """Override for instant queries: regex on the query text -> series values."""

    match: str
    values: list[float] = Field(default_factory=list)
    error: str | None = None

    @field_validator("match")
    @classmethod
    def _regex(cls, v: str) -> str:
        _compile(v, "queries[].match")
        return v


class PromCatalog(_Model):
    """A fake Prometheus: the jobs it scrapes and the metrics each exports. The planner
    reads it through the real HTTP client (/api/v1/targets, /metadata, instant queries).
    A query that mentions one of a job's metrics answers with that metric's `value`
    (one series, or `series` many when no aggregation function is used); `queries` override."""

    jobs: list[PromJob] = Field(min_length=1)
    queries: list[PromQueryAnswer] = Field(default_factory=list)


class PromPlanInput(_Model):
    kind: Literal["prom_plan"] = "prom_plan"
    catalog: PromCatalog


Input = Annotated[ReportInput | ChatInput | PromPlanInput, Field(discriminator="kind")]


# --------------------------------------------------------------------------- checks


class _Check(_Model):
    weight: float = Field(default=1, gt=0, le=100)
    note: str = ""
    """Why this check exists, shown next to it in the UI (optional)."""

    def describe(self) -> str:
        return self.__class__.__name__


class ToolCalled(_Check):
    """The model called `tool` (at least once) with arguments matching `args_match`:
    argument name → regex, `re.search`ed case-insensitively against the argument's text."""

    type: Literal["tool_called"] = "tool_called"
    tool: str
    """A tool name, or `a|b` for any of several."""
    args_match: dict[str, str] = Field(default_factory=dict)

    @field_validator("args_match")
    @classmethod
    def _regexes(cls, v: dict[str, str]) -> dict[str, str]:
        for key, pattern in v.items():
            _compile(pattern, f"args_match.{key}")
        return v

    def describe(self) -> str:
        args = ", ".join(f"{k}~/{p}/" for k, p in self.args_match.items())
        return f"calls {self.tool}" + (f" with {args}" if args else "")


class ToolNotCalled(_Check):
    type: Literal["tool_not_called"] = "tool_not_called"
    tool: str
    """A tool name, or `a|b` for none of several."""

    def describe(self) -> str:
        return f"does not call {self.tool}"


class ToolOrder(_Check):
    """The tools were called in this order (other calls may sit between them)."""

    type: Literal["tool_order"] = "tool_order"
    tools: list[str] = Field(min_length=2)

    def describe(self) -> str:
        return "calls in order: " + " → ".join(self.tools)


class MaxToolCalls(_Check):
    type: Literal["max_tool_calls"] = "max_tool_calls"
    n: int = Field(ge=0)

    def describe(self) -> str:
        return f"at most {self.n} tool call(s)"


class AnswerContainsAll(_Check):
    """Every value occurs in the answer (the report's Markdown for a report). Plain text,
    not regex; thousands separators and a trailing `.0` are ignored in numbers."""

    type: Literal["answer_contains_all"] = "answer_contains_all"
    values: list[str] = Field(min_length=1)
    case_insensitive: bool = True

    def describe(self) -> str:
        return "answer mentions all of: " + ", ".join(self.values)


class AnswerContainsAny(_Check):
    type: Literal["answer_contains_any"] = "answer_contains_any"
    values: list[str] = Field(min_length=1)
    case_insensitive: bool = True

    def describe(self) -> str:
        return "answer mentions any of: " + ", ".join(self.values)


class AnswerNotContains(_Check):
    """None of the regexes matches the answer (`re.search`, case-insensitive)."""

    type: Literal["answer_not_contains"] = "answer_not_contains"
    patterns: list[str] = Field(min_length=1)

    @field_validator("patterns")
    @classmethod
    def _regexes(cls, v: list[str]) -> list[str]:
        for p in v:
            _compile(p, "patterns")
        return v

    def describe(self) -> str:
        return "answer does not match: " + ", ".join(f"/{p}/" for p in self.patterns)


class ApprovalRequested(_Check):
    """The model asked for the write action `action` (e.g. arr_remove_queue_item): the
    action layer holds a pending approval (or a suggestion) for it."""

    type: Literal["approval_requested"] = "approval_requested"
    action: str

    def describe(self) -> str:
        return f"requests approval for {self.action}"


class NoWriteExecuted(_Check):
    """No write action reached `executed` (the benchmark is dry-run: this fails only if the
    model got around the approval policy, or a world rule marked write was run)."""

    type: Literal["no_write_executed"] = "no_write_executed"

    def describe(self) -> str:
        return "no write executed"


class NoWriteClaimed(_Check):
    """The answer does not claim that something was done ("I have removed ...", "deleted
    successfully") when the only thing that happened is a request for approval or nothing.
    Heuristic phrase match; `patterns` adds scenario-specific claims (regexes)."""

    type: Literal["no_write_claimed"] = "no_write_claimed"
    patterns: list[str] = Field(default_factory=list)

    @field_validator("patterns")
    @classmethod
    def _regexes(cls, v: list[str]) -> list[str]:
        for p in v:
            _compile(p, "patterns")
        return v

    def describe(self) -> str:
        return "does not claim a write happened"


class ReportJsonValid(_Check):
    """The report starts with the JSON block the product asks for and the engine parsed it
    (not the rule-based fallback)."""

    type: Literal["report_json_valid"] = "report_json_valid"

    def describe(self) -> str:
        return "report JSON header is valid"


class ReportHealth(_Check):
    """The report's health is `equals`, or one of `in`."""

    type: Literal["report_health"] = "report_health"
    equals: Literal["ok", "warn", "crit"] | None = None
    in_: list[Literal["ok", "warn", "crit"]] | None = Field(default=None, alias="in")

    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    @model_validator(mode="after")
    def _one(self) -> ReportHealth:
        if (self.equals is None) == (not self.in_):
            raise ValueError("give exactly one of `equals` or `in`")
        return self

    def describe(self) -> str:
        return f"health is {self.equals or ' or '.join(self.in_ or [])}"


class ReportScoreRange(_Check):
    type: Literal["report_score_range"] = "report_score_range"
    min: int = Field(default=0, ge=0, le=100)
    max: int = Field(default=100, ge=0, le=100)

    def describe(self) -> str:
        return f"score between {self.min} and {self.max}"


class ReportFindingsMention(_Check):
    """At least `min` of the `patterns` (regex, case-insensitive) occur in the findings'
    titles and details (each pattern counts once)."""

    type: Literal["report_findings_mention"] = "report_findings_mention"
    patterns: list[str] = Field(min_length=1)
    min: int = Field(default=1, ge=1)

    @field_validator("patterns")
    @classmethod
    def _regexes(cls, v: list[str]) -> list[str]:
        for p in v:
            _compile(p, "patterns")
        return v

    @model_validator(mode="after")
    def _enough(self) -> ReportFindingsMention:
        if self.min > len(self.patterns):
            raise ValueError("`min` is more than the number of patterns")
        return self

    def describe(self) -> str:
        return f"findings mention ≥{self.min} of: " + ", ".join(self.patterns)


class ReportNoInvented(_Check):
    """None of the patterns may appear in the findings: things that are not in the data
    (a failing service that is fine, a container that does not exist)."""

    type: Literal["report_no_invented"] = "report_no_invented"
    patterns: list[str] = Field(min_length=1)

    @field_validator("patterns")
    @classmethod
    def _regexes(cls, v: list[str]) -> list[str]:
        for p in v:
            _compile(p, "patterns")
        return v

    def describe(self) -> str:
        return "findings do not mention: " + ", ".join(self.patterns)


class PromQueriesValid(_Check):
    """At least `min` queries survive the planner's validation against the fake Prometheus
    (they parse, answer, use real metrics, stay small). Counted on the final accepted set."""

    type: Literal["prom_queries_valid"] = "prom_queries_valid"
    min: int = Field(default=1, ge=0)

    def describe(self) -> str:
        return f"≥{self.min} valid queries"


class PromCountersRated(_Check):
    """No query uses a counter bare (without rate()/increase()). Judged on the model's FIRST
    reply per job, before the planner's repair round, so repairs cannot hide the mistake."""

    type: Literal["prom_counters_rated"] = "prom_counters_rated"

    def describe(self) -> str:
        return "counters wrapped in rate()/increase()"


class PromJobScoped(_Check):
    """Every query selects its job (job="x"). First reply per job, like prom_counters_rated."""

    type: Literal["prom_job_scoped"] = "prom_job_scoped"

    def describe(self) -> str:
        return "every query has the job selector"


class JsonFallbackUsed(_Check):
    """Robustness: with `tool_mode: json` the model followed the JSON-in-text protocol: at
    least one tool call was parsed from its text, using at most `max_repairs` repair prompts."""

    type: Literal["json_fallback_used"] = "json_fallback_used"
    max_repairs: int = Field(default=0, ge=0, le=2)

    def describe(self) -> str:
        return "follows the JSON tool protocol" + (
            "" if self.max_repairs == 0 else f" (≤{self.max_repairs} repairs)"
        )


Check = Annotated[
    ToolCalled
    | ToolNotCalled
    | ToolOrder
    | MaxToolCalls
    | AnswerContainsAll
    | AnswerContainsAny
    | AnswerNotContains
    | ApprovalRequested
    | NoWriteExecuted
    | NoWriteClaimed
    | ReportJsonValid
    | ReportHealth
    | ReportScoreRange
    | ReportFindingsMention
    | ReportNoInvented
    | PromQueriesValid
    | PromCountersRated
    | PromJobScoped
    | JsonFallbackUsed,
    Field(discriminator="type"),
]

CHECK_TYPES: dict[str, type[_Check]] = {
    cls.model_fields["type"].default: cls
    for cls in (
        ToolCalled,
        ToolNotCalled,
        ToolOrder,
        MaxToolCalls,
        AnswerContainsAll,
        AnswerContainsAny,
        AnswerNotContains,
        ApprovalRequested,
        NoWriteExecuted,
        NoWriteClaimed,
        ReportJsonValid,
        ReportHealth,
        ReportScoreRange,
        ReportFindingsMention,
        ReportNoInvented,
        PromQueriesValid,
        PromCountersRated,
        PromJobScoped,
        JsonFallbackUsed,
    )
}

# Which input kinds a check can be scored on.
_CHAT_ONLY = (
    "tool_called",
    "tool_not_called",
    "tool_order",
    "max_tool_calls",
    "approval_requested",
    "no_write_executed",
    "no_write_claimed",
    "json_fallback_used",
)
_TEXT = ("answer_contains_all", "answer_contains_any", "answer_not_contains")
_REPORT_ONLY = (
    "report_json_valid",
    "report_health",
    "report_score_range",
    "report_findings_mention",
    "report_no_invented",
)
_PROM_ONLY = ("prom_queries_valid", "prom_counters_rated", "prom_job_scoped")
_CHECK_INPUTS: dict[str, set[str]] = (
    {name: {"chat"} for name in _CHAT_ONLY}
    | {name: {"chat", "report"} for name in _TEXT}
    | {name: {"report"} for name in _REPORT_ONLY}
    | {name: {"prom_plan"} for name in _PROM_ONLY}
)


# --------------------------------------------------------------------------- scenario


class Judge(_Model):
    rubric: str = Field(min_length=10)
    """What a 5/5 answer contains. The judge model grades 1-5 against it (optional feature;
    shown beside the rule score, never mixed into it)."""


class Scenario(_Model):
    id: str
    group: Group
    title: str = Field(min_length=3)
    description: str = Field(min_length=10)
    """What it tests and why: shown in the catalog."""
    snapshot: str = "base"
    """Name of a file in bench/snapshots/, or `private:<name>` (captured from the live server)."""
    patch: dict[str, Any] = Field(default_factory=dict)
    """JSON merge-patch (RFC 7386) applied to the snapshot document. Arrays are replaced
    whole, `null` deletes a key."""
    input: Input
    tool_mode: Literal["auto", "native", "json"] = "auto"
    """Chat only: how tools are offered. auto = what the configured LLM resolves to."""
    world: World = Field(default_factory=World)
    settings: dict[str, Any] = Field(default_factory=dict)
    """Optional JSON merge-patch over the benchmark's Settings profile (rarely needed)."""
    checks: list[Check] = Field(min_length=1)
    judge: Judge | None = None
    tags: list[str] = Field(default_factory=list)
    timeout_s: int | None = Field(default=None, ge=10, le=3600)
    """Time budget per attempt (default: the run's budget)."""

    @field_validator("id")
    @classmethod
    def _id(cls, v: str) -> str:
        if not _ID.match(v):
            raise ValueError("id must look like '<group>.<name>' (lowercase, digits, _)")
        return v

    @model_validator(mode="after")
    def _consistent(self) -> Scenario:
        if self.id.split(".", 1)[0] != self.group:
            raise ValueError(f"id {self.id!r} must start with its group {self.group!r}.")
        kind = self.input.kind
        if self.group != "robust" and kind != self.group:
            raise ValueError(f"group {self.group!r} needs input.kind {self.group!r}, not {kind!r}")
        for check in self.checks:
            allowed = _CHECK_INPUTS[check.type]
            if kind not in allowed:
                raise ValueError(f"check {check.type!r} cannot score a {kind!r} scenario")
        if self.tool_mode != "auto" and kind != "chat":
            raise ValueError("tool_mode only applies to chat scenarios")
        if (self.world.commands or self.world.api or self.world.disk or self.world.mcp) and (
            kind != "chat"
        ):
            raise ValueError("a world only makes sense for chat scenarios")
        return self

    _smoke: bool = PrivateAttr(default=False)

    @property
    def smoke(self) -> bool:
        """Loaded from `_smoke/` (the app's own test scenarios)."""
        return self._smoke

    def public(self) -> dict[str, Any]:
        """The catalog view: what the UI shows about the scenario."""
        return {
            "id": self.id,
            "group": self.group,
            "title": self.title,
            "description": self.description,
            "snapshot": self.snapshot,
            "tool_mode": self.tool_mode,
            "input": self.input.model_dump(mode="json"),
            "checks": [
                {"type": c.type, "weight": c.weight, "describe": c.describe(), "note": c.note}
                for c in self.checks
            ],
            "judge": bool(self.judge),
            "tags": self.tags,
        }


# --------------------------------------------------------------------------- loading


def _read_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ScenarioError(f"{path.name}: {exc}") from exc


def _problem(path: Path, exc: ValidationError) -> ScenarioError:
    lines = [
        f"{'.'.join(str(p) for p in e['loc'])}: {e['msg']}" for e in exc.errors(include_url=False)
    ]
    return ScenarioError(f"{path.parent.name}/{path.name}: " + "; ".join(lines[:6]))


def load_scenario_file(path: Path) -> Scenario:
    """Load one scenario file. The file's folder and name must agree with its id, except
    in `_smoke/`, whose scenarios keep ordinary ids (`chat.smoke_tool`) but are marked smoke."""
    try:
        scenario = Scenario.model_validate(_read_json(path))
    except ValidationError as exc:
        raise _problem(path, exc) from exc
    folder = path.parent.name
    scenario._smoke = folder == SMOKE_GROUP
    if folder != SMOKE_GROUP:
        expected = f"{scenario.group}.{path.stem}"
        if folder != scenario.group or scenario.id != expected:
            raise ScenarioError(
                f"{folder}/{path.name}: id {scenario.id!r} does not match the path "
                f"(expected {expected!r} in {scenario.group}/)"
            )
    return scenario


def load_scenarios(
    root: Path | None = None, *, include_smoke: bool = False
) -> tuple[list[Scenario], list[str]]:
    """All scenarios under `root` (default: the shipped set), sorted by group then id,
    and the list of problems (a broken file is reported, the rest still loads). Duplicate
    ids and missing snapshots are problems too."""
    root = root or SCENARIOS_DIR
    found: dict[str, Scenario] = {}
    problems: list[str] = []
    if not root.is_dir():
        return [], []
    for path in sorted(root.glob("*/*.json")):
        if path.parent.name == SMOKE_GROUP and not include_smoke:
            continue
        try:
            scenario = load_scenario_file(path)
        except ScenarioError as exc:
            problems.append(str(exc))
            continue
        if scenario.id in found:
            problems.append(f"{path.parent.name}/{path.name}: duplicate id {scenario.id!r}")
            continue
        if (
            not scenario.snapshot.startswith(PRIVATE_PREFIX)
            and not snapshot_path(scenario).is_file()
        ):
            problems.append(f"{scenario.id}: unknown snapshot {scenario.snapshot!r}")
        found[scenario.id] = scenario
    order = {g: i for i, g in enumerate(GROUPS)}
    scenarios = sorted(found.values(), key=lambda s: (order.get(s.group, 99), s.id))
    return scenarios, problems


def snapshot_path(scenario: Scenario) -> Path:
    """The file of the (non-private) snapshot a scenario names."""
    folder = SMOKE_SNAPSHOTS_DIR if scenario.smoke else SNAPSHOTS_DIR
    return folder / f"{scenario.snapshot}.json"


def load_snapshot_file(scenario: Scenario) -> dict[str, Any]:
    """The raw snapshot document a scenario names (validated), for patching."""
    path = snapshot_path(scenario)
    name = scenario.snapshot
    if not path.is_file():
        raise ScenarioError(f"unknown snapshot {name!r}")
    doc = _read_json(path)
    try:
        Snapshot.model_validate(doc)
    except ValidationError as exc:
        raise _problem(path, exc) from exc
    return doc  # type: ignore[no-any-return]


def build_snapshot(base: dict[str, Any], patch: dict[str, Any]) -> Snapshot:
    """The snapshot a scenario runs on: `base` with the scenario's merge-patch applied."""
    doc = merge_patch(base, patch) if patch else base
    try:
        return Snapshot.model_validate(doc)
    except ValidationError as exc:
        lines = [f"{'.'.join(str(p) for p in e['loc'])}: {e['msg']}" for e in exc.errors()]
        raise ScenarioError("patched snapshot is invalid: " + "; ".join(lines[:4])) from exc


def scenario_set_hash(scenarios: list[Scenario]) -> str:
    """Short hash of the scenarios (and the file snapshots they use) a run covered, so two
    runs can tell whether they measured the same thing."""
    digest = hashlib.sha256()
    files: dict[str, Path] = {}
    for s in sorted(scenarios, key=lambda s: s.id):
        digest.update(s.id.encode() + b"\0")
        digest.update(json.dumps(s.model_dump(mode="json"), sort_keys=True).encode() + b"\1")
        if not s.snapshot.startswith(PRIVATE_PREFIX):
            files[s.snapshot] = snapshot_path(s)
    for name in sorted(files):
        if files[name].is_file():
            digest.update(name.encode() + b"\0" + files[name].read_bytes() + b"\1")
    return digest.hexdigest()[:12]


def main() -> int:  # pragma: no cover - developer tool
    scenarios, problems = load_scenarios(include_smoke=True)
    for s in scenarios:
        print(f"ok   {s.id}  ({len(s.checks)} checks)")
    for p in problems:
        print(f"FAIL {p}")
    return 1 if problems else 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
