"""`diagnose_service`: full test details + non-secret config + collector + retry state +
where to look next, never a secret."""

from __future__ import annotations

import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
import respx

from ai_server_monitor.apiaccess import api_tool_specs
from ai_server_monitor.apiaccess.diagnose import diagnose_tool_spec
from ai_server_monitor.arr import qbit_auth
from ai_server_monitor.chat.prompts import system_prompt
from ai_server_monitor.chat.tools import ToolContext, ToolError
from ai_server_monitor.collectors.base import CollectorResult, HostEnv
from ai_server_monitor.db import Database
from ai_server_monitor.settings_store import SettingsStore

from .arrfakes import QBIT_PASSWORD, make_settings

QBIT = "http://qbit.test:8080"


class FakeDocker:
    async def get(self, path: str, **params: Any) -> Any:
        if path.endswith("/logs"):
            lines = [
                "2026-11-15T10:00:00Z (N) all fine",
                "2026-11-15T10:00:12Z (W) WebUI: Invalid Host header, port mismatch.",
                "2026-11-15T10:00:13Z (W) WebAPI login failure. Password_PBKDF2=secretvalue",
            ]
            frames = b""
            for line in lines:
                data = f"{line}\n".encode()
                frames += bytes([1, 0, 0, 0]) + len(data).to_bytes(4, "big") + data
            return frames
        assert path == "/containers/json"
        return [
            {
                "Names": ["/qbittorrent"],
                "Image": "lscr.io/linuxserver/qbittorrent",
                "State": "running",
                "Status": "Up 3 days",
                "Mounts": [
                    {"Type": "bind", "Source": "/srv/qbit/config", "Destination": "/config"}
                ],
            },
            {"Names": ["/radarr"], "Image": "radarr", "State": "running", "Mounts": []},
        ]


async def ctx_for(db: Database, tmp_path: Path) -> ToolContext:
    settings = make_settings()
    await SettingsStore(db).save(settings)
    collector_result = CollectorResult(
        "arr",
        time.time() - 90,
        True,
        data={
            "apps": [],
            "qbittorrent": {
                "type": "qbittorrent",
                "name": "qbittorrent",
                "ok": False,
                "error": "qBittorrent answered 401 Unauthorized ...",
            },
        },
    )
    app = SimpleNamespace(
        settings=settings,
        db=db,
        host_env=HostEnv(root=tmp_path),
        collectors=SimpleNamespace(
            latest={"arr": collector_result},
            collectors={"docker": SimpleNamespace(api=FakeDocker())},
        ),
    )
    return ToolContext(app, "s1")  # type: ignore[arg-type]


def tool(settings: Any) -> Any:
    spec = diagnose_tool_spec(ToolError)
    assert "diagnose_service" not in {s.name for s in api_tool_specs(settings)}
    return spec


@respx.mock
async def test_diagnose_qbit_401_gives_cause_config_container_and_next_steps(
    db: Database, tmp_path: Path
) -> None:
    ctx = await ctx_for(db, tmp_path)
    respx.post(f"{QBIT}/api/v2/auth/login").respond(401, text="Unauthorized")
    spec = tool(ctx.app.settings)
    assert spec.enabled(ctx.app.settings)
    out = await spec.handler(ctx, spec.args_model.model_validate({"service": "qbit"}))
    (d,) = out["diagnosis"]
    assert d["service"] == "qbittorrent"
    assert d["config"]["url"] == QBIT and d["config"]["username"] == "admin"
    assert d["config"]["pw_configured"] is True and "settings_last_saved" in d["config"]
    test = d["connection_test"]
    assert test["ok"] is False and "NOT a wrong password" in test["message"]
    assert test["details"]["http_status"] == 401 and test["details"]["kind"] == "host_validation"
    assert d["login_retry_state"]["paused"] is True
    assert "401 Unauthorized" in d["collector"]["this_service_error"]
    assert d["container"]["name"] == "qbittorrent"
    assert d["container"]["config_file_on_host"] == "/srv/qbit/config/qBittorrent/qBittorrent.conf"
    steps = " ".join(d["next_steps"])
    assert "container_logs name=qbittorrent" in steps and "grep -i webui /srv/qbit" in steps
    assert QBIT_PASSWORD not in str(out)
    shown = d["container"]["relevant_log_lines"]
    assert len(shown) == 2 and "Invalid Host header" in shown[0]
    assert "secretvalue" not in str(shown)


@respx.mock
async def test_diagnose_does_not_log_in_again_while_paused(db: Database, tmp_path: Path) -> None:
    ctx = await ctx_for(db, tmp_path)
    route = respx.post(f"{QBIT}/api/v2/auth/login").respond(200, text="Fails.")
    spec = tool(ctx.app.settings)
    args = spec.args_model.model_validate({"service": "qbittorrent"})
    await spec.handler(ctx, args)
    out = await spec.handler(ctx, args)
    assert route.call_count == 1
    assert "Not tried again" in out["diagnosis"][0]["connection_test"]["message"]
    assert (
        qbit_auth.state_of(qbit_auth.backoff_key(QBIT, "admin", QBIT_PASSWORD))["failures_in_a_row"]
        == 1
    )


async def test_diagnose_unknown_service_lists_the_configured(db: Database, tmp_path: Path) -> None:
    ctx = await ctx_for(db, tmp_path)
    spec = tool(ctx.app.settings)
    with pytest.raises(ToolError, match="qbittorrent"):
        await spec.handler(ctx, spec.args_model.model_validate({"service": "plex"}))


async def test_chat_gets_the_tool_from_build_tools_only_when_a_service_is_configured() -> None:
    from ai_server_monitor.chat.tools import build_tools
    from ai_server_monitor.config import Settings

    assert "diagnose_service" in build_tools(make_settings())
    assert "diagnose_service" not in build_tools(Settings())


def test_prompt_says_diagnose_first_and_never_offer_what_a_tool_can_do() -> None:
    text = system_prompt("en", tool_names={"diagnose_service", "container_logs"})
    assert "`diagnose_service` your FIRST tool" in text and "`container_logs`" in text
    assert "NEVER end with an offer" in system_prompt("en", tool_names=set())
    assert "do not exist here" in system_prompt("en")  # docker/podman/kubectl


def test_prompt_has_the_troubleshooting_rule_only_with_the_tool() -> None:
    assert "INVESTIGATE" in system_prompt("en", tool_names={"diagnose_service"})
    assert "INVESTIGATE" not in system_prompt("en", tool_names={"api_call"})
