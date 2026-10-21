"""Contract tests for the MCP server, through a real MCP client (in memory)."""

from __future__ import annotations

import json
import os
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest
from mcp import Client
from mcp.types import CallToolResult, TextContent

from conftest import FIXTURES
from torus.config import CaptureMode
from torus.core.clock import ManualClock
from torus.core.reference import ReferenceData
from torus.core.store import SQLiteBackend
from torus.mcp.server import TorusTools, build_server

pytestmark = pytest.mark.anyio

SNAPSHOT = FIXTURES / "mcp_tools_snapshot.json"
EXPECTED_TOOLS = {
    "recall",
    "get_entity",
    "search",
    "timeline",
    "what_changed",
    "upsert_entity",
    "merge_entities",
    "record_evidence",
    "record_claim",
    "supersede_claim",
    "retract_claim",
    "record_relation",
    "record_thesis",
    "update_thesis",
    "record_rejected_hypothesis",
    "next_distillation",
    "complete_distillation",
    "add_event",
    "impact",
    "list_alerts",
    "resolve_alert",
    "watch",
    "unwatch",
    "list_watches",
    "neighbors",
    "find_paths",
}


@pytest.fixture
def tools(store: SQLiteBackend, reference: ReferenceData, clock: ManualClock) -> TorusTools:
    return TorusTools(store, reference, clock=clock, harness="test", external_session_id="s-1")


@pytest.fixture
async def client(tools: TorusTools) -> AsyncIterator[Client]:
    async with Client(build_server(tools)) as c:
        yield c


def _text(result: CallToolResult) -> str:
    return "\n".join(c.text for c in result.content if isinstance(c, TextContent))


async def call(client: Client, tool: str, /, **arguments: Any) -> dict[str, Any]:
    result = await client.call_tool(tool, arguments)
    assert not result.is_error, _text(result)
    assert result.structured_content is not None
    return result.structured_content


async def call_error(client: Client, tool: str, /, **arguments: Any) -> str:
    result = await client.call_tool(tool, arguments)
    assert result.is_error, result.structured_content
    return _text(result)


async def test_lists_expected_tools_with_annotations(client: Client) -> None:
    listed = (await client.list_tools()).tools
    assert {t.name for t in listed} == EXPECTED_TOOLS
    for tool in listed:
        assert tool.description, tool.name
        assert tool.annotations is not None
        read_only = tool.name in {
            "recall",
            "get_entity",
            "search",
            "timeline",
            "what_changed",
            "impact",
            "list_alerts",
            "list_watches",
            "neighbors",
            "find_paths",
        }
        assert tool.annotations.read_only_hint is read_only, tool.name


async def test_tool_schemas_match_snapshot(client: Client) -> None:
    """Catches accidental breaking changes to the tool contract.

    After an intended change, regenerate with TORUS_UPDATE_SNAPSHOTS=1.
    """
    listed = (await client.list_tools()).tools
    current = {
        t.name: {"input": t.input_schema, "output": t.output_schema}
        for t in sorted(listed, key=lambda t: t.name)
    }
    if os.environ.get("TORUS_UPDATE_SNAPSHOTS") == "1":
        SNAPSHOT.write_text(json.dumps(current, indent=2, sort_keys=True) + "\n", "utf-8")
    assert SNAPSHOT.exists(), "run once with TORUS_UPDATE_SNAPSHOTS=1 to create the snapshot"
    assert current == json.loads(SNAPSHOT.read_text("utf-8"))


async def test_server_instructions(client: Client) -> None:
    assert client.instructions is not None and "recall" in client.instructions
    assert "Capture mode: balanced" in client.instructions  # default


async def test_server_instructions_follow_capture_mode(
    store: SQLiteBackend, reference: ReferenceData, clock: ManualClock
) -> None:
    tools = TorusTools(store, reference, clock=clock, mode=CaptureMode.LIVE)
    async with Client(build_server(tools)) as c:
        assert c.instructions is not None and "Capture mode: live" in c.instructions


