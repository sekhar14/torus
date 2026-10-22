"""Thesis weak points, filing diffs, and risk-factor changes on watcher events."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

from conftest import GraphFactory
from edgar_support import ACME_CIK, ACME_FILINGS, ARCHIVE, FILINGS, FakeFetcher, acme_routes
from torus.core.challenge import weak_points
from torus.core.clock import ManualClock
from torus.core.filing_diff import diff_sections, sentences, summarize
from torus.core.impact import ingest_event
from torus.core.models import (
    Alias,
    AliasType,
    ClaimEvidence,
    ClaimType,
    EntityKind,
    EventKind,
    EvidenceKind,
    RelationType,
    Stance,
    Support,
    Verdict,
)
from torus.core.store import SQLiteBackend
from torus.sources.edgar_docs import decode, html_to_text, previous_filing, split_items
from torus.watchers import run_watchers
from torus.watchers.edgar import parse_submissions
from torus.watchers.http import RateLimiter


def test_weak_points(store: SQLiteBackend, graph: GraphFactory, clock: ManualClock) -> None:
    nvda, tsm, hynix = graph.company("NVDA"), graph.company("TSM"), graph.company("HYNIX")
    store.record_relation(tsm.id, RelationType.SUPPLIES, nvda.id, 0.9)
    store.record_relation(hynix.id, RelationType.SUPPLIES, nvda.id, 0.8)  # unmentioned
    news = store.record_evidence("https://news.example.com/x")
    denial = store.record_evidence("https://news.example.com/denial")
    filing = store.record_evidence("https://sec.gov/x", kind=EvidenceKind.FILING)
    solid = store.record_claim("TSMC makes NVDA GPUs", ClaimType.FACT, 0.9, [tsm.id], [filing.id])
    thin = store.record_claim(
        "NVDA lead times are 52 weeks",
        ClaimType.METRIC,
        0.45,
        [nvda.id],
        [
            ClaimEvidence(evidence_id=news.id, support=Support.SUPPORTS),
            ClaimEvidence(evidence_id=denial.id, support=Support.CONTRADICTS),
        ],
        valid_to=clock.now() + timedelta(days=20),
    )
    guess = store.record_claim("Demand lasts", ClaimType.OPINION, 0.6, [nvda.id])
    thesis = store.record_thesis(
        "NVDA supply-bound",
        "s",
        Stance.BULLISH,
        0.7,
        subject_entity_id=nvda.id,
        assumptions=[solid.id, thin.id, guess.id],
        triggers=[("Hyperscaler capex cuts", None)],
    )
    bear = store.record_thesis("NVDA peaks", "s", Stance.BEARISH, 0.4, subject_entity_id=nvda.id)
    store.record_rejected_hypothesis("Intel wins NVDA foundry", "no capacity", [nvda.id])
    [alert] = ingest_event(store, EventKind.NEWS, "NVDA delay", clock.now(), [nvda.id]).alerts[:1]
    store.resolve_alert(alert.id, Verdict.WEAKENS, "delay hurts", confidence=0.6)

    wp = weak_points(store, thesis.id, clock.now())
    issues = {a.text: a.issues for a in wp.assumptions}
    assert issues["TSMC makes NVDA GPUs"] == []
    assert set(issues["NVDA lead times are 52 weeks"]) == {
        "a single non-primary source",
        "low confidence (0.45)",
        f"expires {(clock.now() + timedelta(days=20)):%Y-%m-%d}",
        "recorded evidence contradicts it",
    }
    assert issues["Demand lasts"] == ["no source recorded", "a opinion, not a fact"]
    assert wp.assumptions[0].text == "NVDA lead times are 52 weeks"  # most issues first
    assert [d.name for d in wp.unwatched_dependencies] == ["HYNIX Inc"]
    assert wp.text_only_triggers == ["Hyperscaler capex cuts"]
    kinds = {(v.kind, v.id) for v in wp.opposing_views}
    assert ("thesis", bear.id) in kinds and ("alert", alert.id) in kinds
    assert any(v.kind == "ruled_out" for v in wp.opposing_views)
    assert wp.issue_count == 8


def _risk(fixture: str) -> str:
    return split_items(html_to_text(decode((FILINGS / fixture).read_bytes())), "10-K")["1A"]


def test_diff_sections_on_fixture_filings() -> None:
    diff = diff_sections(_risk("acme-10k-2024.htm"), _risk("acme-10k-2025.htm"))
    assert diff.added == ["Our gross margin could decline if HBM memory prices rise."]
    assert diff.removed == ["Demand for gaming products is seasonal."]
    [changed] = diff.changed
    assert changed.before == "We depend on two foundries for our wafers."
    assert changed.after == "We depend on a single foundry for all of our wafers."
    assert diff.unchanged == 2 and not diff.truncated
    text = summarize(diff)
    assert text.startswith("Risk factors: 1 new, 1 reworded, 1 dropped")
    assert "HBM memory prices" in text
    assert summarize(
        diff_sections(
            "Same sentence that is long enough to count here.",
            "Same sentence that is long enough to count here.",
        )
    ).endswith("no changes from the previous filing.")


def test_sentences_ignore_tables_page_numbers_and_join_breaks() -> None:
    text = (
        "Our supply depends on\n12\nTable of Contents\none foundry in Taiwan today.\n"
        "Revenue | 1,000 | 2,000"
    )
    assert sentences(text) == ["Our supply depends on one foundry in Taiwan today."]


def test_previous_filing() -> None:
    import json

    from edgar_support import acme_submissions

    _, filings = parse_submissions(json.loads(acme_submissions()))
    latest_10k = next(f for f in filings if f.form == "10-K")
    previous = previous_filing(latest_10k, filings)
    assert previous is not None and previous.accession == "0000999001-25-000012"
    assert previous_filing(previous, filings) is None


def test_watcher_adds_risk_factor_changes(store: SQLiteBackend, tmp_path: Path) -> None:
    acme = store.upsert_entity(
        EntityKind.COMPANY,
        "ACME CHIPS INC",
        canonical_key=f"cik:{ACME_CIK}",
        aliases=[Alias(alias="ACME", alias_type=AliasType.TICKER)],
    )
    store.watch_entity(acme.id)
    store.set_setting("sec_contact", "me@example.com")
    limiter = RateLimiter(0.0, monotonic=lambda: 0.0, sleep=lambda s: None)
    clock = ManualClock(datetime(2026, 2, 21, 9, 0, tzinfo=UTC))  # the day after the 10-K
    report = run_watchers(
        store,
        fetcher=FakeFetcher(acme_routes()),
        clock=clock,
        limiter=limiter,
        rss=False,
        home=tmp_path,
    )
    assert report.new_events == 2, report.sources  # the 10-K, and the 10-Q (no previous 10-Q)
    [event] = [e for e in store.events() if e.payload["form"] == "10-K"]
    assert event.summary is not None and "Risk factors: 1 new" in event.summary
    assert "HBM memory prices" in event.summary
    assert (tmp_path / "sources" / "edgar" / ACME_CIK).is_dir()
    assert len(ACME_FILINGS) == 3 and ARCHIVE

    store.set_setting("filing_diffs", "off")
    assert store.get_setting("filing_diffs") == "off"
