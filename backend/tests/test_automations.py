from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from ai_server_monitor.actions import ActionService
from ai_server_monitor.arr.automations import AutomationEnv, register, search, stalled, triage
from ai_server_monitor.collectors.arr import ArrCollector
from ai_server_monitor.collectors.base import HostEnv
from ai_server_monitor.db import Database
from ai_server_monitor.scheduler import Scheduler

from .arrfakes import HASH_A, HASH_B, FakeArrWorld, make_settings, torrent

HOURS = 3600


def iso_ago(hours: float) -> str:
    return (datetime.now(UTC) - timedelta(hours=hours)).strftime("%Y-%m-%dT%H:%M:%SZ")


def queue_item(item_id: int, title: str, hash_: str | None = None, **extra: Any) -> dict[str, Any]:
    return {
        "id": item_id,
        "title": title,
        "status": "downloading",
        "trackedDownloadStatus": "ok",
        "trackedDownloadState": "downloading",
        "downloadId": hash_.upper() if hash_ else None,
        "added": iso_ago(10),
        **extra,
    }


def make_env(
    db: Database, world: FakeArrWorld, *, policy: str = "approve", **arr: Any
) -> AutomationEnv:
    actions = {
        "stalled_cleanup": {"policy": policy, "stalled_after": "6h"},
        "missing_search": {"policy": policy, "batch": 3},
        "cutoff_search": {"policy": "suggest", "batch": 2},
        "indexer_triage": {"policy": policy},
    }
    settings = make_settings(actions, **arr)
    service = ActionService(db, settings, transport=world.transport())
    return AutomationEnv(service, settings)


def calls(world: FakeArrWorld, host: str, method: str) -> list[dict[str, Any]]:
    return world.calls(host, method)


@pytest.fixture
def world() -> FakeArrWorld:
    return FakeArrWorld()


# --------------------------------------------------------------------------- stalled


async def test_stalled_torrent_maps_to_the_owning_arr_and_is_requested_once(
    db: Database, world: FakeArrWorld
) -> None:
    world.torrents = {
        HASH_A: torrent(HASH_A, "Owned.Stuck.2026", "stalledDL", 0.3, inactive_s=7 * HOURS),
        "c" * 40: torrent("c" * 40, "Fine.Download", "downloading", 0.3, 7 * HOURS, dlspeed=900),
    }
    # The same hash sits in the *Sonarr* queue; the Radarr queue holds an unrelated item.
    world.queue["sonarr"] = [queue_item(42, "Show.S01E01", HASH_A)]
    world.queue["radarr"] = [queue_item(7, "Other.Movie", "d" * 40)]
    env = make_env(db, world)

    summary = await stalled.run(env)
    assert summary == {"requested": 1, "outcomes": {"pending": 1}}
    [record] = (await env.service.list_actions())["items"]
    assert record["action"] == "arr_remove_queue_item" and record["status"] == "pending"
    assert record["args"] == {
        "app": "sonarr",  # mapped by download id to the app that owns the download
        "instance": "sonarr",
        "queue_id": 42,
        "blocklist": True,
        "remove_from_client": True,
        "search": True,
        "title": "Show.S01E01",
    }
    assert record["source"] == "scheduler" and "no progress for 7.0h" in record["reason"]
    assert calls(world, "sonarr.test", "DELETE") == []  # approval first

    # Running again does not file a second request for the same item.
    assert await stalled.run(env) == {"requested": 0, "outcomes": {}}
    assert (await env.service.list_actions())["total"] == 1

    # Approving removes it from the owning app with blocklist + re-search.
    done = await env.service.decide(record["id"], "approve")
    assert done["status"] == "executed"
    delete = calls(world, "sonarr.test", "DELETE")[0]
    assert delete["path"] == "/api/v3/queue/42"
    assert delete["params"]["blocklist"] == "true" and delete["params"]["skipRedownload"] == "false"
    assert calls(world, "radarr.test", "DELETE") == []


