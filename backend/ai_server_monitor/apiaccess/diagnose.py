"""`diagnose_service`: the chat's tool for "why can't you reach X?".

It runs the same connection test as the Settings → Test button (full details: HTTP status, body
excerpt, headers sent, what it means), adds the non-secret configuration of that service (URL,
user name, whether a password/key is set, when the settings were last saved: never the secret),
what the collector saw last, the login back-off state, and where to look next (the service's
container, its logs call and its config file), so the model investigates instead of guessing.
"""

from __future__ import annotations

import re
import time
from typing import Any
from urllib.parse import quote

from pydantic import BaseModel, ConfigDict, Field

from ..arr import qbit_auth
from ..config import Settings
from ..settings_store import KV_KEY

_ARR_KINDS = ("radarr", "sonarr", "whisparr")
_ALIASES = {"qbit": "qbittorrent", "qb": "qbittorrent", "qbittorrent": "qbittorrent"}
_CONTAINER_WORDS = {"qbittorrent": ("qbittorrent", "qbit")}
# Where each app keeps its config inside the container (the usual /config mount).
_CONFIG_FILES = {"qbittorrent": "qBittorrent/qBittorrent.conf", "arr": "config.xml"}
LOGS_QUERY = {"stdout": True, "stderr": True, "tail": 200, "timestamps": True}


class DiagnoseArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")
    service: str = Field(
        description="qbittorrent, prowlarr, radarr, sonarr, whisparr (or an instance name), "
        "prometheus or docker."
    )


DESCRIPTION = (
    "Why can't the monitor reach a configured service (qBittorrent, Radarr, Sonarr, Prowlarr, "
    "Prometheus, Docker)? Runs the connection test with full details (HTTP status, body, "
    "headers sent, what it means) plus the service's non-secret settings, the collector's "
    "last error and the retry pause. Call it FIRST when a service is unreachable, then read "
    "the container logs and config it points to."
)


def _services(settings: Settings) -> list[tuple[str, str, int | None, dict[str, Any]]]:
    """(kind, instance name, index, stored settings node) of everything that can be tested."""
    stored = settings.storage_dict()
    arr = stored["arr"]
    out: list[tuple[str, str, int | None, dict[str, Any]]] = []
    for kind in _ARR_KINDS:
        for i, node in enumerate(arr.get(kind) or []):
            out.append((kind, str(node.get("name") or kind), i, node))
    for kind in ("prowlarr", "qbittorrent"):
        if arr.get(kind):
            out.append((kind, kind, None, arr[kind]))
    prom = stored["collectors"]["prometheus"]
    if prom.get("url"):
        out.append(("prometheus", "prometheus", None, prom))
    out.append(("docker", "docker", None, stored["collectors"]["docker"]))
    return out


def _resolve(settings: Settings, wanted: str) -> list[tuple[str, str, int | None, dict[str, Any]]]:
    word = _ALIASES.get(wanted.strip().lower(), wanted.strip().lower())
    found = _services(settings)
    exact = [s for s in found if word in (s[0], s[1].lower())]
    return exact or [s for s in found if word in s[1].lower() or s[0] in word]


def _config_view(kind: str, node: dict[str, Any], saved_at: float | None) -> dict[str, Any]:
    out: dict[str, Any] = {"url": node.get("url")}
    if kind == "qbittorrent":
        out["username"] = node.get("username")
        out["pw_configured"] = bool(node.get("password"))
    elif kind == "prometheus":
        out["bearer_configured"] = bool(node.get("token"))
        out["username"] = node.get("username") or None
        out["pw_configured"] = bool(node.get("password"))
    elif kind != "docker":
        out["key_configured"] = bool(node.get("api_key"))
    if saved_at:
        out["settings_last_saved"] = (
            f"{_ago(time.time() - saved_at)} ago (all settings are saved together)"
        )
    return {k: v for k, v in out.items() if v is not None}


def _ago(seconds: float) -> str:
    seconds = max(int(seconds), 0)
    for unit, size in (("d", 86400), ("h", 3600), ("min", 60)):
        if seconds >= size:
            return f"{seconds // size} {unit}"
    return f"{seconds} s"


def _collector_view(ctx: Any, kind: str, name: str) -> dict[str, Any] | None:
    collector = "arr" if kind in (*_ARR_KINDS, "prowlarr", "qbittorrent") else kind
    latest = getattr(ctx.app.collectors, "latest", {}).get(collector)
    if latest is None:
        return {"collector": collector, "note": "has not run yet"}
    out: dict[str, Any] = {
        "collector": collector,
        "last_run": f"{_ago(time.time() - latest.ts)} ago",
        "collector_ok": latest.ok,
    }
    if latest.error:
        out["collector_error"] = latest.error[:600]
    if collector == "arr":
        entries = [*(latest.data.get("apps") or [])]
        for key in ("prowlarr", "qbittorrent"):
            if latest.data.get(key):
                entries.append(latest.data[key])
        for entry in entries:
            if entry.get("type") == kind and entry.get("name") == name:
                out["this_service_ok"] = entry.get("ok")
                if entry.get("error"):
                    out["this_service_error"] = str(entry["error"])[:600]
    return out


_RELEVANT = re.compile(
    r"warn|fail|login|auth|invalid|unauthori|forbidden|denied|ban|host|csrf|error|refus", re.I
)
LOG_LINES_SHOWN = 12


