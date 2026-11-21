"""The image check of `docker:updates` (`jobs/images.py`): the registry digest comes from the
Docker daemon's `GET /distribution/<image>/json`, asked from the backend. The daemon is a
`httpx.MockTransport`. Invented image names and addresses only."""

from __future__ import annotations

import base64
import json
from pathlib import Path
from typing import Any

import httpx
import pytest

from ai_server_monitor.jobs import images
from ai_server_monitor.jobs.images import (
    check_images,
    daemon_client,
    hub_image,
    image_check_tool,
    registry_auth,
)

ANON = "docker hub: anonymous"
USER = "invented-user"
TOKEN = "dckr_pat_inventedtoken0123"

DEFAULT_RUNNING = [
    "registry.example.invalid/team/app:1.0",
    "invented/cache:2",
    "local-build:dev",
    "invented/cache:2",
]
LOCAL_DIGESTS = {
    "registry.example.invalid/team/app:1.0": ["registry.example.invalid/team/app@sha256:aaa1"],
    "invented/cache:2": ["invented/cache@sha256:bbb1"],
    "invented/limited:1": ["invented/limited@sha256:ddd1"],
    "invented/badlogin:1": ["invented/badlogin@sha256:eee1"],
    "local-build:dev": [],
}
REGISTRY_DIGESTS = {
    "registry.example.invalid/team/app:1.0": "sha256:aaa1",
    "invented/cache:2": "sha256:ccc2",
}


def desc(digest: str) -> dict[str, Any]:
    return {
        "Descriptor": {
            "mediaType": "application/vnd.oci.image.index.v1+json",
            "digest": digest,
            "size": 10,
        },
        "Platforms": [{"architecture": "amd64", "os": "linux"}],
    }


class Daemon:
    """A fake Docker daemon; it keeps every request it got."""

    def __init__(self, running: list[str] | None = None, *, proxy_403: bool = False):
        self.running = DEFAULT_RUNNING if running is None else running
        self.proxy_403 = proxy_403
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        path = request.url.path
        if path == "/containers/json":
            return httpx.Response(200, json=[{"Image": image} for image in self.running])
        if path.startswith("/images/"):
            image = path.removeprefix("/images/").removesuffix("/json")
            if image == "invented/gone:1":
                return httpx.Response(404, json={"message": "No such image: invented/gone:1"})
            if image in LOCAL_DIGESTS:
                return httpx.Response(200, json={"RepoDigests": LOCAL_DIGESTS[image]})
            if "same" in image or "update" in image:
                return httpx.Response(200, json={"RepoDigests": ["invented/any@sha256:aaa1"]})
            return httpx.Response(200, json={"RepoDigests": []})  # own: built here
        assert path.startswith("/distribution/")
        image = path.removeprefix("/distribution/").removesuffix("/json")
        if self.proxy_403:
            return httpx.Response(
                403, text="<html><body><h1>403 Forbidden</h1>\nRequest forbidden</body></html>\n"
            )
        if image == "invented/limited:1":
            message = "toomanyrequests: retry-after: 1.5ms, allowed: 10/minute"
            return httpx.Response(429, json={"message": message})
        if image == "invented/badlogin:1":
            return httpx.Response(
                401, json={"message": "unauthorized: incorrect username or password"}
            )
        if image in REGISTRY_DIGESTS:
            return httpx.Response(200, json=desc(REGISTRY_DIGESTS[image]))
        if "same" in image:
            return httpx.Response(200, json=desc("sha256:aaa1"))
        if "update" in image:
            return httpx.Response(200, json=desc("sha256:ccc2"))
        return httpx.Response(404, json={"message": "denied: requested access is denied"})

    def distribution(self) -> list[httpx.Request]:
        return [r for r in self.requests if r.url.path.startswith("/distribution/")]

    def client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(transport=httpx.MockTransport(self), base_url="http://daemon")


async def check(daemon: Daemon, login: tuple[str, str] | None = None) -> list[str]:
    async with daemon.client() as client:
        return (await check_images(client, login)).splitlines()


async def test_classes_in_order_then_the_counts_and_the_hub_line() -> None:
    daemon = Daemon()
    assert await check(daemon) == [
        "newer    invented/cache:2 | local: invented/cache@sha256:bbb1 | registry: sha256:ccc2",
        "local    local-build:dev",
        "current  registry.example.invalid/team/app:1.0",
        "counts: 1 newer, 0 limited, 0 unknown, 1 local, 1 current",
        ANON,
    ]
    # each registry image once; the local image has no registry to ask
    assert [r.url.path for r in daemon.distribution()] == [
        "/distribution/invented/cache:2/json",
        "/distribution/registry.example.invalid/team/app:1.0/json",
    ]