async def test_record_flow_roundtrip(client: Client, store: SQLiteBackend) -> None:
    ev = await call(
        client,
        "record_evidence",
        uri="https://investor.nvidia.com/q3",
        title="NVDA Q3 call",
        quote="supply constrained by CoWoS",
        published_at="2025-11-19",
    )
    assert ev["published_at"].startswith("2025-11-19T00:00:00")
    claim = await call(
        client,
        "record_claim",
        text="TSMC CoWoS capacity constrains NVDA data-center shipments",
        claim_type="fact",
        confidence=0.85,
        entities=["NVDA"],
        mentioned=["TSM"],
        evidence_ids=[ev["id"]],
    )
    assert claim["session_id"] is not None
    thesis = await call(
        client,
        "record_thesis",
        title="NVDA supply-bound through 2027",
        statement="Supply, not demand, limits growth.",
        stance="bullish",
        confidence=0.7,
        subject="$NVDA",
        horizon="12m",
        assumption_claim_ids=[claim["id"]],
        triggers=[{"description": "TSMC cuts CoWoS guide", "entity": "TSM"}],
    )
    rel = await call(
        client, "record_relation", source="TSM", rel="SUPPLIES", target="NVDA", confidence=0.9
    )
    assert rel["rel"] == "SUPPLIES"

    recalled = await call(client, "recall", entities=["nvda"])
    assert [t["id"] for t in recalled["theses"]] == [thesis["id"]]
    assert recalled["claims"][0]["evidence"][0]["uri"] == "https://investor.nvidia.com/q3"
    assert recalled["entities"][0]["canonical_key"] == "cik:0001045810"

    # NVDA and TSM came from SEC reference data and now live in the graph.
    assert store.get_entity_by_key("cik:0001046179") is not None
    session = store.get_session(claim["session_id"])
    assert (session.harness, session.external_session_id) == ("test", "s-1")


async def test_recall_detects_entities_in_query(client: Client) -> None:
    ev = await call(client, "record_evidence", uri="https://example.com/a")
    await call(
        client,
        "record_claim",
        text="Blackwell ramp ahead of plan",
        claim_type="fact",
        confidence=0.8,
        entities=["NVDA"],
        evidence_ids=[ev["id"]],
    )
    recalled = await call(client, "recall", query="Any update on $NVDA ramps?")
    assert [e["name"] for e in recalled["entities"]] == ["NVIDIA CORP"]
    assert recalled["claims"][0]["text"] == "Blackwell ramp ahead of plan"


async def test_recall_as_of(client: Client, clock: ManualClock) -> None:
    ev = await call(client, "record_evidence", uri="https://example.com/a")
    claim = await call(
        client,
        "record_claim",
        text="guide $200B",
        claim_type="fact",
        confidence=0.8,
        entities=["NVDA"],
        evidence_ids=[ev["id"]],
    )
    before = clock.advance(days=1).isoformat()
    clock.advance(days=1)
    await call(client, "supersede_claim", claim_id=claim["id"], reason="raised", text="guide $210B")
    then = await call(client, "recall", entities=["NVDA"], as_of=before)
    assert [c["text"] for c in then["claims"]] == ["guide $200B"]


async def test_invalid_input_returns_tool_error_not_crash(client: Client) -> None:
    ev = await call(client, "record_evidence", uri="https://example.com/a")
    missing_evidence = await call_error(
        client, "record_claim", text="x", claim_type="fact", confidence=0.5, entities=["NVDA"]
    )
    assert "evidence" in missing_evidence
    bad_enum = await call_error(
        client, "record_claim", text="x", claim_type="rumour", confidence=0.5
    )
    assert "claim_type" in bad_enum
    out_of_range = await call_error(
        client, "record_claim", text="x", claim_type="opinion", confidence=1.5
    )
    assert "confidence" in out_of_range
    unknown = await call_error(
        client,
        "record_claim",
        text="x",
        claim_type="fact",
        confidence=0.5,
        entities=["Totally Unknown Widgets"],
        evidence_ids=[ev["id"]],
    )
    assert "upsert_entity" in unknown
    missing_claim = await call_error(client, "retract_claim", claim_id="nope", reason="r")
    assert "not found" in missing_claim
    # The server keeps working after errors.
    await call(client, "search", query="anything")


async def test_ambiguous_entity_lists_candidates(client: Client) -> None:
    error = await call_error(client, "get_entity", entity="Twin Name Holdings")
    assert "ambiguous" in error
    assert "cik:0002011641" in error and "cik:0002011642" in error


