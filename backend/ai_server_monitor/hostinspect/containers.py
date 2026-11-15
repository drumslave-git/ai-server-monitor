"""Docker container summaries from the Engine API's inspect/list output. Environment
variables are reported by name only: their values are secrets more often than not."""

from __future__ import annotations

from typing import Any

MAX_ENV_NAMES = 40
MAX_HEALTH_LOG = 3


def _ports(raw: dict[str, Any] | None) -> list[str]:
    out = []
    for container_port, bindings in (raw or {}).items():
        if not bindings:
            continue
        for b in bindings:
            host = b.get("HostIp") or ""
            host = "" if host in ("0.0.0.0", "::") else host + ":"
            out.append(f"{host}{b.get('HostPort')}->{container_port}")
    return sorted(set(out))


def _mounts(mounts: list[dict[str, Any]] | None) -> list[str]:
    out = []
    for m in mounts or []:
        kind = m.get("Type")
        source = m.get("Name") if kind == "volume" and m.get("Name") else m.get("Source")
        mode = "rw" if m.get("RW", True) else "ro"
        out.append(f"{kind}: {source} -> {m.get('Destination')} ({mode})")
    return out


def summarize_inspect(raw: dict[str, Any]) -> dict[str, Any]:
    state = raw.get("State") or {}
    config = raw.get("Config") or {}
    host = raw.get("HostConfig") or {}
    labels = config.get("Labels") or {}
    health = state.get("Health") or {}
    env_names = sorted({e.split("=", 1)[0] for e in config.get("Env") or [] if e})
    networks = (raw.get("NetworkSettings") or {}).get("Networks") or {}
    running = state.get("Running")
    out: dict[str, Any] = {
        "name": str(raw.get("Name", "")).lstrip("/"),
        "id": str(raw.get("Id", ""))[:12],
        "image": config.get("Image"),
        "created": raw.get("Created"),
        "state": state.get("Status"),
        "running": running,
        "started_at": state.get("StartedAt"),
        "finished_at": None if running else state.get("FinishedAt"),
        "exit_code": None if running else state.get("ExitCode"),
        "oom_killed": state.get("OOMKilled"),
        "error": state.get("Error") or None,
        "restart_count": raw.get("RestartCount"),
        "restart_policy": (host.get("RestartPolicy") or {}).get("Name"),
        "health": health.get("Status"),
        "command": " ".join([*(config.get("Entrypoint") or []), *(config.get("Cmd") or [])])[:300]
        or None,
        "mounts": _mounts(raw.get("Mounts")),
        "ports": _ports((raw.get("NetworkSettings") or {}).get("Ports")),
        "networks": sorted(networks),
        "network_mode": host.get("NetworkMode"),
        "compose_project": labels.get("com.docker.compose.project"),
        "compose_service": labels.get("com.docker.compose.service"),
        "env_names": env_names[:MAX_ENV_NAMES],
        "env_values": "hidden (names only)",
    }
    log = health.get("Log") or []
    if log:
        out["health_log"] = [
            {"exit": e.get("ExitCode"), "output": str(e.get("Output", ""))[:200].strip()}
            for e in log[-MAX_HEALTH_LOG:]
        ]
    return {k: v for k, v in out.items() if v not in (None, [], "")}


def summarize_list(entries: list[dict[str, Any]]) -> list[dict[str, Any]]:
    rows = []
    for e in entries:
        names = e.get("Names") or []
        ports = sorted(
            {
                f"{p['PublicPort']}->{p['PrivatePort']}/{p.get('Type', 'tcp')}"
                for p in e.get("Ports") or []
                if p.get("PublicPort")
            }
        )
        candidate = {
            "name": (names[0] if names else str(e.get("Id", ""))[:12]).lstrip("/"),
            "image": e.get("Image"),
            "state": e.get("State"),
            "status": e.get("Status"),
            "ports": ports,
            "mounts": len(e.get("Mounts") or []),
        }
        rows.append({k: v for k, v in candidate.items() if v not in (None, [], "")})
    rows.sort(key=lambda r: (r.get("state") != "running", r["name"]))
    return rows