async def test_a_local_image_is_its_own_class_and_no_registry_is_asked() -> None:
    """Fault 2 of the first 0.10.2 Daily: images built on the host showed as registry errors."""
    daemon = Daemon(["local-build:dev"])
    assert await check(daemon) == [
        "local    local-build:dev",
        "counts: 0 newer, 0 limited, 0 unknown, 1 local, 0 current",
        ANON,
    ]
    assert daemon.distribution() == []


async def test_the_registrys_rate_limit_is_its_own_class_with_its_answer() -> None:
    """Fault 3: `toomanyrequests` was one of the "unknown registry errors"."""
    daemon = Daemon(["invented/limited:1", "invented/cache:2", "invented/gone:1"])
    assert await check(daemon) == [
        "newer    invented/cache:2 | local: invented/cache@sha256:bbb1 | registry: sha256:ccc2",
        "limited  invented/limited:1: toomanyrequests: retry-after: 1.5ms, allowed: 10/minute",
        "unknown  invented/gone:1: No such image: invented/gone:1",
        "counts: 1 newer, 1 limited, 1 unknown, 0 local, 0 current",
        ANON,
    ]


async def test_a_proxy_without_distribution_gives_unknown_with_its_answer() -> None:
    lines = await check(Daemon(proxy_403=True))
    images = ("invented/cache:2", "registry.example.invalid/team/app:1.0")
    assert lines == [
        *(f"unknown  {image}: <html><body><h1>403 Forbidden</h1>" for image in images),
        "local    local-build:dev",
        "counts: 0 newer, 0 limited, 2 unknown, 1 local, 0 current",
        ANON,
    ]


async def test_an_unreachable_registry_lookup_is_unknown_with_the_error() -> None:
    def refuse(request: httpx.Request) -> httpx.Response:
        if request.url.path.startswith("/distribution/"):
            raise httpx.ConnectError("connection refused")
        return Daemon(["invented/cache:2"])(request)

    client = httpx.AsyncClient(transport=httpx.MockTransport(refuse), base_url="http://daemon")
    async with client:
        lines = (await check_images(client, None)).splitlines()
    assert lines[0] == "unknown  invented/cache:2: ConnectError: connection refused"
    assert lines[1] == "counts: 0 newer, 0 limited, 1 unknown, 0 local, 0 current"


async def test_newer_then_the_not_checked_then_local_then_current_and_the_counts_last() -> None:
    """Many images of mixed result: the lines the model must act on come first, whatever the
    image names, and it copies the counts instead of counting the lines."""
    names = [f"invented/{kind}-{n:02}:1" for n in range(14) for kind in ("same", "update", "own")]
    lines = await check(Daemon(names))
    assert lines.pop() == ANON
    kinds = [line.split()[0] for line in lines[:-1]]
    assert kinds == ["newer"] * 14 + ["local"] * 14 + ["current"] * 14
    assert lines[0].startswith("newer    invented/update-00:1 | local: invented/any@sha256:aaa1")
    assert lines[14] == "local    invented/own-00:1"
    assert lines[28] == "current  invented/same-00:1"
    assert lines[-1] == "counts: 14 newer, 0 limited, 0 unknown, 14 local, 14 current"


async def test_no_running_container_still_prints_the_counts() -> None:
    assert await check(Daemon([])) == [
        "counts: 0 newer, 0 limited, 0 unknown, 0 local, 0 current",
        ANON,
    ]


@pytest.mark.parametrize(
    ("image", "hub"),
    [
        ("invented/same-1:1", True),  # user/repo
        ("same-2:1", True),  # an official image: no slash
        ("docker.io/invented/same-3:1", True),  # the registry host named
        ("index.docker.io/invented/same-4:1", True),
        ("registry.example.invalid/team/same-5:1", False),  # a host with a dot
        ("192.0.2.5:5000/team/same-6:1", False),  # a host with a port
        ("localhost/same-7:1", False),
        ("localhost:5000/same-8:1", False),
    ],
)
def test_which_images_are_docker_hub_images(image: str, hub: bool) -> None:
    assert hub_image(image) is hub


HEADER = base64.urlsafe_b64encode(
    json.dumps({"username": USER, "password": TOKEN}, separators=(",", ":")).encode()
).decode()


