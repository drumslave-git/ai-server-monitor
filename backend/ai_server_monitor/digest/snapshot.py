"""The digester's input: latest collector results, in the same shape as GET /api/collectors.

    {name: {"state": "active|disabled|unavailable", "reason": ..., "ok": ..., "error": ...,
            "last_ts": ..., "result": {"ts": ..., "ok": ..., "data": {...}, "error": ...} | None}}

Using the API shape means a live instance's `/api/collectors` output can be digested
as-is (that is how the real-data check works).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from ..collectors import CollectorManager


def collectors_snapshot(manager: CollectorManager) -> dict[str, dict[str, Any]]:
    return {
        name: {**status, "result": r.to_dict() if (r := manager.latest.get(name)) else None}
        for name, status in manager.status().items()
    }


_LIST_KEYS = {
    "system": ("filesystems", "disks", "network", "temperatures"),
    "docker": ("containers",),
    "systemd": ("failed", "restarting"),
    "gpu": ("gpus",),
    "apt": ("packages",),
    "image_updates": ("images",),
    "arr": ("findings", "apps"),
}
_DICT_KEYS = {
    "system": ("cpu", "memory", "processes"),
    "docker": ("summary", "engine", "disk_usage"),
    "image_updates": ("summary",),
    "arr": ("summary",),
}


def _dicts(value: Any) -> list[dict[str, Any]]:
    return [item for item in value if isinstance(item, dict)] if isinstance(value, list) else []


def sanitize(name: str, data: dict[str, Any]) -> dict[str, Any]:
    """Make the shapes rules and sections rely on safe: known list keys hold only dicts,
    known dict keys are dicts. Collector data is not trusted to be well-formed."""
    out = dict(data)
    for key in _LIST_KEYS.get(name, ()):
        if key in out:
            out[key] = _dicts(out[key])
    for key in _DICT_KEYS.get(name, ()):
        if key in out and not isinstance(out[key], dict):
            out[key] = {}
    procs = out.get("processes")
    if name == "system" and isinstance(procs, dict):
        out["processes"] = {
            **procs,
            "top_cpu": _dicts(procs.get("top_cpu")),
            "top_memory": _dicts(procs.get("top_memory")),
        }
    if name == "gpu":
        out["gpus"] = [{**g, "processes": _dicts(g.get("processes"))} for g in out.get("gpus", [])]
    return out


class Snapshot:
    def __init__(self, raw: dict[str, dict[str, Any]], now: float):
        self.raw = raw
        self.now = now

    def entry(self, name: str) -> dict[str, Any] | None:
        return self.raw.get(name)

    def data(self, name: str) -> dict[str, Any]:
        """The collector's data if its latest run succeeded, else {}."""
        entry = self.raw.get(name) or {}
        result = entry.get("result") or {}
        data = result.get("data") if result.get("ok") else None
        return sanitize(name, data) if isinstance(data, dict) else {}

    def has(self, name: str) -> bool:
        return bool(self.data(name))

    def names(self) -> list[str]:
        return list(self.raw)
