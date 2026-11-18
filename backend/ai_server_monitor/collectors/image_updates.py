"""Container image updates: is the image behind each running container still current?

For every `repo:tag` in use, the digest the registry serves for that tag today
(`HEAD /v2/<repo>/manifests/<tag>`) is compared with the digests the local image was pulled
as (`RepoDigests`). Both sides are the digest of the *top-level* manifest — the multi-arch
index for multi-platform images — so they are directly comparable.

Strictly read-only: Docker API GETs and registry HEADs/GETs. Only anonymous access to
registries (no credentials), so private images are reported as "auth required".
This finds new builds of the *same tag* (latest, 1.27, ...), not newer version numbers.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import os
import re
import time
from dataclasses import dataclass
from datetime import timedelta
from typing import Any

import httpx

from ..config import ImageUpdatesCollectorSettings
from .base import Collector, HostEnv
from .docker import DEFAULT_SOCKET, DockerAPI

log = logging.getLogger(__name__)

UP_TO_DATE = "up_to_date"
UPDATE_AVAILABLE = "update_available"
UNKNOWN = "unknown"

DOCKER_HUB = "docker.io"
DOCKER_HUB_API = "registry-1.docker.io"
MANIFEST_ACCEPT = ", ".join(
    (
        "application/vnd.oci.image.index.v1+json",
        "application/vnd.docker.distribution.manifest.list.v2+json",
        "application/vnd.oci.image.manifest.v1+json",
        "application/vnd.docker.distribution.manifest.v2+json",
    )
)
_CONCURRENCY = 4
_REQUEST_TIMEOUT = 15.0
_MAX_RETRIES = 2
_MAX_WAIT = 30.0
_DEFAULT_TOKEN_TTL = 60.0
_IMAGE_ID = re.compile(r"^(sha256:)?[0-9a-f]{12,64}$")
_DIGEST = re.compile(r"^sha256:[0-9a-f]{64}$")
_AUTH_PARAM = re.compile(r'(\w+)="([^"]*)"')


# --------------------------------------------------------------------------- references


@dataclass(frozen=True)
class ImageRef:
    registry: str  # canonical: docker.io, ghcr.io, lscr.io, localhost:5000
    repo: str  # library/nginx, linuxserver/nginx
    tag: str | None  # None when pinned by digest only
    digest: str | None

    @property
    def api_host(self) -> str:
        return DOCKER_HUB_API if self.registry == DOCKER_HUB else self.registry


def parse_ref(ref: str) -> ImageRef:
    """Parse a Docker image reference the way the Docker CLI does."""
    rest = ref.strip()
    digest = None
    if "@" in rest:
        rest, digest = rest.split("@", 1)
    first, _, remainder = rest.partition("/")
    if remainder and ("." in first or ":" in first or first == "localhost"):
        registry, path = first.lower(), remainder
    else:
        registry, path = DOCKER_HUB, rest
    if registry in ("index.docker.io", "registry-1.docker.io", "registry.hub.docker.com"):
        registry = DOCKER_HUB
    tag = None
    last = path.rsplit("/", 1)[-1]
    if ":" in last:
        path, tag = path.rsplit(":", 1)
    if registry == DOCKER_HUB and "/" not in path:
        path = f"library/{path}"
    if not path:
        raise ValueError(f"invalid image reference {ref!r}")
    if tag is None and digest is None:
        tag = "latest"
    return ImageRef(registry, path, tag, digest)


# --------------------------------------------------------------------------- registry


@dataclass
class Outcome:
    """What the registry said about one tag."""

    digest: str | None = None
    reason: str | None = None  # set when digest is None
    cacheable: bool = True  # False for transient failures (network, 5xx, rate limit)


class RegistryClient:
    """Anonymous manifest-digest lookups with Bearer token handling, caching and backoff."""

    def __init__(
        self,
        *,
        cache_ttl: float,
        transport: httpx.AsyncBaseTransport | None = None,
    ):
        self.cache_ttl = cache_ttl
        self._http = httpx.AsyncClient(
            transport=transport,
            timeout=httpx.Timeout(_REQUEST_TIMEOUT, connect=8.0),
            follow_redirects=True,
            headers={"User-Agent": "ai-server-monitor"},
        )
        self._sem = asyncio.Semaphore(_CONCURRENCY)
        self._cache: dict[tuple[str, str, str], tuple[float, Outcome]] = {}
        self._tokens: dict[tuple[str, str], tuple[float, str]] = {}
        self._blocked_until: dict[str, float] = {}  # registry host -> monotonic time

    async def close(self) -> None:
        await self._http.aclose()

    async def digest(self, ref: ImageRef) -> tuple[Outcome, bool]:
        """(outcome, from_cache) for the digest the registry serves for `ref.tag` now."""
        assert ref.tag is not None
        key = (ref.registry, ref.repo, ref.tag)
        hit = self._cache.get(key)
        if hit and time.monotonic() - hit[0] < self.cache_ttl:
            return hit[1], True
        async with self._sem:
            outcome = await self._lookup(ref)
        if outcome.cacheable:
            self._cache[key] = (time.monotonic(), outcome)
        return outcome, False

    async def _lookup(self, ref: ImageRef) -> Outcome:
        try:
            return await self._lookup_unsafe(ref)
        except (httpx.HTTPError, ValueError) as exc:
            reason = f"registry unreachable: {type(exc).__name__}: {exc}".rstrip(": ")
            return Outcome(reason=reason[:300], cacheable=False)

    async def _lookup_unsafe(self, ref: ImageRef) -> Outcome:
        host = ref.api_host
        url = f"https://{host}/v2/{ref.repo}/manifests/{ref.tag}"
        scope = f"repository:{ref.repo}:pull"
        token = self._token_for(host, scope)
        response = await self._request("HEAD", url, host, token)
        if response.status_code == 401:
            fresh = await self._authenticate(response, host, scope)
            if isinstance(fresh, Outcome):
                return fresh
            token = fresh
            response = await self._request("HEAD", url, host, token)
        code = response.status_code
        if code == 200:
            digest = response.headers.get("Docker-Content-Digest")
            if not digest or not _DIGEST.match(digest):
                digest = await self._digest_from_body(url, host, token)
            return Outcome(digest) if digest else Outcome(reason="registry sent no digest")
        if code in (401, 403):
            return Outcome(reason="auth required (private image or no anonymous access)")
        if code == 404:
            return Outcome(reason="tag not found in the registry (private, renamed or removed)")
        if code == 429:
            return Outcome(reason="rate limited by the registry", cacheable=False)
        return Outcome(reason=f"registry answered HTTP {code}", cacheable=code < 500)

    async def _digest_from_body(self, url: str, host: str, token: str | None) -> str | None:
        """Fallback for registries that omit Docker-Content-Digest on HEAD."""
        response = await self._request("GET", url, host, token)
        if response.status_code != 200:
            return None
        header = response.headers.get("Docker-Content-Digest")
        if header and _DIGEST.match(header):
            return header
        return "sha256:" + hashlib.sha256(response.content).hexdigest()

    def _token_for(self, host: str, scope: str) -> str | None:
        entry = self._tokens.get((host, scope))
        if entry and entry[0] > time.monotonic():
            return entry[1]
        return None

    async def _authenticate(
        self, challenge: httpx.Response, host: str, scope: str
    ) -> str | Outcome:
        header = challenge.headers.get("WWW-Authenticate", "")
        scheme, _, params_text = header.partition(" ")
        if scheme.lower() != "bearer":
            return Outcome(reason="auth required (registry wants credentials)")
        params = dict(_AUTH_PARAM.findall(params_text))
        realm = params.get("realm")
        if not realm or not realm.startswith("https://"):
            return Outcome(reason="registry sent an unusable auth challenge")
        query = {"scope": scope}
        if params.get("service"):
            query["service"] = params["service"]
        response = await self._request("GET", realm, host, None, params=query)
        if response.status_code in (401, 403):
            return Outcome(reason="auth required (private image or no anonymous access)")
        if response.status_code != 200:
            return Outcome(
                reason=f"token endpoint answered HTTP {response.status_code}",
                cacheable=response.status_code < 500 and response.status_code != 429,
            )
        body = response.json()
        token = body.get("token") or body.get("access_token")
        if not token:
            return Outcome(reason="token endpoint sent no token")
        ttl = float(body.get("expires_in") or _DEFAULT_TOKEN_TTL)
        self._tokens[(host, scope)] = (time.monotonic() + max(ttl - 10, 5), token)
        return str(token)

    async def _request(
        self,
        method: str,
        url: str,
        host: str,
        token: str | None,
        params: dict[str, str] | None = None,
    ) -> httpx.Response:
        """One request, waiting out and retrying 429s (shared per-registry backoff)."""
        headers = {"Accept": MANIFEST_ACCEPT}
        if token:
            headers["Authorization"] = f"Bearer {token}"
        for attempt in range(_MAX_RETRIES + 1):
            wait = self._blocked_until.get(host, 0.0) - time.monotonic()
            if wait > 0:
                await asyncio.sleep(min(wait, _MAX_WAIT))
            response = await self._http.request(method, url, headers=headers, params=params)
            if response.status_code != 429 or attempt == _MAX_RETRIES:
                return response
            delay = _retry_after(response, default=2.0 * 2**attempt)
            log.info("registry %s rate-limited us; backing off %.0fs", host, delay)
            self._blocked_until[host] = max(
                self._blocked_until.get(host, 0.0), time.monotonic() + delay
            )
        raise AssertionError("unreachable")  # pragma: no cover


def _retry_after(response: httpx.Response, *, default: float) -> float:
    try:
        return min(max(float(response.headers["Retry-After"]), 1.0), _MAX_WAIT)
    except (KeyError, ValueError):
        return min(default, _MAX_WAIT)


# --------------------------------------------------------------------------- collector


def local_digests(repo_digests: list[str], ref: ImageRef) -> list[str]:
    """Digests this image was pulled as from `ref`'s repository."""
    out = []
    for entry in repo_digests:
        try:
            parsed = parse_ref(entry)
        except ValueError:
            continue
        if (
            parsed.registry == ref.registry
            and parsed.repo == ref.repo
            and parsed.digest is not None
        ):
            out.append(parsed.digest)
    return out