async def test_with_a_login_only_docker_hub_images_are_asked_with_it() -> None:
    names = [
        "invented/same-1:1",
        "same-2:1",
        "docker.io/invented/same-3:1",
        "registry.example.invalid/team/same-5:1",
        "192.0.2.5:5000/team/same-6:1",
        "localhost/same-7:1",
    ]
    daemon = Daemon(names)
    lines = await check(daemon, (USER, TOKEN))
    assert lines[-1] == f"docker hub: logged in as {USER}"
    assert lines[-2] == "counts: 0 newer, 0 limited, 0 unknown, 0 local, 6 current"
    assert len(daemon.distribution()) == 6
    for request in daemon.distribution():
        image = request.url.path.removeprefix("/distribution/").removesuffix("/json")
        assert request.headers.get("x-registry-auth") == (HEADER if hub_image(image) else None)
    # the login is in the headers only: nothing the check prints holds it, in clear or encoded
    printed = "\n".join(lines)
    for secret in (TOKEN, HEADER):
        assert secret not in printed
    # the daemon is asked for the container list and the local digests without it
    assert all(
        "x-registry-auth" not in r.headers
        for r in daemon.requests
        if r not in daemon.distribution()
    )


async def test_a_refused_login_is_unknown_with_the_registrys_answer() -> None:
    """The daemon relays Docker Hub's answer, which names the cause."""
    lines = await check(Daemon(["invented/badlogin:1"]), (USER, TOKEN))
    assert lines[0] == "unknown  invented/badlogin:1: unauthorized: incorrect username or password"
    assert lines[-1] == f"docker hub: logged in as {USER}"


@pytest.mark.parametrize(
    "login",
    [
        {"username": 'a"b', "password": "p\\q"},  # characters a printf-built header broke on
        {"username": USER, "password": TOKEN},
    ],
)
def test_the_header_is_json_in_base64url_with_any_character_in_the_login(
    login: dict[str, str],
) -> None:
    header = registry_auth(login["username"], login["password"])
    assert json.loads(base64.urlsafe_b64decode(header)) == login
    assert "+" not in header and "/" not in header


async def test_the_tool_scrubs_its_result_and_adds_it_to_the_evidence_sources() -> None:
    sources: list[str] = []
    daemon = Daemon(["invented/cache:2"])
    tool = image_check_tool(
        daemon.client, None, lambda text: text.replace("sha256:ccc2", "[redacted]"), sources
    )
    assert tool.name == "docker_image_check" and tool.parameters["properties"] == {}
    text = await tool.handler({})
    assert "[redacted]" in text and "sha256:ccc2" not in text
    assert sources == [text]


async def test_the_tool_says_when_the_daemon_cannot_be_reached() -> None:
    def down(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("no route")

    def client() -> httpx.AsyncClient:
        return httpx.AsyncClient(transport=httpx.MockTransport(down), base_url="http://daemon")

    sources: list[str] = []
    text = await image_check_tool(client, None, str, sources).handler({})
    assert text == "could not reach the Docker API: ConnectError: no route"
    assert sources == [text]


def test_a_tcp_address_is_asked_as_http() -> None:
    client = daemon_client("tcp://192.0.2.10:2375", Path("/"), False)
    assert str(client.base_url) == "http://192.0.2.10:2375"


@pytest.mark.parametrize(
    ("url", "isolated", "socket"),
    [
        ("unix:///run/invented/docker.sock", False, "/run/invented/docker.sock"),
        (None, False, "/var/run/docker.sock"),  # unset: Docker's own default socket
        ("", False, "/var/run/docker.sock"),
        # chrooted shell: the socket path is the one under the host root, as the shell sees it
        ("unix:///run/invented/docker.sock", True, "/host/run/invented/docker.sock"),
    ],
)
def test_a_unix_address_goes_through_the_socket(
    monkeypatch: pytest.MonkeyPatch, url: str | None, isolated: bool, socket: str
) -> None:
    seen: list[str] = []

    def transport(**kwargs: Any) -> httpx.MockTransport:
        seen.append(kwargs["uds"])
        return httpx.MockTransport(lambda request: httpx.Response(200))

    monkeypatch.setattr(images.httpx, "AsyncHTTPTransport", transport)
    client = daemon_client(url, Path("/host"), isolated)
    assert seen == [str(Path(socket))]
    assert str(client.base_url) == "http://localhost"
