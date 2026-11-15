"""The two chat tools (`api_call`, `api_endpoints`) and the `api_call` action.

Nothing here imports the chat or action packages (they will import this one): the builders
return plain specs and pieces the registries wrap. Wiring, in the shared files:

    tools:    for spec in api_tool_specs(settings): Tool(**spec.as_kwargs())
    actions:  ActionDef(name=ACTION_NAME, description=ACTION_DESCRIPTION,
                        args_model=ApiCallActionArgs, policy_key=lambda a: POLICY_KEY,
                        target=action_target, check=make_action_check(get_gateway),
                        execute=make_action_executor(get_gateway))
"""

from __future__ import annotations

import copy
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any, ClassVar, Literal

from pydantic import BaseModel, ConfigDict, Field, create_model

from ..arr.base import ArrError
from . import index
from .errors import ApiAccessError
from .gateway import ApiGateway, ApiRequest
from .shaping import DEFAULT_LIMIT, MAX_LIMIT
from .targets import KIND_LABEL, METHODS, build_targets

ACTION_NAME = "api_call"
POLICY_KEY = "api_call"  # TODO setting: arr.actions.api_call, default "approve"
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

    def as_kwargs(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "description": self.description,
            "args_model": self.args_model,
            "kind": self.kind,
            "handler": self.handler,
            "enabled": self.enabled,
        }


class _CompactArgs(BaseModel):
    """Validates with pydantic, advertises a hand-written minimal JSON schema: a small model
    reads every token of it (no anyOf/null noise, no titles)."""

    model_config = ConfigDict(extra="forbid")
    compact_schema: ClassVar[dict[str, Any]] = {}

    @classmethod
    def model_json_schema(cls, *args: Any, **kwargs: Any) -> dict[str, Any]:
        return copy.deepcopy(cls.compact_schema)


def _api_call_model(services: dict[str, str]) -> type[BaseModel]:
    names = tuple(services) or ("none",)
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


def _endpoints_model(services: dict[str, str]) -> type[BaseModel]:
    names = tuple(services) or ("none",)
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
    services = {name: target.label for name, target in build_targets(settings).items()}

    async def endpoints(ctx: Any, args: Any) -> Any:
        gateway = gateway_of(ctx)
        target = gateway.target(args.service) if args.service in gateway.targets else None
        if target is None:
            raise error_cls(f"unknown service {args.service!r}; available: {', '.join(services)}")
        result = index.search(target.kind, args.search) | {"service": args.service}
        related = _related(gateway, args.service)
        if related:
            result["also"] = related
        return result

    async def call(ctx: Any, args: Any) -> Any:
        gateway = gateway_of(ctx)
        try:
            prepared = gateway.prepare(args.service, args.method, args.path, args.query)
            if not prepared.write_like:
                return await gateway.execute(
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
        except ApiAccessError as exc:
            raise error_cls(str(exc)) from exc
        return await _request_approval(ctx, actions_of(ctx), args, prepared.display, error_cls)

    enabled: Callable[[Any], bool] = lambda s: bool(build_targets(s))  # noqa: E731
    return [
        ToolSpec(
            "api_endpoints",
            API_ENDPOINTS_DESCRIPTION,
            _endpoints_model(services),
            "read",
            endpoints,
            enabled,
        ),
        ToolSpec(
            "api_call", API_CALL_DESCRIPTION, _api_call_model(services), "write", call, enabled
        ),
    ]


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
        )
    except Exception as exc:  # ActionError: unknown action / invalid arguments
        raise error_cls(str(exc)) from exc
    return {
        "action_id": record["id"],
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


def make_action_check(get_gateway: Callable[[], ApiGateway]) -> Callable[[Any, Any], str | None]:
    """`ActionDef.check`: why the request cannot be carried out (unknown service, bad path,
    a write to a read-only service), or None."""

    def check(settings: Any, args: ApiCallActionArgs) -> str | None:
        try:
            get_gateway().prepare(args.service, args.method, args.path, args.query)
        except ApiAccessError as exc:
            return str(exc)
        return None

    return check


def make_action_executor(
    get_gateway: Callable[[], ApiGateway],
) -> Callable[[Any, ApiCallActionArgs], Awaitable[dict[str, Any]]]:
    """`ActionDef.execute`: runs the approved call. The request is validated again here;
    the stored arguments are never trusted. A failed call raises ArrError, which the action
    layer records as `failed` with the message."""

    async def execute(clients: Any, args: ApiCallActionArgs) -> dict[str, Any]:
        try:
            return await get_gateway().execute(
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

    return execute


__all__ = [
    "ACTION_DESCRIPTION",
    "ACTION_NAME",
    "KIND_LABEL",
    "POLICY_KEY",
    "ApiCallActionArgs",
    "ToolSpec",
    "action_target",
    "api_tool_specs",
    "make_action_check",
    "make_action_executor",
]
