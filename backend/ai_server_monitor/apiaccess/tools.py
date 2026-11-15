"""The two chat tools (`api_call`, `api_endpoints`) and the `api_call` action.

The tools are built by `api_tool_specs(settings)` (plain specs; `chat/tools.py` wraps them in
`Tool`). The action is registered in `actions/registry.py` through the `ActionDef` hooks:
`args_model=ApiCallActionArgs`, `target=action_target`, `check=action_check`,
`run=action_run` (which uses `service.api`, the ApiGateway). Nothing here imports the chat or
action packages.
"""

from __future__ import annotations

import asyncio
import copy
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any, ClassVar, Literal

from pydantic import BaseModel, ConfigDict, Field, create_model

from ..arr.base import ArrError
from . import index
from .errors import ApiAccessError
from .gateway import ApiGateway, ApiRequest
from .groups import GROUP_LABEL, build_groups
from .shaping import DEFAULT_LIMIT, MAX_LIMIT, MAX_RESULT_CHARS
from .targets import KIND_LABEL, METHODS, build_targets, prepare

ACTION_NAME = "api_call"
POLICY_KEY = "api_call"  # TODO setting: arr.actions.api_call, default "approve"
MIN_MEMBER_CHARS = 1500
"""Smallest share of the result budget a member of a group gets."""
ACTION_RESULT_CHARS = 3000
"""Size of the result kept in the audit log of an approved call."""

ToolKind = Literal["read", "write"]

_NOTES = {
    "pending": "Waiting for the owner's approval (approval id {id}). Tell the user to approve "
    "or reject it in the approvals list; it has NOT been done yet.",
    "suggested": "Recorded as a suggestion only; the policy does not let it run. "
    "It has NOT been done.",
    "executed": "Done (see result).",
    "dry_run": "Dry-run mode is on: nothing was sent. It has NOT been done.",
    "failed": "The request failed (see result.error). It has NOT been done.",
    "rejected": "Rejected (see result.detail). It has NOT been done.",
    "queued": "Approved, but held back by the hourly limit; it runs by itself when the limit "
    "frees up. It has NOT been done yet.",
    "deferred": "Held back by the hourly limit (see result.detail). It has NOT been done.",
}


# --------------------------------------------------------------------------- tool specs


@dataclass(frozen=True)
class ToolSpec:
    """Same fields as `chat.tools.Tool`."""

    name: str
    description: str
    args_model: type[BaseModel]
    kind: ToolKind
    handler: Callable[[Any, Any], Awaitable[Any]]
    enabled: Callable[[Any], bool]
    kind_for: Callable[[dict[str, Any]], ToolKind] | None = None

    def as_kwargs(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "description": self.description,
            "args_model": self.args_model,
            "kind": self.kind,
            "handler": self.handler,
            "enabled": self.enabled,
            "kind_for": self.kind_for,
        }


class _CompactArgs(BaseModel):
    """Validates with pydantic, advertises a hand-written minimal JSON schema: a small model
    reads every token of it (no anyOf/null noise, no titles)."""

    model_config = ConfigDict(extra="forbid")
    compact_schema: ClassVar[dict[str, Any]] = {}

    @classmethod
    def model_json_schema(cls, *args: Any, **kwargs: Any) -> dict[str, Any]:
        return copy.deepcopy(cls.compact_schema)


