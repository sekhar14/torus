"""Onboarding a coverage list, mapping jobs, and reading filings through MCP."""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest
from mcp import Client

from edgar_support import ACME_CIK, FakeFetcher, acme_routes
from torus.cli import main
from torus.core.clock import ManualClock
from torus.core.models import Alias, AliasType, EntityKind, QueueStatus
from torus.core.onboard import MAP_KIND, onboard, read_tickers, suggest_packs
from torus.core.reference import ReferenceData
from torus.core.store import SQLiteBackend
from torus.mcp.server import TorusTools, build_server


def test_read_tickers_from_csv_and_text() -> None:
    csv_text = "Symbol,Shares,Cost\nNVDA,10,100\n$amd,5,90\n,1,1\nNVDA,1,1\n"
    assert read_tickers(csv_text) == ["NVDA", "AMD"]
    assert read_tickers("NVDA, AMD\nTSM") == ["NVDA", "AMD", "TSM"]
    assert read_tickers("AAPL,1\nMSFT,2\n") == ["AAPL", "MSFT"]  # no header: first column
    assert read_tickers("") == []


def test_onboard_watches_queues_and_suggests(
    store: SQLiteBackend, reference: ReferenceData
) -> None:
    report = onboard(store, ["NVDA", "AMD", "NOPE123"], reference)
    assert [c.ref for c in report.companies] == ["NVDA", "AMD"]
    assert all(c.queued and c.created for c in report.companies)
    assert report.unresolved == [("NOPE123", "not in the graph or the SEC list")]
    assert {e.id for e in store.watchlist()} == {c.entity_id for c in report.companies}
    assert report.packs[0] == ("semis", 2)
    assert ("software", 1) in report.packs  # NVDA is in the software pack too
    jobs = store.queue_items(kind=MAP_KIND)
    assert len(jobs) == 2 and jobs[0].payload["forms"] == ["10-K", "20-F", "40-F"]

    again = onboard(store, ["NVDA"], reference)
    assert not again.companies[0].queued and not again.companies[0].created
    assert len(store.queue_items(kind=MAP_KIND)) == 2
    assert suggest_packs(store, []) == []


def test_cli_onboard(torus_home: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    portfolio = tmp_path / "portfolio.csv"
    portfolio.write_text("ticker,weight\nXOM,0.5\nCVX,0.5\n", encoding="utf-8")
    assert main(["onboard", "--file", str(portfolio), "--no-watch"]) == 0
    out = capsys.readouterr().out
    assert "Onboarded 2 companies; 2 mapping job(s) queued." in out
    assert "Suggested pack: energy" in out
    assert main(["onboard"]) == 2


# -- MCP ---------------------------------------------------------------------------


@pytest.fixture
def acme_tools(
    store: SQLiteBackend, reference: ReferenceData, clock: ManualClock, tmp_path: Path
) -> TorusTools:
    store.upsert_entity(
        EntityKind.COMPANY,
        "ACME CHIPS INC",
        canonical_key=f"cik:{ACME_CIK}",
        aliases=[Alias(alias="ACME", alias_type=AliasType.TICKER)],
    )
    store.set_setting("sec_contact", "me@example.com")
    return TorusTools(
        store,
        reference,
        clock=clock,
        harness="test",
        home=tmp_path / "home",
        fetcher=FakeFetcher(acme_routes()),
    )


@pytest.fixture
async def client(acme_tools: TorusTools) -> AsyncIterator[Client]:
    async with Client(build_server(acme_tools)) as c:
        yield c


async def call(client: Client, tool: str, /, **arguments: Any) -> dict[str, Any]:
    result = await client.call_tool(tool, arguments)
    assert not result.is_error, result.content
    assert result.structured_content is not None
    return result.structured_content


@pytest.mark.anyio
async def test_filing_text_by_section_and_page(client: Client) -> None:
    risks = await call(client, "filing_text", company="ACME", section="risk_factors")
    assert risks["form"] == "10-K" and risks["accession"] == "0000999001-26-000010"
    assert risks["section"] == "1A" and "single foundry" in risks["text"]
    assert {"business", "risk_factors", "mdna", "financials"} <= set(risks["sections"])
    assert risks["url"].endswith("acme-20251231.htm")

    previous = await call(client, "filing_text", company="ACME", section="1A", index=1)
    assert "two foundries" in previous["text"]

    paged = await call(client, "filing_text", company="ACME", max_chars=2000)
    assert paged["total_chars"] < 2000 and paged["next_offset"] is None

    quarterly = await call(client, "filing_text", company="ACME", form="10-Q", section="mdna")
    assert "35%" in quarterly["text"]

    missing = await client.call_tool("filing_text", {"company": "ACME", "section": "legal"})
    assert missing.is_error


@pytest.mark.anyio
async def test_mapping_job_records_curator_links(client: Client, store: SQLiteBackend) -> None:
    result = await call(client, "onboard", tickers=["ACME", "NVDA"])
    assert result["mapping_jobs_queued"] == 2
    assert result["suggested_packs"][0]["pack"] == "semis"

    first = await call(client, "next_mapping")
    job = first["job"]
    assert job["name"] == "ACME CHIPS INC" and job["cik"] == ACME_CIK
    assert first["remaining"] == 1
    link = await call(
        client, "record_relation", source="TSM", rel="SUPPLIES", target="ACME", confidence=0.9
    )
    assert store.get_relation(link["id"]).source == "curator"
    done = await call(client, "complete_mapping", queue_id=job["queue_id"], summary="1 link")
    assert done["status"] == "done" and done["remaining"] == 1

    after = await call(
        client, "record_relation", source="AMD", rel="COMPETES_WITH", target="ACME", confidence=0.7
    )
    assert store.get_relation(after["id"]).source == "agent"
    statuses = {i.status for i in store.queue_items(kind=MAP_KIND)}
    assert statuses == {QueueStatus.DONE, QueueStatus.PENDING}


@pytest.mark.anyio
async def test_seed_starter_pack_tool(client: Client) -> None:
    result = await call(client, "seed_starter_pack", pack="energy")
    assert result["pack"] == "starter:energy@1" and result["links_added"] == 26
    bad = await client.call_tool("seed_starter_pack", {"pack": "nope"})
    assert bad.is_error


@pytest.mark.anyio
async def test_filing_text_needs_contact(store: SQLiteBackend, acme_tools: TorusTools) -> None:
    store.set_setting("sec_contact", None)
    async with Client(build_server(acme_tools)) as c:
        result = await c.call_tool("filing_text", {"company": "ACME"})
    assert result.is_error
