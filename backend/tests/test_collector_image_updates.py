from __future__ import annotations

import hashlib
from typing import Any

import httpx
import pytest

from ai_server_monitor.collectors import image_updates as mod
from ai_server_monitor.collectors.base import HostEnv
from ai_server_monitor.collectors.image_updates import (
    ImageRef,
    ImageUpdatesCollector,
    parse_ref,
)
from ai_server_monitor.config import ImageUpdatesCollectorSettings

D_OLD = "sha256:" + "1" * 64
D_NEW = "sha256:" + "2" * 64
ID_A = "sha256:" + "a" * 64


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("nginx", ImageRef("docker.io", "library/nginx", "latest", None)),
        ("nginx:1.27", ImageRef("docker.io", "library/nginx", "1.27", None)),
        ("linuxserver/nginx:latest", ImageRef("docker.io", "linuxserver/nginx", "latest", None)),
        ("ghcr.io/owner/app:v1", ImageRef("ghcr.io", "owner/app", "v1", None)),
        (
            "lscr.io/linuxserver/nginx-proxy",
            ImageRef("lscr.io", "linuxserver/nginx-proxy", "latest", None),
        ),
        ("localhost:5000/app:dev", ImageRef("localhost:5000", "app", "dev", None)),
        ("docker.io/library/redis:7", ImageRef("docker.io", "library/redis", "7", None)),
        ("index.docker.io/foo/bar:1", ImageRef("docker.io", "foo/bar", "1", None)),
        ("nginx@" + D_OLD, ImageRef("docker.io", "library/nginx", None, D_OLD)),
        ("quay.io/x/y:1@" + D_OLD, ImageRef("quay.io", "x/y", "1", D_OLD)),
    ],
)
def test_parse_ref(text: str, expected: ImageRef) -> None:
    assert parse_ref(text) == expected