def _api_call_model(services: dict[str, str], groups: dict[str, list[str]]) -> type[BaseModel]:
    names = (*services, *groups) or ("none",)
    model = create_model(
        "ApiCallArgs",
        __base__=_CompactArgs,
        service=(Literal[names], ...),  # type: ignore[valid-type]
        method=(Literal[METHODS], "GET"),  # type: ignore[valid-type]
        path=(str, Field(min_length=1, max_length=500)),
        query=(dict[str, Any] | None, None),
        body=(Any, None),
        select=(str | None, None),
        limit=(int, Field(default=DEFAULT_LIMIT, ge=1, le=MAX_LIMIT)),
        all_pages=(bool, False),
    )
    listing = ", ".join(f"{n} ({label})" for n, label in services.items())
    if groups:
        listing += ". Groups (GET only, every member is asked, answer per instance): " + ", ".join(
            f"{g} ({GROUP_LABEL[g]})" for g in groups
        )
    model.compact_schema = {
        "type": "object",
        "properties": {
            "service": {"type": "string", "enum": list(names), "description": listing},
            "method": {
                "type": "string",
                "enum": list(METHODS),
                "description": "default GET; others need the owner's approval",
            },
            "path": {
                "type": "string",
                "description": "e.g. /api/v3/wanted/missing (see api_endpoints)",
            },
            "query": {
                "type": "object",
                "description": 'URL parameters, e.g. {"page": 1, "pageSize": 50}',
            },
            "body": {"type": "object", "description": "JSON body for POST/PUT"},
            "select": {
                "type": "string",
                "description": "JMESPath, keep only needed fields: "
                "records[].{title: title, year: year}",
            },
            "limit": {
                "type": "integer",
                "description": f"max list items (default {DEFAULT_LIMIT})",
            },
            "all_pages": {"type": "boolean", "description": "fetch every page of a paged list"},
        },
        "required": ["service", "path"],
    }
    return model


def _endpoints_model(services: dict[str, str], groups: dict[str, list[str]]) -> type[BaseModel]:
    names = (*services, *groups) or ("none",)
    model = create_model(
        "ApiEndpointsArgs",
        __base__=_CompactArgs,
        service=(Literal[names], ...),  # type: ignore[valid-type]
        search=(str | None, Field(default=None, max_length=100)),
    )
    model.compact_schema = {
        "type": "object",
        "properties": {
            "service": {"type": "string", "enum": list(names)},
            "search": {
                "type": "string",
                "description": "words, e.g. missing, queue, indexer, torrents, mounts",
            },
        },
        "required": ["service"],
    }
    return model


API_CALL_DESCRIPTION = (
    "Call the HTTP API of a configured service and get real data (Radarr/Sonarr/Whisparr "
    "library, queue, missing; Prowlarr indexers; qBittorrent torrents; Docker containers; "
    "Prometheus). Unsure of the path or parameters? Call api_endpoints first: it has "
    "ready-made examples. Always pass select to return only the fields you need. "
    "For a question about all movies or all series use the group service movies / series "
    "(every Radarr+Whisparr / Sonarr instance at once). "
    "GET runs now. POST/PUT/DELETE/PATCH are only requested and need the owner's approval "
    "(they are NOT done until approved: say so)."
)
API_ENDPOINTS_DESCRIPTION = (
    "Find the right API call for a service: ready-made examples (with select) and matching "
    "endpoints (method, path, parameters). Use before api_call when unsure. "
    "Examples: search=missing, queue, indexer, torrents, history."
)


