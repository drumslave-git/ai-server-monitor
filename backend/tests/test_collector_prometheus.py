from __future__ import annotations

import httpx
import pytest
from pydantic import ValidationError

from ai_server_monitor.collectors.base import CollectorUnavailable, HostEnv
from ai_server_monitor.collectors.prometheus import PrometheusCollector
from ai_server_monitor.config import PrometheusCollectorSettings, Settings


def make(handler, **cfg) -> PrometheusCollector:  # type: ignore[no-untyped-def]
    settings = PrometheusCollectorSettings(url="http://prom.test:9090", **cfg)
    return PrometheusCollector(settings, HostEnv(), transport=httpx.MockTransport(handler))


def vector(*values: tuple[dict[str, str], str]) -> httpx.Response:
    return httpx.Response(
        200,
        json={
            "status": "success",
            "data": {
                "resultType": "vector",
                "result": [{"metric": m, "value": [1, v]} for m, v in values],
            },
        },
    )


def test_defaults_ship_generic_queries_and_stay_inactive_without_url() -> None:
    cfg = PrometheusCollectorSettings()
    assert cfg.url == "" and cfg.enabled == "auto"
    assert cfg.queries[0].query == "up == 0"


def test_legacy_dict_queries_are_converted_and_duplicates_rejected() -> None:
    cfg = PrometheusCollectorSettings(queries={"load": "node_load5"})  # type: ignore[arg-type]
    assert [(q.name, q.query) for q in cfg.queries] == [("load", "node_load5")]
    with pytest.raises(ValidationError, match="duplicate"):
        PrometheusCollectorSettings(
            queries=[{"name": "a", "query": "up"}, {"name": "a", "query": "down"}]  # type: ignore[list-item]
        )


def test_secrets_are_masked_in_the_public_settings() -> None:
    s = Settings(collectors={"prometheus": {"url": "http://p:9090", "token": "tok"}})
    assert s.public_dict()["collectors"]["prometheus"]["token"] == "********"
    assert s.storage_dict()["collectors"]["prometheus"]["token"] == "tok"


async def test_check_needs_a_url() -> None:
    collector = PrometheusCollector(PrometheusCollectorSettings(), HostEnv())
    with pytest.raises(CollectorUnavailable, match="URL"):
        await collector.check()
    await collector.close()


async def test_collects_each_query_with_errors_per_query() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        query = request.url.params["query"]
        if query == "up == 0":
            return vector(({"job": "node", "instance": "a:9100"}, "0"))
        if query == "broken{":
            return httpx.Response(
                400, json={"status": "error", "errorType": "bad_data", "error": "parse error"}
            )
        return vector(*[({"i": str(i)}, str(i)) for i in range(5)])

    collector = make(
        handler,
        auto_queries=False,
        max_series=3,
        queries=[
            {"name": "down", "query": "up == 0", "description": "targets down"},
            {"name": "bad", "query": "broken{"},
            {"name": "many", "query": "foo"},
        ],
    )
    data = await collector.collect()
    await collector.close()
    down, bad, many = data["queries"]
    assert down["ok"] and down["description"] == "targets down"
    assert down["series"] == [{"labels": {"job": "node", "instance": "a:9100"}, "value": 0.0}]
    assert not bad["ok"] and "parse error" in bad["error"] and "series" not in bad
    assert many["ok"] and len(many["series"]) == 3 and many["total"] == 5 and many["truncated"]
    assert data["summary"] == {"queries": 3, "failed": ["bad"]}


async def test_unreachable_prometheus_is_reported_unavailable() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused")

    collector = make(handler)
    with pytest.raises(CollectorUnavailable, match="cannot reach Prometheus"):
        await collector.collect()
    await collector.close()


async def test_no_queries_is_fine() -> None:
    collector = make(lambda r: httpx.Response(500), queries=[])
    data = await collector.collect()
    await collector.close()
    assert data["queries"] == [] and data["summary"]["queries"] == 0


async def test_persist_view_is_slim() -> None:
    collector = make(
        lambda r: vector(*[({"i": str(i)}, "1") for i in range(30)]),
        queries=[{"name": "x", "query": "foo", "description": "d"}],
    )
    data = await collector.collect()
    await collector.close()
    slim = collector.persist_view(data)["queries"][0]
    assert len(slim["series"]) == 10 and "query" not in slim and "description" not in slim