async def test_update_thesis_lifecycle(client: Client) -> None:
    thesis = await call(
        client, "record_thesis", title="t", statement="s", stance="neutral", confidence=0.5
    )
    at_risk = await call(
        client, "update_thesis", thesis_id=thesis["id"], reason="news", status="at_risk"
    )
    assert at_risk["status"] == "at_risk"
    await call(client, "update_thesis", thesis_id=thesis["id"], reason="dead", status="broken")
    error = await call_error(
        client, "update_thesis", thesis_id=thesis["id"], reason="revive", status="active"
    )
    assert "cannot move from broken to active" in error


async def test_upsert_and_merge_entities(client: Client) -> None:
    custom = await call(
        client, "upsert_entity", name="Team Green Labs", kind="company", aliases=["TGL"]
    )
    found = await call(client, "get_entity", entity="TGL")
    assert found["entity"]["id"] == custom["id"]
    nvda = await call(client, "get_entity", entity="NVDA")
    merged = await call(
        client, "merge_entities", keep="NVDA", duplicate=custom["id"], reason="same company"
    )
    assert merged["id"] == nvda["entity"]["id"]
    again = await call(client, "get_entity", entity="TGL")
    assert again["entity"]["id"] == nvda["entity"]["id"]


async def test_get_entity_lists_relations(client: Client) -> None:
    await call(
        client, "record_relation", source="TSM", rel="SUPPLIES", target="NVDA", confidence=0.9
    )
    view = await call(client, "get_entity", entity="NVDA")
    assert view["resolved_by"] == "ticker"
    [rel] = view["relations"]
    assert (rel["direction"], rel["rel"], rel["other_name"]) == (
        "in",
        "SUPPLIES",
        "TAIWAN SEMICONDUCTOR MANUFACTURING CO LTD",
    )


async def test_timeline_and_what_changed(client: Client, clock: ManualClock) -> None:
    start = clock.now().isoformat()
    ev = await call(client, "record_evidence", uri="https://example.com/a")
    claim = await call(
        client,
        "record_claim",
        text="v1",
        claim_type="fact",
        confidence=0.8,
        entities=["NVDA"],
        evidence_ids=[ev["id"]],
    )
    clock.advance(hours=1)
    await call(client, "retract_claim", claim_id=claim["id"], reason="wrong")
    log = await call(client, "timeline", entity="NVDA")
    assert [c["kind"] for c in log["changes"]] == ["claim_recorded", "claim_retracted"]
    changed = await call(client, "what_changed", since=start)
    kinds = [c["kind"] for c in changed["changes"]]
    assert kinds.count("entity_added") == 1 and "claim_retracted" in kinds


async def test_record_rejected_hypothesis_and_recall(client: Client) -> None:
    await call(
        client,
        "record_rejected_hypothesis",
        text="Inventory build means a demand slowdown",
        reason="Build is CoWoS WIP",
        entities=["NVDA"],
    )
    recalled = await call(client, "recall", entities=["NVDA"])
    assert recalled["rejected_hypotheses"][0]["reason"] == "Build is CoWoS WIP"


async def test_resources(client: Client) -> None:
    thesis = await call(
        client,
        "record_thesis",
        title="Supply-bound",
        statement="s",
        stance="bullish",
        confidence=0.7,
        subject="NVDA",
    )
    templates = {
        t.uri_template for t in (await client.list_resource_templates()).resource_templates
    }
    assert {"torus://entity/{ref}", "torus://thesis/{thesis_id}"} <= templates
    entity_md = (await client.read_resource("torus://entity/NVDA")).contents[0]
    assert "Supply-bound" in getattr(entity_md, "text", "")
    thesis_json = (await client.read_resource(f"torus://thesis/{thesis['id']}")).contents[0]
    assert json.loads(getattr(thesis_json, "text", "{}"))["history"][0]["reason"] == "created"
    digest = (await client.read_resource("torus://digest")).contents[0]
    assert "Theses at risk: 0" in getattr(digest, "text", "")


