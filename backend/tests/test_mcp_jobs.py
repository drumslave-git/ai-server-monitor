"""Jobs with MCP domains (fake MCP server with 46 tools): a run gets exactly the enabled tools
of its domains, nothing to classify or load; MCP results count as looked and as evidence;
"missing tools" fails the run; writes are filed as approvals; a down server is named; the
domain list in the API, the form's Classify call and `job_save`."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi import FastAPI

from ai_server_monitor.context import AppContext
from ai_server_monitor.jobs.api import router as jobs_router
from ai_server_monitor.jobs.runner import RAN_NO_COMMAND
from ai_server_monitor.jobs.tools import job_tools
from ai_server_monitor.mcp_client.api import router as mcp_router

from .mcp_fake import FakeMcp
from .test_chat import call
from .test_mcp_chat import start_app
from .test_mcp_turns import mcp_llm, mcp_tools, offered, settings

JOB = {
    "slug": "heating",
    "name": "Heating",
    "prompt": "Check the kitchen temperature with the climate tools.",
    "interval_s": 3600,
}


def result(status: str = "ok", summary: str = "Fine.", **extra: Any) -> dict[str, Any]:
    return call("final_result", {"status": status, "summary": summary, "findings": [], **extra})


@pytest.fixture
async def fake46() -> AsyncIterator[FakeMcp]:
    server = FakeMcp("streamable_http", extra_tools=40)
    await server.start()
    yield server
    await server.stop()


def client(app: AppContext) -> httpx.AsyncClient:
    api = FastAPI()
    api.include_router(jobs_router)
    api.include_router(mcp_router)
    api.state.ctx = app
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=api), base_url="http://t")


async def sorted_app(app: AppContext) -> None:
    await app.domains.refresh(app.settings.mcp.servers, app.sort_model())


async def test_a_run_gets_exactly_its_domains_tools(tmp_path: Path, fake46: FakeMcp) -> None:
    llm = mcp_llm(
        call("home_get_temperature", {"room": "kitchen"}),
        call(
            "final_result",
            {
                "status": "warn",
                "summary": "Warm.",
                "findings": [
                    {"severity": "warn", "title": "Warm", "evidence": ["21.5 C in kitchen"]}
                ],
            },
            "c2",
        ),
        picks=None,
    )
    async for app in start_app(tmp_path, llm, settings(fake46.url)):
        await sorted_app(app)
        await app.jobs.store.create({**JOB, "domains": ["climate"]}, created_by="user")
        run = await app.jobs.run("heating", "manual")
        first = llm.requests[0]
        assert mcp_tools(first) == {"home_get_temperature", "home_set_heating"}
        assert "load_capability" not in offered(first)
        assert llm.classify_requests == []  # no classification at run time
        assert run["status"] == "warn", run  # an MCP call alone counts as having looked
        assert run["findings"][0]["verified"] is True  # MCP output is evidence
        assert run["mcp"] == {"domains": ["climate"], "unsorted": [], "unreachable": {}}
        # a builtin job ships without domains and gets no MCP tools
        builtin = await app.jobs.store.get("docker")
        assert builtin is not None and builtin["domains"] == []


async def test_missing_tools_fail_the_run(tmp_path: Path, fake46: FakeMcp) -> None:
    llm = mcp_llm(
        call("home_get_temperature", {"room": "kitchen"}),
        result(summary="Checked the kitchen.", missing_tools="No tool reads the boiler pressure."),
        picks=None,
    )
    async for app in start_app(tmp_path, llm, settings(fake46.url)):
        await sorted_app(app)
        await app.jobs.store.create({**JOB, "domains": ["climate"]}, created_by="user")
        run = await app.jobs.run("heating", "manual")
        assert run["status"] == "error"
        assert run["error"] == "missing tools: No tool reads the boiler pressure."
        assert run["summary"] == "Checked the kitchen."


async def test_a_write_is_filed_for_approval_and_the_run_finishes(
    tmp_path: Path, fake46: FakeMcp
) -> None:
    llm = mcp_llm(
        call("home_set_heating", {"room": "kitchen", "on": True}),
        result(summary="Asked to turn the heating on."),
        picks=None,
    )
    async for app in start_app(tmp_path, llm, settings(fake46.url)):
        await sorted_app(app)
        await app.jobs.store.create({**JOB, "domains": ["climate"]}, created_by="user")
        run = await app.jobs.run("heating", "manual")
        assert run["status"] == "ok", run
        assert not any(name == "set_heating" for name, _ in fake46.calls)
        [pending] = await app.actions.pending()
        assert pending["action"] == "mcp_call" and pending["source"] == "job:heating"
        assert "approval" in json.dumps(llm.requests[1]["messages"][-1]).lower()


async def test_a_down_server_is_named_and_does_not_fail_the_run(
    tmp_path: Path, fake46: FakeMcp
) -> None:
    llm = mcp_llm(
        result(summary="Could not reach the sensors."),
        result(summary="Still nothing to call."),  # after the look-first retry
        picks=None,
    )
    async for app in start_app(tmp_path, llm, settings(fake46.url)):
        await sorted_app(app)
        await app.jobs.store.create({**JOB, "domains": ["climate"]}, created_by="user")
        await fake46.stop()
        run = await app.jobs.run("heating", "manual")
        await fake46.start()
        # the server being down did not end the run; it failed only because it called nothing
        assert run["status"] == "error" and run["error"] == RAN_NO_COMMAND
        assert "home" in run["mcp"]["unreachable"]
        assert "unavailable this run" in json.dumps(llm.requests[0]["messages"])
        assert mcp_tools(llm.requests[0]) == set()


async def test_domains_in_the_api_classify_and_job_save(tmp_path: Path, fake46: FakeMcp) -> None:
    llm = mcp_llm(picks=["climate"])
    async for app in start_app(tmp_path, llm, settings(fake46.url)):
        await sorted_app(app)
        async with client(app) as http:
            r = await http.post("/api/jobs", json={**JOB, "domains": ["nope"]})
            assert r.status_code == 422
            assert "unknown domain nope (existing domains: bulk, climate, misc)" in r.text
            r = await http.post("/api/jobs", json={**JOB, "domains": ["climate"]})
            assert r.status_code == 201 and r.json()["domains"] == ["climate"]
            r = await http.post("/api/jobs/classify-domains", json={"prompt": JOB["prompt"]})
            assert r.json() == {"domains": ["climate"]}
            assert (
                "Request:\n" + JOB["prompt"] in llm.classify_requests[-1]["messages"][-1]["content"]
            )
            # a domain a job uses cannot be deleted
            page = (await http.get("/api/mcp/tools")).json()
            climate = next(d for d in page["domains"] if d["name"] == "climate")
            assert climate["used_by"] == ["Heating"]
            r = await http.delete(f"/api/mcp/domains/{climate['id']}")
            assert r.status_code == 409 and "used by: Heating" in r.text
            # a builtin with domains is edited; reset empties them again
            r = await http.put("/api/jobs/docker", json={"domains": ["bulk"]})
            assert r.json()["domains"] == ["bulk"] and r.json()["edited"]
            r = await http.post("/api/jobs/docker/reset")
            assert r.json()["domains"] == [] and not r.json()["edited"]
        save = next(t for t in job_tools(app, "s1") if t.name == "job_save")
        with pytest.raises(Exception, match="existing domains: bulk, climate, misc"):
            await save.handler({"slug": "heating", "domains": ["nope"]})
        out = await save.handler({"slug": "heating", "domains": ["bulk", "climate"]})
        assert out["domains"] == ["bulk", "climate"]


def test_a_missing_tools_failure_keeps_its_sentence_in_the_headline() -> None:
    from ai_server_monitor.jobs.status import headline

    run = {"status": "error", "error": "missing tools: No tool reads the boiler pressure."}
    assert headline(run, "Heating") == (
        "Heating failed: missing tools: No tool reads the boiler pressure."
    )


async def test_classify_offers_only_domains_with_an_enabled_tool(
    tmp_path: Path, fake46: FakeMcp
) -> None:
    llm = mcp_llm(picks=[])
    async for app in start_app(tmp_path, llm, settings(fake46.url, chosen=["get_temperature"])):
        await sorted_app(app)
        async with client(app) as http:
            page = (await http.get("/api/mcp/tools")).json()
            assert page["sorting"] is False and len(page["domains"]) == 3
            r = await http.post("/api/jobs/classify-domains", json={"prompt": JOB["prompt"]})
            assert r.json() == {"domains": []}
            offered_to_model = llm.classify_requests[-1]["messages"][-1]["content"]
            assert "- climate:" in offered_to_model
            assert "- bulk:" not in offered_to_model and "- misc:" not in offered_to_model