def classify(local: list[str], remote: str) -> str:
    return UP_TO_DATE if remote in local else UPDATE_AVAILABLE


class ImageUpdatesCollector(Collector):
    name = "image_updates"
    timeout = timedelta(minutes=5)
    cfg: ImageUpdatesCollectorSettings

    def __init__(
        self,
        cfg: ImageUpdatesCollectorSettings,
        env: HostEnv,
        *,
        docker_transport: httpx.AsyncBaseTransport | None = None,
        registry_transport: httpx.AsyncBaseTransport | None = None,
    ):
        super().__init__(cfg, env)
        url = cfg.url or os.environ.get("DOCKER_HOST") or DEFAULT_SOCKET
        if url.startswith("unix://") and docker_transport is None:
            path = url.removeprefix("unix://")
            if not os.path.exists(path) and env.in_container:
                alt = env.path(path)
                if alt.exists():
                    url = f"unix://{alt}"
        self.api = DockerAPI(url, transport=docker_transport)
        self.registry = RegistryClient(
            cache_ttl=cfg.cache_ttl.total_seconds(), transport=registry_transport
        )

    async def check(self) -> None:
        await self.api.get("/_ping")

    async def close(self) -> None:
        await self.api.close()
        await self.registry.close()

    async def collect(self) -> dict[str, Any]:
        listing = await self.api.get("/containers/json")  # running containers only
        groups: dict[tuple[str, str], dict[str, Any]] = {}
        for entry in listing:
            image = str(entry.get("Image") or "")
            image_id = str(entry.get("ImageID") or "")
            name = (entry.get("Names") or ["/" + str(entry.get("Id", ""))[:12]])[0].lstrip("/")
            group = groups.setdefault(
                (image, image_id), {"image": image, "image_id": image_id, "containers": []}
            )
            group["containers"].append(name)
        images = await asyncio.gather(*(self._check_group(g) for g in groups.values()))
        images.sort(key=lambda i: (_ORDER[i["status"]], i["image"]))
        counts = {
            s: sum(i["status"] == s for i in images)
            for s in (UP_TO_DATE, UPDATE_AVAILABLE, UNKNOWN)
        }
        return {
            "summary": {"total": len(images), **counts},
            "updates_available": [i["image"] for i in images if i["status"] == UPDATE_AVAILABLE],
            "images": images,
        }

    async def _check_group(self, group: dict[str, Any]) -> dict[str, Any]:
        row: dict[str, Any] = {
            "image": group["image"],
            "containers": sorted(group["containers"]),
            "image_id": group["image_id"].removeprefix("sha256:")[:12],
            "status": UNKNOWN,
            "reason": None,
        }
        try:
            await self._verdict(group, row)
        except (httpx.HTTPError, PermissionError) as exc:
            row["reason"] = f"cannot inspect the image: {type(exc).__name__}: {exc}"[:300]
        return row

    async def _verdict(self, group: dict[str, Any], row: dict[str, Any]) -> None:
        image = group["image"]
        if _IMAGE_ID.match(image):
            # The tag was moved to a newer image (docker pull) after this container started.
            row["reason"] = (
                "container runs an image its tag no longer points to; "
                "recreate the container to use the newer one"
            )
            return
        try:
            ref = parse_ref(image)
        except ValueError:
            row["reason"] = "cannot parse the image reference"
            return
        if ref.digest:
            row["reason"] = "pinned to a digest (not checked)"
            return
        try:
            inspect = await self.api.get(f"/images/{group['image_id']}/json")
        except httpx.HTTPStatusError as exc:
            row["reason"] = f"cannot inspect the image: HTTP {exc.response.status_code}"
            return
        local = local_digests(inspect.get("RepoDigests") or [], ref)
        if not local:
            row["reason"] = (
                "no registry digest recorded for this image (built or loaded locally)"
                if not inspect.get("RepoDigests")
                else "image digests belong to a different repository than its tag"
            )
            return
        outcome, cached = await self.registry.digest(ref)
        row["registry"] = ref.registry
        row["local_digests"] = local
        row["cached"] = cached
        if outcome.digest is None:
            row["reason"] = outcome.reason
            return
        row["remote_digest"] = outcome.digest
        row["status"] = classify(local, outcome.digest)

    def persist_view(self, data: dict[str, Any]) -> dict[str, Any]:
        keep = ("image", "containers", "status", "reason")
        return {**data, "images": [{k: i.get(k) for k in keep} for i in data.get("images", [])]}


_ORDER = {UPDATE_AVAILABLE: 0, UNKNOWN: 1, UP_TO_DATE: 2}