async def test_server_starts_via_cli(tmp_path: Path) -> None:
    """`torus serve` speaks MCP over stdio."""
    import sys

    from mcp import StdioServerParameters

    env = {**os.environ, "TORUS_HOME": str(tmp_path / "home")}
    params = StdioServerParameters(command=sys.executable, args=["-m", "torus", "serve"], env=env)
    async with Client(params) as c:
        names = {t.name for t in (await c.list_tools()).tools}
        assert names == EXPECTED_TOOLS
        result = await c.call_tool("search", {"query": "nothing yet"})
        assert not result.is_error
    assert (tmp_path / "home" / "torus.db").exists()


# -- M4.1: revising theses ---------------------------------------------------------


async def test_update_thesis_replaces_assumption_and_adds_trigger(client: Client) -> None:
    ev = await call(client, "record_evidence", uri="https://example.com/q3-call")
    old = await call(
        client,
        "record_claim",
        text="[demo] packaging constrains supply",
        claim_type="opinion",
        confidence=0.5,
        entities=["NVDA"],
    )
    thesis = await call(
        client,
        "record_thesis",
        title="supply-bound",
        statement="s",
        stance="bullish",
        confidence=0.65,
        subject="NVDA",
        assumption_claim_ids=[old["id"]],
    )
    new = await call(
        client,
        "record_claim",
        text="CEO: demand far exceeds CoWoS supply",
        claim_type="fact",
        confidence=0.9,
        entities=["TSM"],
        mentioned=["NVDA"],
        evidence_ids=[ev["id"]],
    )
    revised = await call(
        client,
        "update_thesis",
        thesis_id=thesis["id"],
        reason="swap demo assumption for sourced claim; add warning sign",
        confidence=0.6,
        add_claims=[{"claim_id": new["id"], "role": "assumption"}],
        remove_claim_ids=[old["id"]],
        add_triggers=[{"description": "TSMC says the CoWoS gap has closed", "entity": "TSM"}],
    )
    assert [c["claim_lineage_id"] for c in revised["claims"]] == [new["lineage_id"]]
    assert revised["confidence"] == 0.6
    assert [t["description"] for t in revised["triggers"]] == ["TSMC says the CoWoS gap has closed"]


async def test_record_relation_twice_keeps_one_link(client: Client) -> None:
    args = {"source": "TSM", "rel": "SUPPLIES", "target": "NVDA", "confidence": 0.95}
    first = await call(client, "record_relation", **args)
    second = await call(client, "record_relation", **args)
    assert second["id"] == first["id"]
    view = await call(client, "get_entity", entity="NVDA")
    assert len(view["relations"]) == 1


# -- M5: distillation ----------------------------------------------------------------


def _queue_research_session(store: SQLiteBackend, reference: ReferenceData) -> str:
    """A session the hooks saw: prompt entities, an agent record, sources, queued."""
    from torus.core.models import ClaimType, Stance
    from torus.core.resolve import Resolver
    from torus.hooks.handlers import DISTILL_KIND

    nvda = Resolver(store, reference).resolve("NVDA", create=True).entity
    assert nvda is not None
    store.record_thesis("NVDA supply-bound", "s", Stance.BULLISH, 0.6, subject_entity_id=nvda.id)
    transcript_path = FIXTURES / "transcripts" / "research_session.jsonl"
    session = store.start_session("claude-code", "sess-123", str(transcript_path))
    store.note_session_entities(session.id, [nvda.id])
    url = "https://www.trendforce.com/news/cowos-gap"
    ev = store.record_evidence(url, title="TrendForce")
    recorded = store.record_claim(
        "CEO says demand far exceeds CoWoS supply",
        ClaimType.FACT,
        0.9,
        [nvda.id],
        [ev.id],
        session_id=session.id,
    )
    store.note_session_record(session.id, "claim", recorded.id)
    store.add_source_candidate(session.id, "WebFetch", uri=url)
    store.add_source_candidate(session.id, "WebSearch", query="TSMC CoWoS capacity 2026")
    store.enqueue(
        DISTILL_KIND,
        f"distill:{session.id}",
        {"session_id": session.id, "capture_mode": "balanced"},
    )
    return session.id


async def test_next_distillation_empty_queue(client: Client) -> None:
    result = await call(client, "next_distillation")
    assert result == {"job": None, "remaining": 0}


