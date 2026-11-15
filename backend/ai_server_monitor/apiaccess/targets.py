"""The services the model may call, built from the settings, and the rules for a request.

A *service* is one configured instance: `radarr`, `sonarr-4k`, `prowlarr`, `qbittorrent`,
`docker`, `prometheus`. The model only ever names a service and a relative path; the base
URL, credentials and API prefix live here and never leave this module.
"""

from __future__ import annotations

import json
import logging
import os
import re
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import parse_qsl, unquote

from .errors import ApiAccessError

log = logging.getLogger(__name__)

ARR_KINDS = ("radarr", "sonarr", "whisparr")
INDEXER_KINDS = (*ARR_KINDS, "prowlarr")
KIND_LABEL = {
    "radarr": "Radarr",
    "sonarr": "Sonarr",
    "whisparr": "Whisparr",
    "prowlarr": "Prowlarr",
    "qbittorrent": "qBittorrent",
    "docker": "Docker",
    "prometheus": "Prometheus",
}
DEFAULT_DOCKER_SOCKET = "unix:///var/run/docker.sock"

METHODS = ("GET", "POST", "PUT", "DELETE", "PATCH")
WRITE_METHODS = frozenset({"POST", "PUT", "DELETE", "PATCH"})
READ_ONLY_KINDS = frozenset({"docker", "prometheus"})

# GETs that make the apps query every indexer (interactive search). They are treated like
# writes: the owner approves them, so a model cannot hammer the indexers.
_INDEXER_QUERY = re.compile(r"^/api/v\d+/(search|release)(/|$)", re.IGNORECASE)
# Never useful to a chat and (for *arr) full of credentials.
_BLOCKED = (
    re.compile(r"/initialize(\.js|\.json)?$", re.IGNORECASE),
    re.compile(r"^/api/v2/auth/", re.IGNORECASE),
)
# Credentials are added by the gateway; a model must not smuggle its own.
_CREDENTIAL_PARAMS = re.compile(r"^(api[_-]?key|apikey|token|access_token|password|auth)$", re.I)
_PATH_CHARS = re.compile(r"^[A-Za-z0-9._~!$&'()*+,;=:@%/-]*$")
MAX_PATH = 500

Query = list[tuple[str, str]]


@dataclass(frozen=True)
class Target:
    name: str
    kind: str
    base_url: str
    prefix: str
    """API prefix added to paths that do not start with /api/ (e.g. /api/v3)."""
    headers: dict[str, str] = field(default_factory=dict, repr=False)
    basic_auth: tuple[str, str] | None = field(default=None, repr=False)
    uds: str | None = None
    """Unix socket path (Docker)."""
    login: tuple[str, str] | None = field(default=None, repr=False)
    """qBittorrent WebUI user name and password (cookie login)."""

    @property
    def label(self) -> str:
        return KIND_LABEL[self.kind]

    @property
    def read_only(self) -> bool:
        return self.kind in READ_ONLY_KINDS


def build_targets(settings: Any) -> dict[str, Target]:
    """All services the settings configure, by name. Names are unique: on a clash the first
    one wins (instance names are only unique within one kind)."""
    targets: dict[str, Target] = {}

    def add(target: Target) -> None:
        if target.name in targets:
            log.warning("api access: service name %r is used twice; keeping the first", target.name)
            return
        targets[target.name] = target

    arr = settings.arr
    for kind in ARR_KINDS:
        for inst in getattr(arr, kind):
            add(
                Target(
                    inst.name,
                    kind,
                    str(inst.url).rstrip("/"),
                    "/api/v3",
                    headers={"X-Api-Key": inst.api_key.get_secret_value()},
                )
            )
    if arr.prowlarr:
        add(
            Target(
                arr.prowlarr.name,
                "prowlarr",
                str(arr.prowlarr.url).rstrip("/"),
                "/api/v1",
                headers={"X-Api-Key": arr.prowlarr.api_key.get_secret_value()},
            )
        )
    if arr.qbittorrent:
        url = str(arr.qbittorrent.url).rstrip("/")
        add(
            Target(
                "qbittorrent",
                "qbittorrent",
                url,
                "/api/v2",
                headers={"Referer": url},  # qBittorrent insists on its own origin
                login=(arr.qbittorrent.username, arr.qbittorrent.password.get_secret_value()),
            )
        )
    docker = settings.collectors.docker
    if docker.enabled is not False:
        add(_docker_target(docker.url))
    prom = settings.collectors.prometheus
    if str(prom.url):
        token = prom.token.get_secret_value()
        add(
            Target(
                "prometheus",
                "prometheus",
                str(prom.url).rstrip("/"),
                "/api/v1",
                headers={"Authorization": f"Bearer {token}"} if token else {},
                basic_auth=(
                    (prom.username, prom.password.get_secret_value())
                    if prom.username and not token
                    else None
                ),
            )
        )
    return targets