async def _log_evidence(api: Any, container: str) -> dict[str, Any]:
    """The lines of the last 200 that look relevant (or the newest ones): the service's own
    account of what happened, so the model does not have to ask for it."""
    from ..collectors.docker import demux_logs
    from ..hostinspect.output import scrub_output

    try:
        raw = await api.get(
            f"/containers/{quote(container, safe='')}/logs",
            stdout="1",
            stderr="1",
            tail="200",
            timestamps="1",
        )
    except Exception as exc:
        return {"log_error": f"cannot read the logs: {exc}"[:200]}
    if not isinstance(raw, bytes | bytearray):
        return {}
    lines = demux_logs(bytes(raw))
    picked = [line for line in lines if _RELEVANT.search(line)] or lines
    shown = [scrub_output(line[:300]) for line in picked[-LOG_LINES_SHOWN:]]
    return {"log_lines_read": len(lines), "relevant_log_lines": shown}


async def _container(
    ctx: Any, kind: str, name: str, *, with_logs: bool = False
) -> dict[str, Any] | None:
    """Best effort: the Docker container behind this service, with its config bind mount."""
    api = getattr(ctx.app.collectors.collectors.get("docker"), "api", None)
    if api is None or kind == "docker":
        return None
    words = _CONTAINER_WORDS.get(kind, (kind, name.lower()))
    try:
        listing = await api.get("/containers/json", all="true")
    except Exception as exc:
        return {"error": f"Docker API: {exc}"[:200]}
    matches = []
    for c in listing:
        haystack = " ".join([*c.get("Names", []), str(c.get("Image", ""))]).lower()
        if any(w and w in haystack for w in words):
            matches.append(c)
    if not matches:
        return {"note": "no container matches; list them with container_inspect (no name)"}
    chosen = next((c for c in matches if c.get("State") == "running"), matches[0])
    cname = str((chosen.get("Names") or ["?"])[0]).lstrip("/")
    out: dict[str, Any] = {
        "name": cname,
        "state": chosen.get("State"),
        "status": chosen.get("Status"),
        "image": chosen.get("Image"),
    }
    if with_logs:
        out.update(await _log_evidence(api, cname))
    if len(matches) > 1:
        out["other_matches"] = [str((c.get("Names") or ["?"])[0]).lstrip("/") for c in matches[1:]]
    config = next(
        (m for m in chosen.get("Mounts") or [] if m.get("Destination") == "/config"), None
    )
    if config and config.get("Source"):
        file = _CONFIG_FILES.get(kind, _CONFIG_FILES["arr"] if kind in _ARR_KINDS else "")
        out["config_dir_on_host"] = config["Source"]
        if file:
            out["config_file_on_host"] = f"{str(config['Source']).rstrip('/')}/{file}"
    return out


def _next_steps(kind: str, container: dict[str, Any] | None) -> list[str]:
    steps = []
    if container and container.get("name"):
        steps.append(
            "Container logs: api_call service=docker GET "
            f"/containers/{quote(container['name'], safe='')}/logs "
            f"query={LOGS_QUERY} (what the service itself says happened)"
        )
        if path := container.get("config_file_on_host"):
            grep = "grep -i webui" if kind == "qbittorrent" else "cat"
            steps.append(f"Config file: run_command `{grep} {path}` (secrets are hidden)")
    else:
        steps.append("Find the container with container_inspect (no name), then read its logs.")
    return steps


async def diagnose(ctx: Any, args: DiagnoseArgs, error_cls: type[Exception]) -> Any:
    from ..conntest import run_test  # (conntest pulls in the MCP client: import late)

    settings: Settings = ctx.app.settings
    targets = _resolve(settings, args.service)
    if not targets:
        known = ", ".join(sorted({s[1] for s in _services(settings)}))
        raise error_cls(f"no service {args.service!r} is configured; configured: {known}")
    row = await ctx.app.db.fetchone("SELECT updated FROM kv WHERE key = ?", (KV_KEY,))
    saved_at = float(row[0]) if row else None
    results = []
    for kind, name, index, node in targets[:3]:
        test = await run_test(kind, node, settings, ctx.app.host_env, index=index)
        view: dict[str, Any] = {
            "service": name,
            "kind": kind,
            "config": _config_view(kind, node, saved_at),
            "connection_test": {"ok": test.ok, "message": test.message, "details": test.details},
        }
        if kind == "qbittorrent":
            view["login_retry_state"] = qbit_auth.state_of(
                qbit_auth.backoff_key(
                    str(node.get("url") or ""),
                    str(node.get("username") or ""),
                    str(node.get("password") or ""),
                )
            )
        view["collector"] = _collector_view(ctx, kind, name)
        container = await _container(ctx, kind, name, with_logs=not test.ok)
        if container:
            view["container"] = container
        if not test.ok:
            view["next_steps"] = _next_steps(kind, container)
        results.append(view)
    return {"diagnosis": results}


def diagnose_spec(error_cls: type[Exception]) -> tuple[str, str, type[BaseModel], Any]:
    async def handler(ctx: Any, args: DiagnoseArgs) -> Any:
        return await diagnose(ctx, args, error_cls)

    return "diagnose_service", DESCRIPTION, DiagnoseArgs, handler
