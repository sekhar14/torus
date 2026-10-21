from __future__ import annotations

import pytest

from conftest import GraphFactory
from torus.core.clock import ManualClock
from torus.core.errors import EvidenceRequiredError, NotCurrentError, NotFoundError
from torus.core.models import (
    ClaimEntity,
    ClaimEntityRole,
    ClaimEvidence,
    ClaimStatus,
    ClaimType,
    Support,
)
from torus.core.store import SQLiteBackend


@pytest.mark.parametrize("claim_type", [ClaimType.FACT, ClaimType.METRIC])
def test_fact_and_metric_require_evidence(store: SQLiteBackend, claim_type: ClaimType) -> None:
    with pytest.raises(EvidenceRequiredError):
        store.record_claim("NVDA revenue grew 60%", claim_type, 0.9)
    assert store.counts()["claims"] == 0


def test_contradicting_evidence_alone_is_not_support(store: SQLiteBackend) -> None:
    ev = store.record_evidence("https://example.com/x")
    with pytest.raises(EvidenceRequiredError):
        store.record_claim(
            "x",
            ClaimType.FACT,
            0.5,
            evidence=[ClaimEvidence(evidence_id=ev.id, support=Support.CONTRADICTS)],
        )


def test_inference_needs_evidence_or_derivation(store: SQLiteBackend, graph: GraphFactory) -> None:
    nvda = graph.company("NVDA")
    with pytest.raises(EvidenceRequiredError):
        store.record_claim("NVDA margins will compress", ClaimType.INFERENCE, 0.6)
    base = graph.fact("NVDA gross margin fell 3pts QoQ", nvda)
    inf = store.record_claim(
        "NVDA margins are under pressure", ClaimType.INFERENCE, 0.6, derived_from=[base.id]
    )
    assert inf.derived_from == (base.id,)


@pytest.mark.parametrize("claim_type", [ClaimType.OPINION, ClaimType.FORECAST])
def test_opinions_and_forecasts_need_no_evidence(
    store: SQLiteBackend, claim_type: ClaimType
) -> None:
    claim = store.record_claim("AI capex keeps growing through 2027", claim_type, 0.5)
    assert claim.status is ClaimStatus.ACTIVE
    assert claim.lineage_id == claim.id


def test_claim_links_roundtrip(store: SQLiteBackend, graph: GraphFactory) -> None:
    nvda, tsm = graph.company("NVDA"), graph.company("TSM")
    ev = graph.evidence("https://example.com/call")
    session = store.start_session("claude-code", "s1")
    claim = store.record_claim(
        "TSMC CoWoS capacity constrains NVDA data-center shipments",
        ClaimType.FACT,
        0.85,
        entities=[nvda.id, ClaimEntity(entity_id=tsm.id, role=ClaimEntityRole.OBJECT)],
        evidence=[ev.id],
        session_id=session.id,
    )
    got = store.get_claim(claim.id)
    assert {(e.entity_id, e.role) for e in got.entities} == {
        (nvda.id, ClaimEntityRole.SUBJECT),
        (tsm.id, ClaimEntityRole.OBJECT),
    }
    assert got.evidence == (ClaimEvidence(evidence_id=ev.id, support=Support.SUPPORTS),)
    assert got.session_id == session.id
    assert [c.id for c in store.claims(entity_id=tsm.id)] == [claim.id]


@pytest.mark.parametrize(
    "kwargs",
    [
        {"entities": ["missing-entity"]},
        {"evidence": ["missing-evidence"]},
        {"derived_from": ["missing-claim"]},
        {"session_id": "missing-session"},
    ],
)
def test_unknown_references_rejected_atomically(
    store: SQLiteBackend, kwargs: dict[str, object]
) -> None:
    with pytest.raises(NotFoundError):
        store.record_claim("x", ClaimType.OPINION, 0.5, **kwargs)  # type: ignore[arg-type]
    assert store.counts()["claims"] == 0


@pytest.mark.parametrize("confidence", [-0.1, 1.1])
def test_confidence_bounds(store: SQLiteBackend, confidence: float) -> None:
    with pytest.raises(ValueError):
        store.record_claim("x", ClaimType.OPINION, confidence)


def test_empty_text_rejected(store: SQLiteBackend) -> None:
    with pytest.raises(ValueError):
        store.record_claim("   ", ClaimType.OPINION, 0.5)


def test_supersede_carries_over_unchanged_fields(
    store: SQLiteBackend, graph: GraphFactory, clock: ManualClock
) -> None:
    nvda = graph.company("NVDA")
    old = graph.fact("NVDA FY26 revenue guide is $200B", nvda)
    clock.advance(days=1)
    new = store.supersede_claim(old.id, reason="guidance raised", text="NVDA FY26 guide is $210B")
    assert new.lineage_id == old.lineage_id
    assert new.supersedes_id == old.id
    assert new.entities == old.entities
    assert new.evidence == old.evidence
    assert new.claim_type is ClaimType.FACT
    old_now = store.get_claim(old.id)
    assert old_now.status is ClaimStatus.SUPERSEDED
    assert old_now.change_reason == "guidance raised"
    assert old_now.superseded_at == new.recorded_at


def test_cannot_supersede_or_retract_non_current(store: SQLiteBackend, graph: GraphFactory) -> None:
    old = graph.fact("x", graph.company("NVDA"))
    store.supersede_claim(old.id, reason="r", text="y")
    with pytest.raises(NotCurrentError):
        store.supersede_claim(old.id, reason="r2", text="z")
    with pytest.raises(NotCurrentError):
        store.retract_claim(old.id, reason="r3")


def test_retract_removes_from_current_view(store: SQLiteBackend, graph: GraphFactory) -> None:
    nvda = graph.company("NVDA")
    claim = graph.fact("NVDA is buying back $50B", nvda)
    retracted = store.retract_claim(claim.id, reason="misread the release")
    assert retracted.status is ClaimStatus.RETRACTED
    assert store.claims(entity_id=nvda.id) == []
    assert store.current_claim(claim.lineage_id) is None


def test_claims_filter_by_type_and_limit(store: SQLiteBackend, graph: GraphFactory) -> None:
    nvda = graph.company("NVDA")
    graph.fact("a", nvda)
    graph.fact("b", nvda)
    store.record_claim("c", ClaimType.OPINION, 0.5, entities=[nvda.id])
    assert len(store.claims(entity_id=nvda.id, claim_types=[ClaimType.FACT])) == 2
    assert len(store.claims(entity_id=nvda.id, limit=1)) == 1
