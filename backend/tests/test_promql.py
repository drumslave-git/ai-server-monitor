from __future__ import annotations

import json
from typing import Any

import httpx
import pytest

from ai_server_monitor.promql import PromQL, PromQLError, normalize, parse_value


def make(handler: Any, **kwargs: Any) -> PromQL:
    return PromQL("http://prom.test:9090/", transport=httpx.MockTransport(handler), **kwargs)


def ok(result_type: str, result: Any, **extra: Any) -> httpx.Response:
    return httpx.Response(
        200,
        json={"status": "success", "data": {"resultType": result_type, "result": result}, **extra},
    )


def test_parse_value_keeps_non_finite_as_strings() -> None:
    assert parse_value("1.5") == 1.5
    assert parse_value("NaN") == "NaN"
    assert parse_value("+Inf") == "+Inf"
    assert parse_value("-Inf") == "-Inf"
    assert parse_value("abc") == "abc"
    json.dumps(parse_value("NaN"), allow_nan=False)  # must stay strict-JSON serializable


def test_normalize_scalar_string_and_vector() -> None:
    assert normalize({"resultType": "scalar", "result": [1.0, "42"]}).series == [
        {"labels": {}, "value": 42.0}
    ]
    assert normalize({"resultType": "string", "result": [1.0, "hi"]}).series[0]["value"] == "hi"
    vector = normalize(
        {
            "resultType": "vector",
            "result": [
                {"metric": {"job": "a"}, "value": [1.0, "1"]},
                {"metric": {"job": "b"}, "value": [1.0, "NaN"]},
            ],
        }
    )
    assert vector.total == 2 and not vector.truncated
    assert vector.series == [
        {"labels": {"job": "a"}, "value": 1.0},
        {"labels": {"job": "b"}, "value": "NaN"},
    ]


def test_normalize_caps_series_and_points() -> None:
    raw = [{"metric": {"i": str(i)}, "value": [0, str(i)]} for i in range(10)]
    capped = normalize({"resultType": "vector", "result": raw}, max_series=3)
    assert len(capped.series) == 3 and capped.total == 10 and capped.truncated

    values = [[float(t), str(t)] for t in range(100)]
    matrix = normalize(
        {"resultType": "matrix", "result": [{"metric": {}, "values": values}]}, max_points=5
    )
    points = matrix.series[0]["values"]
    assert len(points) == 5
    assert points[0] == [0.0, 0.0] and points[-1] == [99.0, 99.0]


def test_normalize_rejects_unknown_type() -> None:
    with pytest.raises(PromQLError):
        normalize({"resultType": "weird", "result": []})


async def test_instant_query_sends_params_and_auth() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return ok("vector", [{"metric": {"__name__": "up"}, "value": [1, "0"]}])

    client = make(handler, token="s3cret", timeout=7)
    result = await client.instant("up == 0", at=1700000000)
    await client.close()
    request = seen[0]
    assert request.url.path == "/api/v1/query"
    assert request.url.params["query"] == "up == 0"
    assert request.url.params["time"] == "1700000000"
    assert request.url.params["timeout"] == "7s"
    assert request.headers["Authorization"] == "Bearer s3cret"
    assert result.to_dict()["series"] == [{"labels": {"__name__": "up"}, "value": 0.0}]


async def test_basic_auth_and_base_path() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return ok("scalar", [1, "3"])

    client = PromQL(
        "http://gw.test/prometheus/",
        username="bob",
        password="pw",
        transport=httpx.MockTransport(handler),
    )
    await client.instant("vector(3)")
    await client.close()
    assert seen[0].url.path == "/prometheus/api/v1/query"
    assert seen[0].headers["Authorization"].startswith("Basic ")


async def test_range_query() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/v1/query_range"
        assert request.url.params["step"] == "30"
        return ok("matrix", [{"metric": {"a": "b"}, "values": [[1, "1"], [2, "2"]]}])

    client = make(handler)
    result = await client.range("x", start=1, end=100, step=30)
    assert result.result_type == "matrix"
    assert result.series[0]["values"] == [[1.0, 1.0], [2.0, 2.0]]
    with pytest.raises(PromQLError):
        await client.range("x", start=10, end=5)
    await client.close()


async def test_prometheus_error_reply() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            400, json={"status": "error", "errorType": "bad_data", "error": "parse error"}
        )

    client = make(handler)
    with pytest.raises(PromQLError, match="bad_data: parse error") as info:
        await client.instant("{{")
    assert not info.value.unreachable
    await client.close()


async def test_non_json_and_http_errors() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.params["query"] == "html":
            return httpx.Response(200, text="<html>login</html>")
        return httpx.Response(401, text="unauthorized")

    client = make(handler)
    with pytest.raises(PromQLError, match="not a Prometheus API reply"):
        await client.instant("html")
    with pytest.raises(PromQLError, match="HTTP 401: unauthorized"):
        await client.instant("x")
    await client.close()


async def test_unreachable_and_timeout_are_flagged() -> None:
    def refuse(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused")

    def slow(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("slow")

    for handler in (refuse, slow):
        client = make(handler)
        with pytest.raises(PromQLError) as info:
            await client.instant("up")
        assert info.value.unreachable
        await client.close()


async def test_warnings_are_kept() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return ok("vector", [], warnings=["partial response"])

    client = make(handler)
    result = await client.instant("up")
    assert result.warnings == ["partial response"]
    assert result.to_dict()["warnings"] == ["partial response"]
    await client.close()