async def test_distillation_job_contents(
    client: Client, store: SQLiteBackend, reference: ReferenceData
) -> None:
    session_id = _queue_research_session(store, reference)
    job = (await call(client, "next_distillation"))["job"]
    assert job["session_id"] == session_id
    assert job["attempt"] == 1
    assert job["entities"][0].startswith("NVIDIA CORP")
    assert [r["text"] for r in job["already_recorded"]] == [
        "CEO says demand far exceeds CoWoS supply"
    ]
    assert [t["title"] for t in job["theses"]] == ["NVDA supply-bound"]
    sources = {(s["tool"], s["uri"] or s["query"]): s["evidence_id"] for s in job["sources"]}
    assert sources[("WebFetch", "https://www.trendforce.com/news/cowos-gap")] is not None
    assert sources[("WebSearch", "TSMC CoWoS capacity 2026")] is None
    assert "Amkor is adding outsourced CoWoS-like capacity" in job["transcript"]
    assert "private reasoning" not in job["transcript"]


async def test_curator_records_are_attributed_to_the_distilled_session(
    client: Client, store: SQLiteBackend, reference: ReferenceData
) -> None:
    session_id = _queue_research_session(store, reference)
    job = (await call(client, "next_distillation"))["job"]
    claim = await call(
        client,
        "record_claim",
        text="Amkor adds outsourced CoWoS-like capacity",
        claim_type="opinion",
        confidence=0.5,
        entities=["NVDA"],
    )
    assert claim["session_id"] == session_id
    done = await call(
        client, "complete_distillation", queue_id=job["queue_id"], summary="1 claim added"
    )
    assert done == {"queue_id": job["queue_id"], "status": "done", "remaining": 0}
    after = await call(
        client, "record_claim", text="unrelated later note", claim_type="opinion", confidence=0.5
    )
    assert after["session_id"] != session_id


async def test_failed_distillation_goes_back_to_the_queue(
    client: Client, store: SQLiteBackend, reference: ReferenceData
) -> None:
    _queue_research_session(store, reference)
    job = (await call(client, "next_distillation"))["job"]
    out = await call(
        client,
        "complete_distillation",
        queue_id=job["queue_id"],
        summary="transcript unreadable",
        failed=True,
    )
    assert out["status"] == "pending" and out["remaining"] == 1
    error = await call_error(
        client, "complete_distillation", queue_id=job["queue_id"], summary="again"
    )
    assert "not leased" in error


# -- M6: events & alerts ----------------------------------------------------------------


async def _supply_chain(client: Client) -> dict[str, Any]:
    ev = await call(client, "record_evidence", uri="https://example.com/call")
    claim = await call(
        client,
        "record_claim",
        text="TSMC CoWoS capacity constrains NVIDIA shipments",
        claim_type="fact",
        confidence=0.8,
        entities=["NVDA", "TSM"],
        evidence_ids=[ev["id"]],
    )
    await call(
        client, "record_relation", source="TSM", rel="SUPPLIES", target="NVDA", confidence=0.9
    )
    return await call(
        client,
        "record_thesis",
        title="NVDA supply-bound",
        statement="s",
        stance="bullish",
        confidence=0.7,
        subject="NVDA",
        assumption_claim_ids=[claim["id"]],
    )


async def test_impact_dry_run_records_nothing(client: Client, store: SQLiteBackend) -> None:
    thesis = await _supply_chain(client)
    result = await call(client, "impact", entities=["TSM"])
    assert [h["thesis_id"] for h in result["hits"]] == [thesis["id"]]
    assert store.counts()["events"] == 0 and store.counts()["alerts"] == 0


