"""Cron time zone (`general.timezone`, else the host's) and spread-out housekeeping jobs."""

from __future__ import annotations

import os
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
from pydantic import ValidationError

from ai_server_monitor.config import Settings
from ai_server_monitor.hostenv import HostEnv, resolve_timezone
from ai_server_monitor.schedule import (
    HOUSEKEEPING_GRACE_S,
    HOUSEKEEPING_JITTER_S,
    Schedule,
    job_trigger,
)


def test_host_timezone_from_etc_timezone(tmp_path: Path) -> None:
    (tmp_path / "etc").mkdir()
    (tmp_path / "etc/timezone").write_text("Europe/Berlin\n")
    assert HostEnv(root=tmp_path).timezone() == ZoneInfo("Europe/Berlin")


@pytest.mark.skipif(os.name != "posix", reason="symlinks")
def test_host_timezone_from_localtime_link(tmp_path: Path) -> None:
    (tmp_path / "etc").mkdir()
    (tmp_path / "etc/localtime").symlink_to("/usr/share/zoneinfo/America/New_York")
    assert HostEnv(root=tmp_path).timezone() == ZoneInfo("America/New_York")


def test_host_timezone_falls_back_to_utc(tmp_path: Path) -> None:
    assert HostEnv(root=tmp_path).timezone() == ZoneInfo("UTC")
    (tmp_path / "etc").mkdir()
    (tmp_path / "etc/timezone").write_text("Not/AZone\n")
    assert HostEnv(root=tmp_path).timezone() == ZoneInfo("UTC")


def test_setting_overrides_the_host_and_is_validated(tmp_path: Path) -> None:
    (tmp_path / "etc").mkdir()
    (tmp_path / "etc/timezone").write_text("Europe/Berlin\n")
    env = HostEnv(root=tmp_path)
    assert resolve_timezone("", env) == ZoneInfo("Europe/Berlin")
    assert resolve_timezone("Asia/Tokyo", env) == ZoneInfo("Asia/Tokyo")
    assert Settings(general={"timezone": " Asia/Tokyo "}).general.timezone == "Asia/Tokyo"
    with pytest.raises(ValidationError, match="unknown time zone"):
        Settings(general={"timezone": "Mars/Olympus"})


def test_cron_runs_in_the_given_time_zone() -> None:
    tz = ZoneInfo("Europe/Berlin")
    trigger = job_trigger({"cron": "0 6 * * *"}, tz)
    now = datetime(2026, 7, 1, 12, 0, tzinfo=tz)
    fire = trigger.get_next_fire_time(None, now)
    assert fire is not None and fire.hour == 6 and fire.utcoffset() == timedelta(hours=2)
    assert Schedule(timezone=tz).timezone == tz


async def test_housekeeping_is_jittered_and_skips_missed_runs() -> None:
    schedule = Schedule(paused=True)

    async def noop() -> None:
        return None

    schedule.add_housekeeping("prune", noop, timedelta(hours=1))
    schedule.add_housekeeping("approvals:sweep", noop, timedelta(minutes=1))
    schedule.start()
    try:
        jobs = {j.id: j for j in schedule._sched.get_jobs()}
        assert jobs["prune"].trigger.jitter == HOUSEKEEPING_JITTER_S
        assert jobs["approvals:sweep"].trigger.jitter == 6.0  # 10 % of the interval
        assert jobs["prune"].misfire_grace_time == HOUSEKEEPING_GRACE_S
    finally:
        schedule.stop()