@pytest.mark.parametrize("policy", ["auto", "approve"])
async def test_torrent_not_owned_by_any_arr_is_only_suggested(
    db: Database, world: FakeArrWorld, policy: str
) -> None:
    world.torrents = {
        HASH_B: torrent(HASH_B, "Hand.Added.Iso", "stalledDL", 0.2, inactive_s=9 * HOURS)
    }
    world.queue["radarr"] = [queue_item(7, "Unrelated", "d" * 40)]
    env = make_env(db, world, policy=policy)

    summary = await stalled.run(env)
    assert summary == {"requested": 1, "outcomes": {"suggested": 1}}
    [record] = (await env.service.list_actions())["items"]
    assert record["action"] == "qbit_delete_torrent" and record["status"] == "suggested"
    assert record["policy"] == policy  # even under `auto` it is never executed
    assert "not owned by any *arr" in record["reason"]
    assert HASH_B in world.torrents
    assert [r for r in world.requests if r["path"] == "/api/v2/torrents/delete"] == []
    # And a suggestion cannot be turned into a deletion later.
    from ai_server_monitor.actions import Conflict

    with pytest.raises(Conflict):
        await env.service.decide(record["id"], "approve")


async def test_no_unowned_suggestions_when_an_arr_queue_cannot_be_read(
    db: Database, world: FakeArrWorld
) -> None:
    world.torrents = {HASH_A: torrent(HASH_A, "Maybe.Owned", "stalledDL", 0.2, 9 * HOURS)}
    world.fail_hosts.add("radarr.test")  # ownership cannot be established
    env = make_env(db, world)
    assert (await stalled.run(env))["requested"] == 0


async def test_failed_queue_items_are_cleaned_but_fresh_ones_get_time(
    db: Database, world: FakeArrWorld
) -> None:
    world.queue["radarr"] = [
        queue_item(
            1,
            "Import.Blocked.Movie",
            HASH_A,  # the broken torrent below belongs to this very item
            trackedDownloadStatus="warning",
            trackedDownloadState="importBlocked",
            statusMessages=[{"title": "Movie", "messages": ["No files found are eligible"]}],
        ),
        queue_item(2, "Just.Failed", trackedDownloadState="failed", added=iso_ago(1)),
        queue_item(3, "Healthy", "e" * 40),
        queue_item(4, "Failed.Usenet", status="failed", trackedDownloadState="failedPending"),
    ]
    world.torrents = {
        HASH_A: torrent(HASH_A, "Same.Item", "error", 0.1),
    }
    env = make_env(db, world)
    summary = await stalled.run(env)
    items = (await env.service.list_actions())["items"]
    ids = sorted(i["args"]["queue_id"] for i in items)
    assert ids == [1, 4] and summary["requested"] == 2  # item 1 once, 2 too fresh, 3 fine
    assert all(i["action"] == "arr_remove_queue_item" for i in items)
    by_id = {i["args"]["queue_id"]: i for i in items}
    assert "qBittorrent state error" in by_id[1]["reason"]  # found via the torrent first
    assert "failedPending" in by_id[4]["reason"]


async def test_stalled_cleanup_is_skipped_when_qbit_is_unreadable(
    db: Database, world: FakeArrWorld
) -> None:
    world.fail_hosts.add("qbit.test")
    env = make_env(db, world)
    assert "unreadable" in (await stalled.run(env))["skipped"]


# --------------------------------------------------------------------------- searches


def wanted(prefix: str, n: int, **extra: Any) -> list[dict[str, Any]]:
    # Older lastSearchTime first: item 0 was searched longest ago.
    return [
        {
            "id": i,
            "title": f"{prefix}{i}",
            "year": 2020 + i,
            "lastSearchTime": f"2026-01-0{i + 1}T00:00:00Z",
            **extra,
        }
        for i in range(n)
    ]


async def test_missing_search_rotates_through_the_wanted_list(
    db: Database, world: FakeArrWorld
) -> None:
    items = wanted("Movie", 7)
    items[1]["isAvailable"] = False  # not released yet: never searched
    world.wanted[("radarr", "missing")] = items
    env = make_env(db, world, policy="suggest", sonarr=[], whisparr=[])

    batches = []
    for _ in range(2):
        assert (await search.run(env, "missing"))["outcomes"] == {"suggested": 1}
        batches.append((await env.service.list_actions(limit=1))["items"][0]["args"]["ids"])
    # Least recently searched first, 3 per run, nothing repeated, unreleased item skipped.
    assert batches == [[0, 2, 3], [4, 5, 6]]
    assert (await search.run(env, "missing")) == {"requested": 0, "outcomes": {}}
    first = (await env.service.list_actions())["items"][-1]
    assert first["action"] == "arr_search" and first["status"] == "suggested"
    assert first["args"]["purpose"] == "missing" and first["policy"] == "suggest"
    assert first["args"]["title"].startswith("Movie0 (2020), Movie2 (2022)")
    assert calls(world, "radarr.test", "POST") == []  # suggestions send nothing