async def test_add_event_review_and_resolve(client: Client) -> None:
    thesis = await _supply_chain(client)
    added = await call(
        client,
        "add_event",
        title="TSMC 8-K: CoWoS ramp delayed",
        occurred_at="2026-01-08",
        entities=["TSM"],
        kind="filing",
        uri="https://www.sec.gov/example-8k.htm",
    )
    assert added["created"] is True
    [view] = added["alerts"]
    assert view["thesis_title"] == "NVDA supply-bound"
    assert view["event"]["title"] == "TSMC 8-K: CoWoS ramp delayed"
    assert view["alert"]["paths"][0]["via"] == "assumption claim"

    again = await call(
        client,
        "add_event",
        title="dup",
        occurred_at="2026-01-08",
        entities=["TSM"],
        uri="https://www.sec.gov/example-8k.htm",
    )
    assert again["created"] is False and again["alerts"] == []

    listed = await call(client, "list_alerts")
    assert [a["alert"]["id"] for a in listed["alerts"]] == [view["alert"]["id"]]
    recalled = await call(client, "recall", entities=["NVDA"])
    assert recalled["alerts"][0]["id"] == view["alert"]["id"]

    resolved = await call(
        client,
        "resolve_alert",
        alert_id=view["alert"]["id"],
        verdict="weakens",
        rationale="Delay keeps supply tight longer; relief moves to 2027",
        confidence=0.55,
    )
    assert resolved["alert"]["status"] == "resolved"
    assert (resolved["thesis_status"], resolved["thesis_confidence"]) == ("at_risk", 0.55)
    assert (await call(client, "list_alerts"))["alerts"] == []
    history = await call(client, "timeline", entity="NVDA")
    assert "thesis_changed" in [c["kind"] for c in history["changes"]]
    assert thesis["id"] in {c["object_id"] for c in history["changes"]}


async def test_resolve_alert_needs_rationale(client: Client) -> None:
    await _supply_chain(client)
    added = await call(client, "add_event", title="x", occurred_at="2026-01-08", entities=["TSM"])
    error = await call_error(
        client,
        "resolve_alert",
        alert_id=added["alerts"][0]["alert"]["id"],
        verdict="supports",
        rationale="  ",
    )
    assert "rationale" in error


# -- M7: watchers ---------------------------------------------------------------------


async def test_watch_tools(client: Client) -> None:
    watched = await call(client, "watch", entity="NVDA", note="supply story")
    assert [(e["name"], e["reason"]) for e in watched["entities"]] == [("NVIDIA CORP", "watchlist")]
    await call(
        client,
        "record_thesis",
        title="t",
        statement="s",
        stance="bullish",
        confidence=0.6,
        subject="TSM",
    )
    listed = await call(client, "list_watches")
    reasons = {e["name"]: e["reason"] for e in listed["entities"]}
    assert reasons == {
        "NVIDIA CORP": "watchlist",
        "TAIWAN SEMICONDUCTOR MANUFACTURING CO LTD": "open thesis",
    }
    after = await call(client, "unwatch", entity="NVDA")
    assert [e["name"] for e in after["entities"]] == ["TAIWAN SEMICONDUCTOR MANUFACTURING CO LTD"]


# -- M8: graph queries & provenance ---------------------------------------------------


async def test_neighbors_and_find_paths(client: Client) -> None:
    for src, dst in (("ASML", "TSM"), ("TSM", "NVDA"), ("TSM", "AMD")):
        await call(
            client, "record_relation", source=src, rel="SUPPLIES", target=dst, confidence=0.9
        )
    sub = await call(client, "neighbors", entity="TSM", hops=1)
    assert {n["label"] for n in sub["nodes"]} == {
        "TAIWAN SEMICONDUCTOR MANUFACTURING CO LTD",
        "ASML HOLDING NV",
        "NVIDIA CORP",
        "ADVANCED MICRO DEVICES INC",
    }
    result = await call(client, "find_paths", source="ASML", target="AMD")
    assert result["texts"][0] == (
        "ASML HOLDING NV -SUPPLIES-> TAIWAN SEMICONDUCTOR MANUFACTURING CO LTD"
        " -SUPPLIES-> ADVANCED MICRO DEVICES INC"
    )


async def test_relation_source_is_curator_during_distillation(
    client: Client, store: SQLiteBackend, reference: ReferenceData
) -> None:
    _queue_research_session(store, reference)
    plain = await call(
        client, "record_relation", source="TSM", rel="SUPPLIES", target="NVDA", confidence=0.9
    )
    assert plain["source"] == "agent"
    job = (await call(client, "next_distillation"))["job"]
    curated = await call(
        client, "record_relation", source="AMD", rel="COMPETES_WITH", target="NVDA", confidence=0.9
    )
    assert curated["source"] == "curator"
    await call(client, "complete_distillation", queue_id=job["queue_id"], summary="ok")
