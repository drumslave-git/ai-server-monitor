from __future__ import annotations

import time

import pytest
from pydantic import ValidationError

from ai_server_monitor.arr import ArrError, Clients, QBittorrent
from ai_server_monitor.collectors.arr import ArrCollector, is_stalled_candidate, torrent_row
from ai_server_monitor.collectors.base import CollectorUnavailable, HostEnv
from ai_server_monitor.config import ArrSettings, Settings

from .arrfakes import (
    API_KEY,
    HASH_A,
    HASH_B,
    QBIT_PASSWORD,
    FakeArrWorld,
    make_settings,
    torrent,
)


def make_collector(world: FakeArrWorld, settings: Settings) -> ArrCollector:
    collector = ArrCollector(settings.collectors.arr, HostEnv(), transport=world.transport())
    collector.app_settings = settings
    return collector


async def test_qbit_logs_in_once_and_relogs_in_on_403() -> None:
    world = FakeArrWorld()
    world.torrents = {HASH_A: torrent(HASH_A, "x")}
    client = QBittorrent(
        "http://qbit.test:8080", "admin", QBIT_PASSWORD, transport=world.transport()
    )
    assert await client.version() == "v5.0.1"
    await client.torrents()
    assert world.logins == 1
    login = world.calls("qbit.test", "POST")[0]
    assert login["headers"]["referer"] == "http://qbit.test:8080"
    world.session_valid = False  # the session expired
    assert len(await client.torrents()) == 1
    assert world.logins == 2  # transparently logged in again, once
    await client.close()


async def test_qbit_wrong_password_and_unreachable() -> None:
    world = FakeArrWorld()
    client = QBittorrent("http://qbit.test:8080", "admin", "wrong", transport=world.transport())
    with pytest.raises(ArrError, match="wrong username or password"):
        await client.version()
    world.fail_hosts.add("qbit.test")
    with pytest.raises(ArrError) as info:
        await client.version()
    assert info.value.unreachable
    await client.close()


async def test_arr_queue_paging_and_auth_error() -> None:
    world = FakeArrWorld()
    world.queue["radarr"] = [{"id": i, "title": f"m{i}"} for i in range(450)]
    clients = Clients(make_settings().arr, transport=world.transport())
    app = clients.apps[("radarr", "radarr")]
    items, total = await app.queue(max_items=1000)
    assert total == 450 and len(items) == 450
    capped, total = await app.queue(max_items=50)
    assert len(capped) == 50 and total == 450
    world.deny_arr_key = True
    with pytest.raises(ArrError, match="access denied") as info:
        await app.status()
    assert API_KEY not in str(info.value)
    await clients.close()


async def test_collector_reports_queue_health_wanted_indexers_and_torrents() -> None:
    world = FakeArrWorld()
    world.queue["radarr"] = [
        {
            "id": 1,
            "title": "Movie.2026.1080p",
            "status": "completed",
            "trackedDownloadStatus": "warning",
            "trackedDownloadState": "importBlocked",
            "statusMessages": [{"title": "Movie", "messages": ["No files found are eligible"]}],
            "protocol": "torrent",
            "indexer": "NZBgeek",
        },
        {
            "id": 2,
            "title": "Fine.Movie",
            "status": "downloading",
            "trackedDownloadStatus": "ok",
            "trackedDownloadState": "downloading",
        },
    ]
    world.health = {
        "radarr": [{"source": "IndexerStatusCheck", "type": "warning", "message": "Indexers down"}]
    }
    world.indexers = [
        {"id": 1, "name": "Good", "enable": True, "protocol": "torrent"},
        {"id": 2, "name": "Bad", "enable": True, "protocol": "usenet"},
    ]
    world.indexer_status = [
        {
            "indexerId": 2,
            "disabledTill": "2999-01-01T00:00:00Z",
            "mostRecentFailure": "2026-11-14T10:00:00Z",
        },
        {"indexerId": 1, "disabledTill": "2001-01-01T00:00:00Z"},
    ]
    world.torrents = {
        HASH_A: torrent(HASH_A, "Stuck", "stalledDL", 0.4, inactive_s=7 * 3600),
        HASH_B: torrent(HASH_B, "Moving", "downloading", 0.4, inactive_s=7 * 3600, dlspeed=500),
        "c" * 40: torrent("c" * 40, "Seeding", "uploading", 1.0, inactive_s=99999),
        "d" * 40: torrent("d" * 40, "Broken", "error", 0.1),
    }
    settings = make_settings()
    collector = make_collector(world, settings)
    await collector.check()
    data = await collector.collect()
    await collector.close()

    radarr = next(a for a in data["apps"] if a["name"] == "radarr")
    assert radarr["version"] == "5.9.1"
    assert radarr["queue"]["total"] == 2 and radarr["queue"]["problems"] == 1
    problem = radarr["queue"]["items"][0]
    assert problem["problem"] and problem["tracked_state"] == "importBlocked"
    assert "No files found" in problem["messages"][0]
    assert radarr["health"] == [
        {"source": "IndexerStatusCheck", "level": "warning", "message": "Indexers down"}
    ]
    assert radarr["wanted"] == {"missing": 7, "cutoff_unmet": 3}
    assert {a["name"] for a in data["apps"]} == {"radarr", "sonarr"}

    indexers = {i["name"]: i for i in data["prowlarr"]["indexers"]}
    assert indexers["Bad"]["failing"] and indexers["Bad"]["disabled_till"].startswith("2999")
    assert not indexers["Good"]["failing"]
    assert data["prowlarr"]["summary"] == {"total": 2, "enabled": 2, "failing": 1}

    qbit = data["qbittorrent"]
    assert qbit["version"] == "v5.0.1" and qbit["summary"]["total"] == 4
    assert {t["name"] for t in qbit["stalled"]} == {"Stuck", "Broken"}  # not Moving/Seeding
    assert qbit["stalled"][0]["inactive_s"] >= 7 * 3600
    assert data["summary"]["queue_problems"] == 1 and data["summary"]["indexers_failing"] == 1
    assert data["summary"]["stalled_candidates"] == 2 and data["summary"]["unreachable"] == []


