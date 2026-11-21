"""The image check of the `docker:updates` job: the tool `docker_image_check`.

It runs in the backend, not in the model's shell, because of the optional Docker Hub login
(Settings → General): the user name and token are sent to the Docker daemon from here, as the
header `X-Registry-Auth` of its Engine API request, so neither the shell's environment nor the
arguments of any process ever hold them. The model gets only the result lines.

HTTP through httpx (already a dependency) rather than the Docker SDK: one GET endpoint, async,
no extra packages, and the daemon's answer reaches the model as the daemon words it."""

from __future__ import annotations

import base64
import json
from collections.abc import Callable
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import httpx

from ..agent.runtime import AgentTool

DEFAULT_SOCKET = "/var/run/docker.sock"
HUB_HOSTS = ("docker.io", "index.docker.io", "registry-1.docker.io")
CLASSES = ("newer", "limited", "unknown", "local", "current")

DESCRIPTION = (
    "Check every image of a running container against its registry (read-only). Returns one "
    "line per image, grouped by the word each line starts with (newer, limited, unknown, "
    "local, current), then a line with the counts and a line saying whether Docker Hub was "
    "asked with a login. Takes no arguments."
)


def hub_image(image: str) -> bool:
    """A Docker Hub image: no registry host in its name, or one of Docker Hub's own."""
    host, slash, _ = image.partition("/")
    if not slash:
        return True
    if host in HUB_HOSTS:
        return True
    return not ("." in host or ":" in host or host == "localhost")


def registry_auth(user: str, token: str) -> str:
    """The value of `X-Registry-Auth`: base64url of the JSON login, as the Docker CLI hands
    credentials to the daemon."""
    login = json.dumps({"username": user, "password": token}, separators=(",", ":"))
    return base64.urlsafe_b64encode(login.encode()).decode()


def daemon_client(url: str | None, root: Path, isolated: bool) -> httpx.AsyncClient:
    """A client for the Docker Engine API at the address the `docker` commands use: `tcp://`
    as http, `unix://` through the socket, none as Docker's default socket. When the shell is
    chrooted (isolated), a socket path means the path under the host root.

    No timeout of its own: the agent's tool time limit stops a call that hangs."""
    address = (url or "").strip() or f"unix://{DEFAULT_SOCKET}"
    parts = urlsplit(address)
    if parts.scheme in ("unix", ""):
        path = Path(parts.path or address)
        if isolated:
            path = root / str(path).lstrip("/")
        return httpx.AsyncClient(
            transport=httpx.AsyncHTTPTransport(uds=str(path)),
            base_url="http://localhost",
            timeout=None,
        )
    scheme = "https" if parts.scheme == "https" else "http"
    return httpx.AsyncClient(base_url=f"{scheme}://{parts.netloc}", timeout=None)


def answer_text(response: httpx.Response) -> str:
    """What the daemon (or a proxy in front of it) said: its `message`, else the first line."""
    try:
        message = response.json().get("message")
    except (ValueError, AttributeError):
        message = None
    if isinstance(message, str) and message.strip():
        return message.strip().splitlines()[0]
    lines = response.text.strip().splitlines()
    return lines[0] if lines else f"HTTP {response.status_code}"


async def check_images(client: httpx.AsyncClient, login: tuple[str, str] | None) -> str:
    """The tool's result text: one line per image of a running container, by class, then the
    counts, then "docker hub: anonymous" or "docker hub: logged in as <user>".

    Classes: newer (the registry has another digest for the tag; both are on the line), limited
    (the registry answered `toomanyrequests`), unknown (any other failure, with the answer),
    local (the daemon knows no registry digest: built or loaded on this host, no registry is
    asked) and current."""
    lines: dict[str, list[str]] = {name: [] for name in CLASSES}
    header = registry_auth(*login) if login else None
    containers = await client.get("/containers/json")
    if containers.status_code != 200:
        return f"could not list the containers: {answer_text(containers)}"
    images = sorted({str(c.get("Image")) for c in containers.json() if c.get("Image")})
    for image in images:
        local = await client.get(f"/images/{image}/json")
        if local.status_code != 200:
            lines["unknown"].append(f"unknown  {image}: {answer_text(local)}")
            continue
        digests = " ".join(local.json().get("RepoDigests") or [])
        if not digests:
            lines["local"].append(f"local    {image}")
            continue
        headers = {"X-Registry-Auth": header} if header and hub_image(image) else {}
        try:
            reply = await client.get(f"/distribution/{image}/json", headers=headers)
        except httpx.HTTPError as exc:
            lines["unknown"].append(f"unknown  {image}: {_failure(exc)}")
            continue
        digest = _digest(reply) if reply.status_code == 200 else None
        if digest is None:
            kind = "limited" if "toomanyrequests" in reply.text else "unknown"
            lines[kind].append(f"{kind:<8} {image}: {answer_text(reply)}")
        elif digest in digests:
            lines["current"].append(f"current  {image}")
        else:
            lines["newer"].append(f"newer    {image} | local: {digests} | registry: {digest}")
    counts = ", ".join(f"{len(lines[name])} {name}" for name in CLASSES)
    hub = f"logged in as {login[0]}" if login else "anonymous"
    body = [line for name in CLASSES for line in lines[name]]
    return "\n".join([*body, f"counts: {counts}", f"docker hub: {hub}"])


def _failure(exc: Exception) -> str:
    return f"{type(exc).__name__}: {exc}" if str(exc) else type(exc).__name__


def _digest(reply: httpx.Response) -> str | None:
    try:
        digest = reply.json()["Descriptor"]["digest"]
    except (ValueError, KeyError, TypeError):
        return None
    return digest if isinstance(digest, str) and digest.startswith("sha256:") else None


def image_check_tool(
    client_for: Callable[[], httpx.AsyncClient],
    login: tuple[str, str] | None,
    scrub: Callable[[str], str],
    sources: list[str],
) -> AgentTool:
    """`docker_image_check`. Its result is also appended to `sources`, the run's evidence pool
    (what the model quotes must occur in it), and scrubbed first, like every tool result."""

    async def handler(args: dict[str, Any]) -> str:
        async with client_for() as client:
            try:
                text = await check_images(client, login)
            except httpx.HTTPError as exc:
                text = f"could not reach the Docker API: {_failure(exc)}"
        text = scrub(text)
        sources.append(text)
        return text

    return AgentTool(
        name="docker_image_check",
        description=DESCRIPTION,
        parameters={"type": "object", "properties": {}},
        handler=handler,
    )