class FakeDocker:
    def __init__(self, containers: list[dict[str, Any]], images: dict[str, dict[str, Any]]):
        self.containers = containers
        self.images = images

    def handler(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        assert request.method == "GET"
        if path == "/_ping":
            return httpx.Response(200, text="OK")
        if path == "/containers/json":
            return httpx.Response(200, json=self.containers)
        if path.startswith("/images/") and path.endswith("/json"):
            image = self.images.get(path.split("/")[2])
            if image is None:
                return httpx.Response(404, json={"message": "no such image"})
            return httpx.Response(200, json=image)
        return httpx.Response(404)


def container(name: str, image: str, image_id: str = ID_A) -> dict[str, Any]:
    return {"Id": name * 10, "Names": [f"/{name}"], "Image": image, "ImageID": image_id}


class FakeRegistry:
    """Docker-Hub-like: anonymous Bearer tokens, per-repo digest table."""

    def __init__(self, digests: dict[str, str]):
        self.digests = digests  # "repo:tag" -> digest
        self.requests: list[httpx.Request] = []
        self.rate_limit_first = 0
        self.omit_digest_header = False
        self.private: set[str] = set()

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        url = request.url
        if url.host == "auth.test":
            scope = url.params["scope"]
            assert scope.startswith("repository:") and scope.endswith(":pull")
            repo = scope.split(":")[1]
            if repo in self.private:
                return httpx.Response(401, json={"errors": [{"code": "UNAUTHORIZED"}]})
            return httpx.Response(200, json={"token": f"tok-{repo}", "expires_in": 300})
        prefix, _, rest = url.path.partition("/manifests/")
        repo = prefix.removeprefix("/v2/")
        key = f"{repo}:{rest}"
        if request.headers.get("Authorization") != f"Bearer tok-{repo}":
            challenge = (
                'Bearer realm="https://auth.test/token",service="registry.test",'
                f'scope="repository:{repo}:pull"'
            )
            return httpx.Response(401, headers={"WWW-Authenticate": challenge})
        if self.rate_limit_first > 0:
            self.rate_limit_first -= 1
            return httpx.Response(429, headers={"Retry-After": "3"})
        if key not in self.digests:
            return httpx.Response(404)
        digest = self.digests[key]
        accept = request.headers["Accept"]
        assert "application/vnd.oci.image.index.v1+json" in accept
        assert "application/vnd.docker.distribution.manifest.v2+json" in accept
        headers = {} if self.omit_digest_header else {"Docker-Content-Digest": digest}
        if request.method == "HEAD":
            return httpx.Response(200, headers=headers)
        return httpx.Response(200, headers=headers, content=b'{"manifest": true}')


@pytest.fixture
def sleeps(monkeypatch: pytest.MonkeyPatch) -> list[float]:
    recorded: list[float] = []

    async def fake_sleep(seconds: float) -> None:
        recorded.append(seconds)

    monkeypatch.setattr(mod.asyncio, "sleep", fake_sleep)
    return recorded


def make(docker: FakeDocker, registry: FakeRegistry, **cfg: Any) -> ImageUpdatesCollector:
    return ImageUpdatesCollector(
        ImageUpdatesCollectorSettings(url="tcp://docker.test:2375", **cfg),
        HostEnv(),
        docker_transport=httpx.MockTransport(docker.handler),
        registry_transport=httpx.MockTransport(registry.handler),
    )


def by_image(data: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {i["image"]: i for i in data["images"]}


async def test_verdicts_for_up_to_date_update_and_hub_normalization() -> None:
    docker = FakeDocker(
        [
            container("web", "nginx:1.27", "sha256:" + "b" * 64),
            container("web2", "nginx:1.27", "sha256:" + "b" * 64),
            container("nginx", "lscr.io/linuxserver/nginx:latest", "sha256:" + "c" * 64),
        ],
        {
            "sha256:" + "b" * 64: {"RepoDigests": [f"nginx@{D_OLD}"]},
            "sha256:" + "c" * 64: {"RepoDigests": [f"lscr.io/linuxserver/nginx@{D_OLD}"]},
        },
    )
    registry = FakeRegistry({"library/nginx:1.27": D_OLD, "linuxserver/nginx:latest": D_NEW})
    collector = make(docker, registry)
    data = await collector.collect()
    await collector.close()
    images = by_image(data)
    nginx = images["nginx:1.27"]
    assert nginx["status"] == "up_to_date" and nginx["containers"] == ["web", "web2"]
    assert nginx["remote_digest"] == D_OLD
    nginx = images["lscr.io/linuxserver/nginx:latest"]
    assert nginx["status"] == "update_available" and nginx["remote_digest"] == D_NEW
    assert data["summary"] == {"total": 2, "up_to_date": 1, "update_available": 1, "unknown": 0}
    assert data["updates_available"] == ["lscr.io/linuxserver/nginx:latest"]
    assert data["images"][0]["status"] == "update_available"  # updates sort first
    # Hub name normalized to library/..., registry hosts are the real API hosts.
    hosts = {r.url.host for r in registry.requests}
    assert "registry-1.docker.io" in hosts and "lscr.io" in hosts


async def test_registry_answers_are_cached_between_runs() -> None:
    docker = FakeDocker(
        [container("web", "nginx:1.27")], {ID_A: {"RepoDigests": [f"nginx@{D_OLD}"]}}
    )
    registry = FakeRegistry({"library/nginx:1.27": D_OLD})
    collector = make(docker, registry)
    first = by_image(await collector.collect())["nginx:1.27"]
    count = len(registry.requests)
    second = by_image(await collector.collect())["nginx:1.27"]
    await collector.close()
    assert first["cached"] is False and second["cached"] is True
    assert len(registry.requests) == count  # no registry traffic at all the second time
    assert second["status"] == "up_to_date"


async def test_cache_expires() -> None:
    docker = FakeDocker([container("web", "nginx:1")], {ID_A: {"RepoDigests": [f"nginx@{D_OLD}"]}})
    registry = FakeRegistry({"library/nginx:1": D_OLD})
    collector = make(docker, registry, cache_ttl="0s")
    await collector.collect()
    count = len(registry.requests)
    await collector.collect()
    await collector.close()
    assert len(registry.requests) > count


async def test_unknown_reasons() -> None:
    docker = FakeDocker(
        [
            container("pinned", f"nginx@{D_OLD}", "sha256:" + "1" * 64),
            container("moved", "sha256:" + "d" * 64, "sha256:" + "d" * 64),
            container("built", "myapp:dev", "sha256:" + "2" * 64),
            container("mirror", "nginx:alpine", "sha256:" + "3" * 64),
            container("private", "ghcr.io/me/secret:latest", "sha256:" + "4" * 64),
            container("gone", "ghcr.io/me/removed:latest", "sha256:" + "5" * 64),
            container("noimage", "foo:1", "sha256:" + "6" * 64),
        ],
        {
            "sha256:" + "2" * 64: {"RepoDigests": []},
            "sha256:" + "3" * 64: {"RepoDigests": [f"other.io/mirror/nginx@{D_OLD}"]},
            "sha256:" + "4" * 64: {"RepoDigests": [f"ghcr.io/me/secret@{D_OLD}"]},
            "sha256:" + "5" * 64: {"RepoDigests": [f"ghcr.io/me/removed@{D_OLD}"]},
        },
    )
    registry = FakeRegistry({})
    registry.private.add("me/secret")
    collector = make(docker, registry)
    images = by_image(await collector.collect())
    await collector.close()
    assert all(i["status"] == "unknown" for i in images.values())
    reasons = {name: i["reason"] for name, i in images.items()}
    assert "pinned" in reasons[f"nginx@{D_OLD}"]
    assert "recreate" in reasons["sha256:" + "d" * 64]
    assert "built or loaded locally" in reasons["myapp:dev"]
    assert "different repository" in reasons["nginx:alpine"]
    assert "auth required" in reasons["ghcr.io/me/secret:latest"]
    assert "not found" in reasons["ghcr.io/me/removed:latest"]
    assert "HTTP 404" in reasons["foo:1"]  # image inspect failed
    # Nothing was asked of the registry for pinned/local/mirror images.
    assert {r.url.host for r in registry.requests} <= {"ghcr.io", "auth.test"}


async def test_rate_limit_backs_off_then_succeeds(sleeps: list[float]) -> None:
    docker = FakeDocker([container("web", "nginx:1")], {ID_A: {"RepoDigests": [f"nginx@{D_OLD}"]}})
    registry = FakeRegistry({"library/nginx:1": D_OLD})
    registry.rate_limit_first = 1
    collector = make(docker, registry)
    row = by_image(await collector.collect())["nginx:1"]
    await collector.close()
    assert row["status"] == "up_to_date"
    assert sleeps and sleeps[0] == pytest.approx(3.0, abs=0.1)


async def test_persistent_rate_limit_is_unknown_and_not_cached(sleeps: list[float]) -> None:
    docker = FakeDocker([container("web", "nginx:1")], {ID_A: {"RepoDigests": [f"nginx@{D_OLD}"]}})
    registry = FakeRegistry({"library/nginx:1": D_OLD})
    registry.rate_limit_first = 99
    collector = make(docker, registry)
    row = by_image(await collector.collect())["nginx:1"]
    assert row["status"] == "unknown" and "rate limited" in row["reason"]
    registry.rate_limit_first = 0
    again = by_image(await collector.collect())["nginx:1"]
    await collector.close()
    assert again["status"] == "up_to_date"  # the failure was not cached


async def test_missing_digest_header_falls_back_to_body_hash() -> None:
    docker = FakeDocker([container("web", "nginx:1")], {ID_A: {"RepoDigests": [f"nginx@{D_OLD}"]}})
    registry = FakeRegistry({"library/nginx:1": D_OLD})
    registry.omit_digest_header = True
    collector = make(docker, registry)
    row = by_image(await collector.collect())["nginx:1"]
    await collector.close()
    expected = "sha256:" + hashlib.sha256(b'{"manifest": true}').hexdigest()
    assert row["remote_digest"] == expected and row["status"] == "update_available"


async def test_unreachable_registry_is_unknown() -> None:
    docker = FakeDocker([container("web", "nginx:1")], {ID_A: {"RepoDigests": [f"nginx@{D_OLD}"]}})

    def refuse(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("no route")

    collector = ImageUpdatesCollector(
        ImageUpdatesCollectorSettings(url="tcp://docker.test:2375"),
        HostEnv(),
        docker_transport=httpx.MockTransport(docker.handler),
        registry_transport=httpx.MockTransport(refuse),
    )
    data = await collector.collect()
    await collector.close()
    row = data["images"][0]
    assert row["status"] == "unknown" and "unreachable" in row["reason"]


async def test_basic_auth_challenge_is_auth_required() -> None:
    docker = FakeDocker(
        [container("web", "reg.test/app:1")], {ID_A: {"RepoDigests": [f"reg.test/app@{D_OLD}"]}}
    )

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, headers={"WWW-Authenticate": 'Basic realm="registry"'})

    collector = ImageUpdatesCollector(
        ImageUpdatesCollectorSettings(url="tcp://docker.test:2375"),
        HostEnv(),
        docker_transport=httpx.MockTransport(docker.handler),
        registry_transport=httpx.MockTransport(handler),
    )
    row = (await collector.collect())["images"][0]
    await collector.close()
    assert row["status"] == "unknown" and "auth required" in row["reason"]


async def test_no_containers_and_persist_view() -> None:
    collector = make(FakeDocker([], {}), FakeRegistry({}))
    data = await collector.collect()
    assert data["summary"]["total"] == 0 and data["images"] == []
    full = {"images": [{"image": "a", "containers": ["x"], "status": "unknown", "reason": "r",
                        "local_digests": [D_OLD]}], "summary": {}}  # fmt: skip
    assert collector.persist_view(full)["images"] == [
        {"image": "a", "containers": ["x"], "status": "unknown", "reason": "r"}
    ]
    await collector.close()
