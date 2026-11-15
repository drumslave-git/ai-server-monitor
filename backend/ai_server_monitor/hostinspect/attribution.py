"""Who owns a path on the host: Docker bind mounts and volumes, *arr root folders, the Docker
data root and a few well-known system locations. Pure data in, strings out; the callers fetch
the data (Docker API, *arr clients) and hand it over."""

from __future__ import annotations

import posixpath
from dataclasses import dataclass, field
from typing import Any

KNOWN_PATHS: tuple[tuple[str, str], ...] = (
    ("/var/lib/docker/overlay2", "Docker image layers and container writable layers"),
    ("/var/lib/docker/volumes", "Docker named volumes"),
    ("/var/lib/docker/containers", "Docker container logs and config"),
    ("/var/lib/docker/buildkit", "Docker build cache"),
    ("/var/lib/docker", "Docker data root"),
    ("/var/lib/containerd", "containerd images and snapshots"),
    ("/var/log/journal", "systemd journal (persistent logs)"),
    ("/var/log", "system log files"),
    ("/var/cache/apt", "apt package cache (apt clean frees it)"),
    ("/var/lib/apt", "apt package lists"),
    ("/var/lib/snapd", "snap packages"),
    ("/snap", "snap packages"),
    ("/var/lib/ollama", "Ollama models"),
    ("/usr/share/ollama", "Ollama models"),
    ("/swap.img", "swap file"),
    ("/swapfile", "swap file"),
    ("/lost+found", "filesystem recovery directory"),
)

MAX_OWNERS = 3


def _norm(path: str) -> str:
    return posixpath.normpath("/" + path.lstrip("/")) if path else "/"


def _under(path: str, base: str) -> bool:
    return path == base or path.startswith(base.rstrip("/") + "/")


@dataclass
class Location:
    path: str
    """Host path."""
    owner: str
    """Human description, e.g. "container 'jellyfin' bind mount (/data, read-write)"."""
    kind: str = "mount"


@dataclass
class Locations:
    items: list[Location] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    def add(self, path: str, owner: str, kind: str = "mount") -> None:
        if path:
            self.items.append(Location(_norm(path), owner, kind))

    def owners(self, path: str) -> list[str]:
        """Descriptions of everything that owns or sits at `path` (most specific first)."""
        path = _norm(path)
        found: list[tuple[int, str]] = []
        for loc in self.items:
            if loc.path == path:
                found.append((0, loc.owner))
            elif _under(path, loc.path):
                found.append((1, f"inside {loc.owner}"))
        for prefix, text in KNOWN_PATHS:
            if _under(path, prefix):
                found.append((2, text))
                break
        found.sort(key=lambda f: f[0])
        out: list[str] = []
        for _, text in found:
            if text not in out:
                out.append(text)
        if len(out) > MAX_OWNERS:
            out = [*out[:MAX_OWNERS], f"and {len(out) - MAX_OWNERS} more"]
        return out

    def inside(self, path: str, *, limit: int = 12) -> list[dict[str, str]]:
        """Owners located below `path` (possibly deeper than the listed levels)."""
        path = _norm(path)
        rows = [
            {"path": loc.path, "owner": loc.owner}
            for loc in self.items
            if loc.path != path and _under(loc.path, path)
        ]
        rows.sort(key=lambda r: r["path"])
        seen: set[tuple[str, str]] = set()
        unique = []
        for row in rows:
            key = (row["path"], row["owner"])
            if key not in seen:
                seen.add(key)
                unique.append(row)
        return unique[:limit]


def _container_name(entry: dict[str, Any]) -> str:
    names = entry.get("Names") or []
    return (names[0] if names else str(entry.get("Id", "?"))[:12]).lstrip("/")


def add_docker(
    locations: Locations, containers: list[dict[str, Any]], info: dict[str, Any]
) -> None:
    """Bind mounts and volumes of every container, plus the Docker data root."""
    root = info.get("DockerRootDir")
    if root:
        locations.add(root, "Docker data root", "docker")
    for entry in containers:
        name = _container_name(entry)
        for mount in entry.get("Mounts") or []:
            source = mount.get("Source")
            dest = mount.get("Destination")
            mode = "read-write" if mount.get("RW", True) else "read-only"
            if mount.get("Type") == "volume":
                volume = mount.get("Name") or "?"
                locations.add(
                    source or "",
                    f"Docker volume '{volume}' (used by container '{name}' at {dest})",
                    "docker",
                )
            elif mount.get("Type") == "bind":
                locations.add(
                    source or "",
                    f"bind mount of container '{name}' ({dest}, {mode})",
                    "docker",
                )


def _arr_containers(
    containers: list[dict[str, Any]], kind: str, instance: str
) -> list[dict[str, Any]]:
    kind, instance = kind.lower(), instance.lower()
    out = []
    for entry in containers:
        labels = entry.get("Labels") or {}
        haystack = " ".join(
            [
                _container_name(entry),
                str(entry.get("Image", "")),
                str(labels.get("com.docker.compose.service", "")),
            ]
        ).lower()
        if kind in haystack or (instance and instance in haystack):
            out.append(entry)
    return out


def add_arr(
    locations: Locations,
    arr_roots: list[dict[str, Any]],
    containers: list[dict[str, Any]],
) -> None:
    """*arr root folders. They are paths inside the arr app's container, so each one is
    translated through that container's bind mounts when the container can be found."""
    for root in arr_roots:
        kind, instance, path = root["app"], root["instance"], root["path"]
        label = f"{kind} '{instance}' root folder {path}"
        translated = False
        for entry in _arr_containers(containers, kind, instance):
            for mount in entry.get("Mounts") or []:
                dest, source = mount.get("Destination"), mount.get("Source")
                if dest and source and _under(_norm(path), _norm(dest)):
                    host = posixpath.join(source, posixpath.relpath(_norm(path), _norm(dest)))
                    locations.add(host, f"{label} (library)", "arr")
                    translated = True
        if not translated:
            locations.add(path, f"{label} (library; container path, host mapping unknown)", "arr")