async def test_collector_isolates_a_down_instance_and_is_unavailable_when_all_are_down() -> None:
    world = FakeArrWorld()
    world.fail_hosts.add("sonarr.test")
    collector = make_collector(world, make_settings())
    data = await collector.collect()
    assert data["summary"]["unreachable"] == ["sonarr/sonarr"]
    sonarr = next(a for a in data["apps"] if a["name"] == "sonarr")
    assert sonarr["ok"] is False and "cannot reach" in sonarr["error"]
    world.fail_hosts.update({"radarr.test", "prowlarr.test", "qbit.test"})
    with pytest.raises(CollectorUnavailable):
        await collector.collect()
    await collector.close()


async def test_collector_unavailable_without_instances() -> None:
    settings = Settings()
    collector = ArrCollector(settings.collectors.arr, HostEnv())
    collector.app_settings = settings
    with pytest.raises(CollectorUnavailable, match="configured"):
        await collector.check()
    await collector.close()


async def test_persist_view_is_slim() -> None:
    world = FakeArrWorld()
    world.queue["radarr"] = [
        {"id": i, "title": f"m{i}", "trackedDownloadStatus": "error"} for i in range(30)
    ]
    world.torrents = {f"{i:040x}": torrent(f"{i:040x}", f"t{i}", "error") for i in range(40)}
    collector = make_collector(world, make_settings())
    data = await collector.collect()
    await collector.close()
    slim = collector.persist_view(data)
    radarr = next(a for a in slim["apps"] if a["name"] == "radarr")
    assert "items" not in radarr["queue"] and len(radarr["problem_items"]) == 10
    assert len(slim["qbittorrent"]["stalled"]) == 10 and slim["qbittorrent"]["stalled_total"] == 40


def test_stalled_rules() -> None:
    now = time.time()
    quiet = torrent_row(torrent(HASH_A, "x", "downloading", 0.5, inactive_s=7200), now)
    assert is_stalled_candidate(quiet, 3600) and not is_stalled_candidate(quiet, 3 * 3600)
    done = torrent_row(torrent(HASH_A, "x", "stalledUP", 1.0, inactive_s=99999), now)
    assert not is_stalled_candidate(done, 60)
    moving = torrent_row(torrent(HASH_A, "x", "downloading", 0.5, 7200, dlspeed=10), now)
    assert not is_stalled_candidate(moving, 60)
    never_active = {**torrent(HASH_A, "x", "metaDL", 0.0), "last_activity": 0}
    assert is_stalled_candidate(
        torrent_row(never_active, now + 7200), 3600
    )  # falls back to added_on


def test_arr_settings_validation_and_secret_masking() -> None:
    settings = make_settings()
    public = settings.public_dict()["arr"]
    assert public["radarr"][0]["api_key"] == "********"
    assert public["qbittorrent"]["password"] == "********"
    assert API_KEY not in str(public)
    with pytest.raises(ValidationError, match="duplicate instance names"):
        ArrSettings.model_validate(
            {
                "sonarr": [
                    {"name": "a", "url": "http://x", "api_key": "k"},
                    {"name": "a", "url": "http://y", "api_key": "k"},
                ]
            }
        )
