"""The selected model's own sampling defaults, capabilities and context length, read from
Ollama's /api/show, and "auto" sampling settings (not sent, so those defaults apply)."""

from __future__ import annotations

import json

import httpx
import pytest
import respx
from fastapi.testclient import TestClient
from pydantic import ValidationError

from ai_server_monitor.agent.models import model_settings
from ai_server_monitor.config import Bootstrap, LLMSettings, settings_json_schema
from ai_server_monitor.llm.modelinfo import clear_cache, ollama_root, parse_show, read_model_info
from ai_server_monitor.main import create_app
from ai_server_monitor.settings_store import upgrade_stored

from .conftest import LLM_URL
from .test_chat import call, say, streamed

SHOW = {
    "parameters": 'temperature 1\ntop_k 64\ntop_p 0.95\nstop "<end>"',
    "capabilities": ["completion", "vision", "tools", "thinking"],
    "model_info": {"general.architecture": "gemma4", "gemma4.context_length": 262144},
}


@pytest.fixture(autouse=True)
def _fresh_cache() -> None:
    clear_cache()


# --------------------------------------------------------------------------- settings


def test_sampling_defaults_to_auto_and_accepts_numbers() -> None:
    cfg = LLMSettings()
    assert (cfg.temperature, cfg.top_p, cfg.top_k) == ("auto", "auto", "auto")
    cfg = LLMSettings(temperature=0.7, top_p=0.9, top_k=40)
    assert (cfg.temperature, cfg.top_p, cfg.top_k) == (0.7, 0.9, 40)
    for bad in ({"temperature": 3}, {"top_p": 0}, {"top_k": -1}, {"temperature": "hot"}):
        with pytest.raises(ValidationError):
            LLMSettings(**bad)


def test_schema_offers_number_or_auto() -> None:
    props = settings_json_schema()["$defs"]["LLMSettings"]["properties"]
    for name in ("temperature", "top_p", "top_k"):
        kinds = props[name]["anyOf"]
        assert {"const": "auto", "type": "string"} in kinds
        assert any(k.get("type") in ("number", "integer") for k in kinds)
        assert props[name]["default"] == "auto"


def test_old_default_temperature_becomes_auto_but_a_chosen_one_stays() -> None:
    cleaned, dropped = upgrade_stored({"llm": {"temperature": 0.2, "model": "m"}})
    assert cleaned == {"llm": {"model": "m"}} and dropped
    kept, dropped = upgrade_stored({"llm": {"temperature": 0.5}})
    assert kept == {"llm": {"temperature": 0.5}} and not dropped


# --------------------------------------------------------------------------- request payload


def test_auto_sends_no_sampling_values() -> None:
    out = model_settings(LLMSettings(base_url=LLM_URL, model="m"))
    assert not {"temperature", "top_p", "top_k"} & out.keys()


def test_chosen_values_are_sent() -> None:
    cfg = LLMSettings(base_url=LLM_URL, model="m", temperature=0.7, top_p=0.9, top_k=40)
    out = model_settings(cfg)
    assert (out["temperature"], out["top_p"], out["top_k"]) == (0.7, 0.9, 40)  # type: ignore[typeddict-item]


# --------------------------------------------------------------------------- reading the model


def test_parse_show_reads_parameters_capabilities_and_context() -> None:
    info = parse_show(SHOW)
    assert info is not None
    assert info.parameters == {"temperature": 1, "top_k": 64, "top_p": 0.95}
    assert info.capabilities == ["completion", "vision", "tools", "thinking"] and info.thinks
    assert info.context_length == 262144
    assert parse_show({"error": "nope"}) is None and parse_show("x") is None


def test_ollama_root_drops_the_v1() -> None:
    assert ollama_root("https://o.example/v1/") == "https://o.example"
    assert ollama_root("http://o:11434") == "http://o:11434"


@respx.mock
async def test_model_info_is_cached_per_model_and_refreshed_when_it_changes() -> None:
    route = respx.post("http://llm.test/api/show").mock(return_value=httpx.Response(200, json=SHOW))
    a = LLMSettings(base_url=LLM_URL, model="a")
    assert (await read_model_info(a)) is not None
    assert (await read_model_info(a)) is not None
    assert route.call_count == 1
    await read_model_info(LLMSettings(base_url=LLM_URL, model="b"))
    assert route.call_count == 2
    assert json.loads(route.calls.last.request.content) == {"model": "b"}


@respx.mock
async def test_not_ollama_or_unreachable_gives_none_quietly() -> None:
    respx.post("http://llm.test/api/show").mock(return_value=httpx.Response(404))
    assert await read_model_info(LLMSettings(base_url=LLM_URL, model="m")) is None
    clear_cache()
    respx.post("http://llm.test/api/show").mock(side_effect=httpx.ConnectError("down"))
    assert await read_model_info(LLMSettings(base_url=LLM_URL, model="m")) is None
    assert await read_model_info(LLMSettings()) is None  # not configured


@respx.mock
def test_model_info_endpoint(bootstrap: Bootstrap) -> None:
    respx.post("http://llm.test/api/show").mock(return_value=httpx.Response(200, json=SHOW))
    with TestClient(create_app(bootstrap)) as client:
        got = client.post(
            "/api/settings/llm-model-info", json={"base_url": LLM_URL, "model": "m"}
        ).json()
        assert got == {
            "available": True,
            "parameters": {"temperature": 1, "top_k": 64, "top_p": 0.95},
            "capabilities": ["completion", "vision", "tools", "thinking"],
            "context_length": 262144,
        }
        none = client.post("/api/settings/llm-model-info", json={}).json()
        assert none["available"] is False and none["parameters"] == {}


@respx.mock
def test_worker_model_info_is_read_as_the_agents_use_the_worker(bootstrap: Bootstrap) -> None:
    route = respx.post("http://llm.test/api/show").mock(return_value=httpx.Response(200, json=SHOW))
    with TestClient(create_app(bootstrap)) as client:
        values = client.get("/api/settings").json()["values"]
        values["llm"].update(base_url=LLM_URL, model="test-model")
        assert client.put("/api/settings", json=values).status_code == 200
        # its own model on the main model's endpoint (its base URL is empty)
        url = "/api/settings/llm-model-info?profile=worker"
        got = client.post(url, json={"model": "small"}).json()
        assert got["available"] is True
        assert json.loads(route.calls.last.request.content) == {"model": "small"}
        # no worker model: the main model does the sub-tasks, so it is the one asked about
        client.post(url, json={"model": ""})
        assert json.loads(route.calls.last.request.content) == {"model": "test-model"}


@respx.mock
def test_settings_test_works_with_auto_sampling(bootstrap: Bootstrap, shell: list[str]) -> None:
    respx.get(f"{LLM_URL}/models").mock(return_value=httpx.Response(404))
    route = respx.post(f"{LLM_URL}/chat/completions").mock(
        side_effect=[streamed(call("bash", {"command": "echo x"})), streamed(say("OK"))]
    )
    with TestClient(create_app(bootstrap)) as client:
        result = client.post(
            "/api/settings/test-llm",
            json={"base_url": LLM_URL, "model": "m", "temperature": "auto"},
        ).json()
    assert result["ok"] is True
    sent = json.loads(route.calls.last.request.content)
    assert "temperature" not in sent and "top_p" not in sent and "top_k" not in sent