def api_tool_specs(
    settings: Any,
    *,
    gateway_of: Callable[[Any], ApiGateway] = lambda ctx: ctx.app.api_gateway,
    actions_of: Callable[[Any], Any] = lambda ctx: ctx.app.actions,
    error_cls: type[Exception] = ApiAccessError,
) -> list[ToolSpec]:
    """The two tools for the services these settings configure (none when no service is).

    `gateway_of(ctx)` / `actions_of(ctx)` find the ApiGateway and the ActionService from a
    tool context; `error_cls` is the exception type whose message goes back to the model
    (the chat's ToolError)."""
    targets = build_targets(settings)
    services = {name: target.label for name, target in targets.items()}
    groups = build_groups(targets)

    async def endpoints(ctx: Any, args: Any) -> Any:
        gateway = gateway_of(ctx)
        group = build_groups(gateway.targets).get(args.service)
        target = gateway.targets.get(group[0] if group else args.service)
        if target is None:
            raise error_cls(f"unknown service {args.service!r}; available: {', '.join(services)}")
        result = index.search(target.kind, args.search) | {"service": args.service}
        if group:
            result["group"] = (
                f"{args.service} = {', '.join(group)}: api_call with service={args.service} "
                "sends the same GET to each of them"
            )
            return result
        related = _related(gateway, args.service)
        if related:
            result["also"] = related
        return result

    async def call(ctx: Any, args: Any) -> Any:
        gateway = gateway_of(ctx)
        members = build_groups(gateway.targets).get(args.service)
        if members is not None:
            return await _fan_out(gateway, args, members, _source(ctx), error_cls)
        try:
            prepared = gateway.prepare(args.service, args.method, args.path, args.query)
            if not prepared.write_like:
                result = await gateway.execute(
                    ApiRequest(
                        service=args.service,
                        method=args.method,
                        path=args.path,
                        query=args.query,
                        body=args.body,
                        select=args.select,
                        limit=args.limit,
                        all_pages=args.all_pages,
                    ),
                    source=_source(ctx),
                )
                # Said again next to the data, where the model reads it just before it answers.
                related = _related(gateway, args.service)
                return {**result, "also": related} if related else result
        except ApiAccessError as exc:
            raise error_cls(str(exc)) from exc
        return await _request_approval(ctx, actions_of(ctx), args, prepared.display, error_cls)

    enabled: Callable[[Any], bool] = lambda s: bool(build_targets(s))  # noqa: E731
    return [
        ToolSpec(
            "api_endpoints",
            API_ENDPOINTS_DESCRIPTION,
            _endpoints_model(services, groups),
            "read",
            endpoints,
            enabled,
        ),
        ToolSpec(
            "api_call",
            API_CALL_DESCRIPTION,
            _api_call_model(services, groups),
            "write",
            call,
            enabled,
            kind_for=_call_kind,
        ),
    ]