async def test_missing_search_under_auto_sends_the_command_and_respects_the_cap(
    db: Database, world: FakeArrWorld
) -> None:
    world.wanted[("radarr", "missing")] = wanted("Movie", 9)
    world.wanted[("sonarr", "missing")] = [
        {
            "id": 70,
            "series": {"title": "Show"},
            "seasonNumber": 1,
            "episodeNumber": 2,
            "lastSearchTime": "2026-01-01T00:00:00Z",
        }
    ]
    env = make_env(db, world, policy="auto", whisparr=[])
    env.settings.arr.actions.missing_search.max_per_hour = 1
    summary = await search.run(env, "missing")
    # batch 3 over two instances -> 1 item each; the per-hour cap lets only one execute.
    assert summary["outcomes"] == {"executed": 1, "rejected": 1}
    records = (await env.service.list_actions())["items"]
    rejected = next(r for r in records if r["status"] == "rejected")
    assert "limit reached" in rejected["result"]["detail"]
    executed = next(r for r in records if r["status"] == "executed")
    assert executed["args"]["app"] == "radarr" and executed["args"]["ids"] == [0]
    body = json.loads(calls(world, "radarr.test", "POST")[0]["body"])
    assert body == {"name": "MoviesSearch", "movieIds": [0]}
    assert rejected["args"]["title"] == "Show S01E02"


async def test_cutoff_search_has_its_own_policy_and_apps_without_wanted_lists_are_skipped(
    db: Database, world: FakeArrWorld
) -> None:
    world.wanted[("radarr", "cutoff")] = wanted("Upgrade", 5)
    world.wanted_unsupported.add("whisparr")
    env = make_env(db, world, policy="auto", sonarr=[])
    summary = await search.run(env, "cutoff")
    # radarr: batch 2 over 2 apps -> 1 item; whisparr offers no list -> nothing.
    assert summary == {"requested": 1, "outcomes": {"suggested": 1}}  # cutoff policy = suggest
    record = (await env.service.list_actions())["items"][0]
    assert record["args"]["purpose"] == "cutoff" and record["args"]["ids"] == [0]


# --------------------------------------------------------------------------- triage


async def arr_result(world: FakeArrWorld, settings: Any) -> dict[str, Any]:
    collector = ArrCollector(settings.collectors.arr, HostEnv(), transport=world.transport())
    collector.app_settings = settings
    data = await collector.collect()
    await collector.close()
    return data


async def test_triage_tests_failing_indexers_and_exposes_findings(
    db: Database, world: FakeArrWorld
) -> None:
    world.indexers = [
        {"id": 1, "name": "Fine", "enable": True},
        {"id": 2, "name": "Flaky", "enable": True},
        {"id": 3, "name": "Off", "enable": False},
    ]
    world.indexer_status = [
        {"indexerId": 2, "disabledTill": "2999-01-01T00:00:00Z"},
        {"indexerId": 3, "disabledTill": "2999-01-01T00:00:00Z"},
    ]
    world.health = {
        "radarr": [
            {"source": "IndexerCheck", "type": "error", "message": "All indexers unavailable"}
        ]
    }
    env = make_env(db, world, policy="suggest")
    data = await arr_result(world, env.settings)
    findings = data["findings"]
    assert {
        "severity": "crit",
        "source": "radarr/radarr",
        "message": "health: All indexers unavailable",
    } in findings
    assert any(f["source"] == "prowlarr/prowlarr" and "Flaky" in f["message"] for f in findings)
    assert not any("Fine" in f["message"] for f in findings)

    from ai_server_monitor.collectors.base import CollectorResult

    result = CollectorResult("arr", 1.0, ok=True, data=data)
    env.collector_results = lambda: {"arr": result}
    summary = await triage.run(env)
    assert summary == {"failing": 1, "requested": 1, "outcomes": {"suggested": 1}}
    [record] = (await env.service.list_actions())["items"]
    assert record["action"] == "prowlarr_test_indexer" and record["args"]["indexer_id"] == 2
    assert record["status"] == "suggested" and "Flaky" in record["args"]["title"]
    assert calls(world, "prowlarr.test", "POST") == []
    assert (await triage.run(env))["requested"] == 0  # not filed again
    env.collector_results = dict
    assert "skipped" in await triage.run(env)


