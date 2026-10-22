"""What-if analysis and thesis memos."""

from __future__ import annotations

from pathlib import Path

import pytest

from conftest import GraphFactory
from torus.cli import main
from torus.core.clock import ManualClock
from torus.core.impact import ingest_event
from torus.core.memo import build_memo, render_html, render_markdown, save_memo
from torus.core.models import (
    ClaimType,
    EventKind,
    EvidenceKind,
    RelationType,
    Stance,
    ThesisClaimRole,
    Verdict,
)
from torus.core.store import SQLiteBackend
from torus.core.whatif import what_if


@pytest.fixture
def thesis(store: SQLiteBackend, graph: GraphFactory, clock: ManualClock) -> str:
    nvda, tsm, amd = (
        graph.company("NVDA", "NVIDIA"),
        graph.company("TSM", "TSMC"),
        graph.company("AMD"),
    )
    store.record_relation(tsm.id, RelationType.SUPPLIES, nvda.id, 0.9)
    store.record_relation(amd.id, RelationType.COMPETES_WITH, nvda.id, 0.8)
    filing = store.record_evidence(
        "https://www.sec.gov/nvda-10k",
        kind=EvidenceKind.FILING,
        title="NVIDIA 10-K FY2025",
        quote="We rely on TSMC to manufacture our GPUs | all of them",
    )
    supply = store.record_claim(
        "TSMC manufactures all NVIDIA data-center GPUs",
        ClaimType.FACT,
        0.9,
        [nvda.id, tsm.id],
        [filing.id],
    )
    view = store.record_claim(
        "Packaging is the binding constraint",
        ClaimType.INFERENCE,
        0.6,
        [nvda.id],
        derived_from=[supply.id],
    )
    risk = store.record_claim("AMD MI400 wins share", ClaimType.FORECAST, 0.4, [amd.id])
    t = store.record_thesis(
        title="NVIDIA stays supply-bound through FY2026",
        statement="Demand exceeds CoWoS supply all year",
        stance=Stance.BULLISH,
        confidence=0.7,
        horizon="FY2026",
        subject_entity_id=nvda.id,
        assumptions=[supply.id, view.id],
        risks=[risk.id],
        triggers=[("TSMC CoWoS capacity slips", tsm.id)],
    )
    store.record_rejected_hypothesis("Samsung wins NVDA packaging", "no qualification", [nvda.id])
    clock.advance(days=3)
    [alert] = ingest_event(
        store, EventKind.FILING, "TSMC 8-K: agreement", clock.now(), [tsm.id], source="manual"
    ).alerts
    store.resolve_alert(alert.id, Verdict.SUPPORTS, "more | capacity", confidence=0.75)
    return t.id


def test_what_if_reaches_and_explains(
    store: SQLiteBackend, clock: ManualClock, thesis: str
) -> None:
    tsm = store.get_entity_by_key("ticker:TSM")
    assert tsm is not None
    result = what_if(store, [tsm.id], "TSMC fab outage: CoWoS capacity slips", clock.now())
    assert result.materiality == 0.8 and result.materiality_reason == "operational disruption"
    [hit] = result.hits
    assert hit.would_alert and hit.hit.thesis_id == thesis
    assert hit.hit.matched_trigger == "TSMC CoWoS capacity slips"
    assert store.events(limit=10)[0].title == "TSMC 8-K: agreement"  # nothing recorded

    store.set_setting("alert_threshold", "5")
    near = what_if(store, [tsm.id], "TSMC publishes sustainability report", clock.now())
    assert [h.would_alert for h in near.hits] in ([], [False])


def test_memo_traces_claims_to_sources(
    store: SQLiteBackend, clock: ManualClock, thesis: str, tmp_path: Path
) -> None:
    memo = build_memo(store, thesis, clock.now())
    assert memo.subject == "NVIDIA" and memo.horizon_date is not None
    assert [c.text for c in memo.claims[ThesisClaimRole.ASSUMPTION.value]] == [
        "TSMC manufactures all NVIDIA data-center GPUs",
        "Packaging is the binding constraint",
    ]
    text = render_markdown(memo)
    assert "## What it rests on" in text and "## Risks" in text
    assert "[NVIDIA 10-K FY2025](https://www.sec.gov/nvda-10k)" in text
    assert "derived from: TSMC manufactures all NVIDIA data-center GPUs" in text
    assert "## What would change our mind" in text and "TSMC CoWoS capacity slips" in text
    assert "| supports | more \\| capacity |" in text
    assert "Samsung wins NVDA packaging" in text
    assert "TSMC supplies NVIDIA" in text
    assert "not financial advice" in text

    page = render_html(memo)
    assert page.startswith("<!doctype html>") and "<table>" in page
    assert '<a href="https://www.sec.gov/nvda-10k">' in page
    assert "more | capacity" in page and "<script" not in page

    md, html_path = save_memo(memo, tmp_path)
    assert md.name.startswith("nvidia-stays-supply-bound") and html_path.suffix == ".html"


def test_memo_escapes_html(store: SQLiteBackend, graph: GraphFactory, clock: ManualClock) -> None:
    acme = graph.company("ACME")
    t = store.record_thesis(
        "<script>alert(1)</script>",
        "x [y](javascript:alert(1))",
        Stance.NEUTRAL,
        0.5,
        subject_entity_id=acme.id,
    )
    page = render_html(build_memo(store, t.id, clock.now()))
    assert "<script>" not in page and "javascript:" not in page.split("<main>")[1].split("href")[0]
    assert 'href="javascript' not in page


def test_cli_whatif_and_memo(torus_home: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["init"]) == 0
    with SQLiteBackend.open(torus_home / "torus.db") as store:
        nvda = GraphFactory(store).company("NVDA")
        t = GraphFactory(store).thesis("NVDA thesis", nvda)
    assert main(["whatif", "NVIDIA recalls GPUs", "NVDA"]) == 0
    assert "ALERT" in capsys.readouterr().out
    assert main(["memo", t.id]) == 0
    assert "memos" in capsys.readouterr().out
    assert main(["memo", "nope"]) == 1