def _docker_target(url: str) -> Target:
    # Same resolution as the Docker collector: setting, else $DOCKER_HOST, else the socket.
    # TODO: the collectors also look for the socket under the host mount inside a container.
    url = url.strip() or os.environ.get("DOCKER_HOST") or DEFAULT_DOCKER_SOCKET
    if url.startswith("unix://"):
        return Target("docker", "docker", "http://docker", "", uds=url.removeprefix("unix://"))
    if url.startswith("tcp://"):
        return Target("docker", "docker", "http://" + url.removeprefix("tcp://").rstrip("/"), "")
    return Target("docker", "docker", url.rstrip("/"), "")


# --------------------------------------------------------------------------- requests


@dataclass(frozen=True)
class Prepared:
    target: Target
    method: str
    path: str
    """Full path as sent, without query string."""
    query: Query
    write_like: bool
    """Needs the owner's approval: a write, or a GET that queries every indexer."""

    @property
    def display(self) -> str:
        return f"{self.method} {self.path}"


def prepare(
    target: Target, method: str, path: str, query: dict[str, Any] | Query | None = None
) -> Prepared:
    """Validate a request against the service's rules. Raises ApiAccessError."""
    method = (method or "GET").strip().upper()
    if method not in METHODS:
        raise ApiAccessError(f"method must be one of {', '.join(METHODS)}")
    if target.read_only and method != "GET":
        raise ApiAccessError(f"{target.name} is read-only: only GET is possible")
    full, from_path = resolve_path(target, path)
    explicit = _query_pairs(query)
    given = {k for k, _ in explicit}
    merged = [*(p for p in from_path if p[0] not in given), *explicit]
    for key, _ in merged:
        if _CREDENTIAL_PARAMS.match(key):
            raise ApiAccessError(
                f"query parameter {key!r} is not allowed: credentials are added automatically"
            )
    write_like = method in WRITE_METHODS or (
        target.kind in INDEXER_KINDS and bool(_INDEXER_QUERY.match(full))
    )
    return Prepared(target, method, full, merged, write_like)


def resolve_path(target: Target, path: str) -> tuple[str, Query]:
    """(full path, query pairs found in the path). The path must stay below the service's
    base URL: no scheme or host, no `..`, nothing that could reach another host."""
    if not isinstance(path, str) or not path.strip():
        raise ApiAccessError("path is required, e.g. /api/v3/health")
    raw = path.strip()
    if len(raw) > MAX_PATH:
        raise ApiAccessError("path is too long")
    if "://" in raw or raw.startswith("//") or "\\" in raw or "#" in raw:
        raise ApiAccessError(
            "path must be relative to the service (no http://host, no //host, no #)"
        )
    if any(ord(c) < 32 or ord(c) == 127 for c in raw):
        raise ApiAccessError("path contains control characters")
    raw_path, _, raw_query = raw.partition("?")
    if "{" in raw_path or "}" in raw_path:
        raise ApiAccessError("replace the {placeholders} in the path with real values")
    if " " in raw_path:
        raise ApiAccessError("path must not contain spaces: put parameters in `query`")
    if not _PATH_CHARS.match(raw_path):
        raise ApiAccessError("path contains characters that are not allowed")
    if not raw_path.startswith("/"):
        raw_path = "/" + raw_path
    # Check the decoded form too: %2e%2e, %2f and double-encoded variants.
    decoded = raw_path
    for _ in range(3):
        decoded = unquote(decoded)
    if any(seg == ".." for seg in re.split(r"[/\\]", decoded)) or "://" in decoded:
        raise ApiAccessError("path must stay inside the service (no `..`)")
    if "//" in decoded or "\\" in decoded:
        raise ApiAccessError("path must not contain // or backslashes")
    if target.prefix and not raw_path.startswith("/api/"):
        raw_path = target.prefix + raw_path
    if any(p.search(raw_path) for p in _BLOCKED):
        raise ApiAccessError("this path is not available to the chat")
    return raw_path, parse_qsl(raw_query, keep_blank_values=True)


def _scalar(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)


def _query_pairs(query: dict[str, Any] | Query | None) -> Query:
    if not query:
        return []
    items = query.items() if isinstance(query, dict) else query
    pairs: Query = []
    for key, value in items:
        if value is None:
            continue
        if isinstance(value, list | tuple):
            pairs.extend((str(key), _scalar(v)) for v in value if v is not None)
        elif isinstance(value, dict):
            pairs.append((str(key), json.dumps(value, separators=(",", ":"))))
        else:
            pairs.append((str(key), _scalar(value)))
    return pairs