# --------------------------------------------------------------------------- add media


async def test_add_media_walk_with_lookup_and_defaults(db: Database, world: FakeArrWorld) -> None:
    world.lookup_results = [
        {"tmdbId": 11, "title": "Other", "id": 0},
        {"tmdbId": 603, "title": "The Matrix", "year": 1999, "id": 0},
    ]
    settings = make_settings()
    assert settings.arr.actions.add_media.policy == "approve"  # default
    service = ActionService(db, settings, transport=world.transport())
    app = service.clients.apps[("radarr", "radarr")]
    assert [m["title"] for m in await app.lookup("matrix")] == ["Other", "The Matrix"]
    assert (await app.quality_profiles())[0]["id"] == 4
    assert (await app.root_folders())[0]["path"] == "/media/movies"

    pending = await service.request(
        "arr_add_media",
        {
            "app": "radarr",
            "instance": "radarr",
            "tmdb_id": 603,
            "quality_profile_id": 4,
            "root_folder_path": "/media/movies",
            "title": "The Matrix",
        },
        "chat:s1",
    )
    assert pending["status"] == "pending" and world.added == []
    done = await service.decide(pending["id"], "approve")
    assert done["status"] == "executed" and done["result"]["id"] == 501
    added = world.added[0]
    assert added["tmdbId"] == 603 and added["qualityProfileId"] == 4 and added["monitored"] is True
    assert added["rootFolderPath"] == "/media/movies"
    assert added["addOptions"] == {"monitor": "movieOnly", "searchForMovie": True}

    # Sonarr: needs a tvdb id and uses series options.
    missing_id = await service.request(
        "arr_add_media",
        {
            "app": "sonarr",
            "instance": "sonarr",
            "tmdb_id": 1,
            "quality_profile_id": 4,
            "root_folder_path": "/tv",
        },
        "chat:s1",
    )
    assert missing_id["status"] == "rejected" and "tvdb_id" in missing_id["result"]["detail"]
    world.lookup_results = [{"tvdbId": 81189, "title": "Breaking Bad", "id": 0}]
    series = await service.request(
        "arr_add_media",
        {
            "app": "sonarr",
            "instance": "sonarr",
            "tvdb_id": 81189,
            "quality_profile_id": 4,
            "root_folder_path": "/tv",
            "search": False,
        },
        "chat:s1",
    )
    await service.decide(series["id"], "approve")
    assert world.added[1]["addOptions"] == {"monitor": "all", "searchForMissingEpisodes": False}
    assert world.added[1]["seasonFolder"] is True

    # Already in the library -> the action fails, nothing is posted.
    world.lookup_results = [{"tmdbId": 603, "title": "The Matrix", "id": 9}]
    again = await service.request(
        "arr_add_media",
        {
            "app": "radarr",
            "instance": "radarr",
            "tmdb_id": 603,
            "quality_profile_id": 4,
            "root_folder_path": "/m",
        },
        "chat:s1",
    )
    failed = await service.decide(again["id"], "approve")
    assert failed["status"] == "failed" and "already in the library" in failed["result"]["error"]
    assert len(world.added) == 2
    await service.close()


# --------------------------------------------------------------------------- scheduling


def test_register_schedules_only_automations_that_are_on_and_configured() -> None:
    settings = make_settings(
        {
            "stalled_cleanup": {"policy": "approve"},
            "missing_search": {"policy": "off"},
            "cutoff_search": {"policy": "suggest"},
            "indexer_triage": {"policy": "suggest"},
        }
    )
    scheduler = Scheduler()
    env = AutomationEnv(service=None, settings=settings)  # type: ignore[arg-type]
    assert sorted(register(scheduler, env)) == [
        "auto:cutoff_search",
        "auto:indexer_triage",
        "auto:stalled_cleanup",
    ]
    nothing = Scheduler()
    bare = make_settings(prowlarr=None, qbittorrent=None, radarr=[], sonarr=[], whisparr=[])
    assert register(nothing, AutomationEnv(service=None, settings=bare)) == []  # type: ignore[arg-type]