async def _fan_out(
    gateway: ApiGateway,
    args: Any,
    members: list[str],
    source: str,
    error_cls: type[Exception],
) -> dict[str, Any]:
    """One GET to every member of a group; each answer is shaped (select, limit, size) on
    its own and they come back side by side. A member that fails reports its error."""
    if args.method != "GET":
        raise error_cls(
            f"{args.service} is a group: GET only. Name one service for {args.method} "
            f"({', '.join(members)})"
        )
    try:
        probe = gateway.prepare(members[0], "GET", args.path, args.query)
    except ApiAccessError as exc:
        raise error_cls(str(exc)) from exc
    if probe.write_like:
        raise error_cls(
            f"{probe.display} queries every indexer and needs the owner's approval: "
            f"name one service ({', '.join(members)}), not the group {args.service}"
        )
    budget = max(MAX_RESULT_CHARS // len(members), MIN_MEMBER_CHARS)

    async def one(name: str) -> dict[str, Any]:
        try:
            result = await gateway.execute(
                ApiRequest(
                    service=name,
                    path=args.path,
                    query=args.query,
                    select=args.select,
                    limit=args.limit,
                    all_pages=args.all_pages,
                ),
                source=source,
                max_chars=budget,
            )
        except ApiAccessError as exc:
            return {"error": str(exc)}
        return {k: v for k, v in result.items() if k not in ("service", "call")}

    answers = await asyncio.gather(*(one(name) for name in members))
    if all("error" in a for a in answers):
        raise error_cls(f"{members[0]}: {answers[0]['error']}")
    return {
        "group": args.service,
        "call": probe.display,
        "results": dict(zip(members, answers, strict=True)),
    }


_FAMILIES = (frozenset({"radarr", "whisparr"}),)  # both hold movies, same API


def _related(gateway: ApiGateway, service: str) -> str | None:
    """Other configured services that answer the same kind of question (a second Radarr,
    Whisparr next to Radarr): a model that was asked about "movies" tends to stop at the first."""
    kind = gateway.targets[service].kind
    others = [
        name
        for name, target in gateway.targets.items()
        if name != service
        and (
            target.kind == kind
            or any(kind in family and target.kind in family for family in _FAMILIES)
        )
    ]
    if not others:
        return None
    return f"{', '.join(others)} holds the same kind of data: query it too for a complete answer"


def _call_kind(args: dict[str, Any]) -> ToolKind:
    """GET reads; anything else is a request for a change."""
    return "read" if str(args.get("method") or "GET").upper() == "GET" else "write"


def _source(ctx: Any) -> str:
    return getattr(ctx, "source", None) or f"chat:{getattr(ctx, 'session_id', '')}"


async def _request_approval(
    ctx: Any, actions: Any, args: Any, display: str, error_cls: type[Exception]
) -> dict[str, Any]:
    """File the call as an action request: policy decides (default: owner approval)."""
    payload = {
        "service": args.service,
        "method": args.method,
        "path": args.path,
        "query": args.query,
        "body": args.body,
        "title": display,
    }
    try:
        record = await actions.request(
            ACTION_NAME,
            {k: v for k, v in payload.items() if v is not None},
            _source(ctx),
            reason="requested in chat",
            run_key=f"turn:{ctx.turn}" if getattr(ctx, "turn", None) else None,
            label="Chat",
        )
    except Exception as exc:  # ActionError: unknown action / invalid arguments
        raise error_cls(str(exc)) from exc
    return {
        "action_id": record["id"],
        "approval_id": record.get("batch_id"),
        "status": record["status"],
        "policy": record["policy"],
        "target": record["target"],
        "result": record["result"],
        "note": _NOTES.get(record["status"], "").format(id=record["id"]),
    }


# --------------------------------------------------------------------------- the action

ACTION_DESCRIPTION = (
    "Call a write endpoint (or an indexer-querying GET) of a configured service's HTTP API."
)


class ApiCallActionArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")

    title: str = Field(default="", max_length=200)
    """Human-readable name; only used in the audit log."""
    service: str = Field(min_length=1, max_length=64)
    method: Literal["GET", "POST", "PUT", "DELETE", "PATCH"]
    path: str = Field(min_length=1, max_length=500)
    query: dict[str, Any] | None = None
    body: Any = None


def action_target(args: ApiCallActionArgs) -> str:
    return f"{args.service}: {args.method} {args.path}"


def action_check(settings: Any, args: ApiCallActionArgs) -> str | None:
    """`ActionDef.check`: why the request cannot be carried out (unknown service, bad path,
    a write to a read-only service), or None. Needs only the settings."""
    target = build_targets(settings).get(args.service)
    if target is None:
        known = ", ".join(build_targets(settings)) or "none configured"
        return f"unknown service {args.service!r}; available: {known}"
    try:
        prepare(target, args.method, args.path, args.query)
    except ApiAccessError as exc:
        return str(exc)
    return None


async def action_run(service: Any, args: ApiCallActionArgs) -> dict[str, Any]:
    """`ActionDef.run`: runs the approved call through `service.api` (the ApiGateway). The
    request is validated again here; the stored arguments are never trusted. A failed call
    raises ArrError, which the action layer records as `failed` with the message."""
    try:
        return await service.api.execute(
            ApiRequest(
                service=args.service,
                method=args.method,
                path=args.path,
                query=args.query,
                body=args.body,
                limit=20,
            ),
            approved=True,
            source="action:api_call",
            max_chars=ACTION_RESULT_CHARS,
        )
    except ApiAccessError as exc:
        raise ArrError(str(exc), status=exc.status) from exc


__all__ = [
    "ACTION_DESCRIPTION",
    "ACTION_NAME",
    "KIND_LABEL",
    "POLICY_KEY",
    "ApiCallActionArgs",
    "ToolSpec",
    "action_check",
    "action_run",
    "action_target",
    "api_tool_specs",
]
