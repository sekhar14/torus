"""Graph audit findings and thesis horizon dates."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from conftest import GraphFactory
from torus.cli import main
from torus.core.audit import audit
from torus.core.clock import ManualClock
from torus.core.horizon import parse_horizon
from torus.core.models import ClaimType, EvidenceKind, RelationType, Stance
from torus.core.starter import seed
from torus.core.store import SQLiteBackend

START = datetime(2026, 1, 15, tzinfo=UTC)


@pytest.mark.parametrize(
    ("horizon", "due"),
    [
        ("12m", datetime(2027, 1, 15, tzinfo=UTC)),
        ("18 months", datetime(2027, 7, 15, tzinfo=UTC)),
        ("2y", datetime(2028, 1, 15, tzinfo=UTC)),
        ("next 2 quarters", datetime(2026, 7, 15, tzinfo=UTC)),
        ("90 days", datetime(2026, 4, 15, tzinfo=UTC)),
        ("FY2026", datetime(2026, 12, 31, 23, 59, 59, tzinfo=UTC)),
        ("FY27", datetime(2027, 12, 31, 23, 59, 59, tzinfo=UTC)),
        ("Q3 2026", datetime(2026, 9, 30, 23, 59, 59, tzinfo=UTC)),
        ("3Q26", datetime(2026, 9, 30, 23, 59, 59, tzinfo=UTC)),
        ("H1 2027", datetime(2027, 6, 30, 23, 59, 59, tzinfo=UTC)),
        ("2026-06", datetime(2026, 6, 30, 23, 59, 59, tzinfo=UTC)),
        ("2026-06-15", datetime(2026, 6, 15, 23, 59, 59, tzinfo=UTC)),
        ("soon", None),
        ("2026-13", None),
        (None, None),
    ],
)
def test_parse_horizon(horizon: str | None, due: datetime | None) -> None:
    assert parse_horizon(horizon, START) == due


def test_horizon_date_recorded_and_changed(
    store: SQLiteBackend, graph: GraphFactory, clock: ManualClock
) -> None:
    nvda = graph.company("NVDA")
    thesis = graph.thesis("NVDA 12m", nvda)  # horizon "12m"
    assert thesis.horizon_date == clock.now().replace(year=2027)
    clock.advance(days=10)
    changed = store.set_thesis_horizon(thesis.id, "FY2028")
    assert changed.horizon == "FY2028" and changed.horizon_date is not None
    assert changed.horizon_date.year == 2028


def _kinds(store: SQLiteBackend, now: datetime) -> dict[str, int]:
    return audit(store, now).counts


def test_findings(store: SQLiteBackend, graph: GraphFactory, clock: ManualClock) -> None:
    nvda, tsm = graph.company("NVDA"), graph.company("TSM")
    web = store.record_evidence("https://news.example.com/cowos")
    filing = store.record_evidence("https://sec.gov/tsm-20f", kind=EvidenceKind.FILING)
    expiring = store.record_claim(
        "NVDA guides FY26 revenue of $130bn",
        ClaimType.FORECAST,
        0.8,
        entities=[nvda.id],
        evidence=[web.id],
        valid_to=clock.now() + timedelta(days=30),
    )
    thin = store.record_claim(
        "TSMC CoWoS capacity doubles in 2026", ClaimType.FACT, 0.6, [tsm.id, nvda.id], [web.id]
    )
    primary = store.record_claim(
        "TSMC makes all NVDA data-center GPUs", ClaimType.FACT, 0.9, [tsm.id], [filing.id]
    )
    opinion = store.record_claim("AI demand stays strong", ClaimType.OPINION, 0.5, [nvda.id])
    store.record_claim(
        "NVDA gross margin will be 75%", ClaimType.INFERENCE, 0.5, [nvda.id], derived_from=[thin.id]
    )
    store.record_thesis(
        title="NVDA bullish 6m",
        statement="Supply relief drives upside",
        stance=Stance.BULLISH,
        confidence=0.7,
        horizon="6m",
        subject_entity_id=nvda.id,
        assumptions=[expiring.id, thin.id, primary.id, opinion.id],
    )
    graph.company("ORPH")
    assert _kinds(store, clock.now()) == {
        "single_source_assumption": 2,  # the forecast and the CoWoS fact
        "opinion_assumption": 1,
        "orphan_entity": 1,
    }
    store.retract_claim(thin.id, "wrong")
    assert _kinds(store, clock.now())["retracted_basis"] == 1

    later = clock.now() + timedelta(days=200)
    counts = _kinds(store, later)
    assert counts["expired_assumption"] == 1
    assert counts["past_horizon"] == 1
    assert counts["stale_thesis"] == 1
    report = audit(store, later)
    assert [f.severity.value for f in report.findings][:2] == ["high", "high"]
    assert report.by_severity["high"] == 2


def test_possible_conflicts(store: SQLiteBackend, graph: GraphFactory) -> None:
    nvda = graph.company("NVDA", "NVIDIA")
    graph.fact("NVIDIA data-center revenue was $41bn last quarter", nvda)
    graph.fact("NVIDIA data-center revenue was $39bn last quarter", nvda)
    graph.fact("NVIDIA opened an office in Taipei", nvda)
    graph.fact("NVIDIA gaming revenue rose sharply", nvda)
    graph.fact("NVIDIA gaming revenue fell sharply", nvda)
    conflicts = [
        f for f in audit(store, datetime.now(UTC)).findings if f.kind == "possible_conflict"
    ]
    assert len(conflicts) == 2
    assert all(len(f.object_ids) == 2 for f in conflicts)


def test_unconfirmed_starter_links(
    store: SQLiteBackend, graph: GraphFactory, reference: object
) -> None:
    seed(store, "semis")
    nvda = store.get_entity_by_key("ticker:NVDA")
    assert nvda is not None
    graph.thesis("NVDA supply", nvda)
    starter = [
        f for f in audit(store, datetime.now(UTC)).findings if f.kind == "unconfirmed_starter_link"
    ]
    assert starter and all(f.object_type == "relation" for f in starter)
    # Confirming a link (research re-records it) clears its finding.
    tsm = store.get_entity_by_key("ticker:TSM")
    assert tsm is not None
    store.record_relation(tsm.id, RelationType.SUPPLIES, nvda.id, 0.9)
    after = [
        f for f in audit(store, datetime.now(UTC)).findings if f.kind == "unconfirmed_starter_link"
    ]
    assert len(after) == len(starter) - 1


def test_cli_audit(torus_home: object, capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["init"]) == 0
    assert main(["audit"]) == 0
    assert "healthy" in capsys.readouterr().out
    assert main(["audit", "--json"]) == 0


def test_cli_audit_summarises_many_low_findings(
    torus_home: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert main(["init"]) == 0
    with SQLiteBackend.open(torus_home / "torus.db") as store:
        seed(store, "semis")
        nvda = store.get_entity_by_key("ticker:NVDA")
        assert nvda is not None
        GraphFactory(store).thesis("NVDA supply", nvda)
    capsys.readouterr()
    assert main(["audit"]) == 0
    out = capsys.readouterr().out
    assert out.count("Unconfirmed starter link:") == 3
    assert "more unconfirmed starter link finding(s)" in out and "--all" in out
    assert main(["audit", "--all"]) == 0
    assert capsys.readouterr().out.count("Unconfirmed starter link:") > 3
