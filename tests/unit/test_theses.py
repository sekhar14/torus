from __future__ import annotations

import pytest

from conftest import GraphFactory
from torus.core.clock import ManualClock
from torus.core.errors import InvalidTransitionError, NotCurrentError, NotFoundError
from torus.core.models import (
    THESIS_TRANSITIONS,
    ClaimType,
    Stance,
    ThesisClaimRole,
    ThesisStatus,
)
from torus.core.store import SQLiteBackend


def test_thesis_links_claims_with_roles(store: SQLiteBackend, graph: GraphFactory) -> None:
    nvda, tsm = graph.company("NVDA"), graph.company("TSM")
    a = graph.fact("TSMC CoWoS constrains NVDA supply", nvda, tsm)
    s = graph.fact("Hyperscaler capex up 40% YoY", nvda)
    r = store.record_claim("Custom ASICs could take share", ClaimType.OPINION, 0.4, [nvda.id])
    thesis = store.record_thesis(
        title="NVDA data-center growth continues",
        statement="Supply, not demand, is the constraint through 2027.",
        stance=Stance.BULLISH,
        confidence=0.7,
        horizon="12m",
        subject_entity_id=nvda.id,
        assumptions=[a.id],
        supporting=[s.id],
        risks=[r.id],
        triggers=[("TSMC guides CoWoS capacity down", tsm.id)],
    )
    roles = {(c.claim_lineage_id, c.role) for c in thesis.claims}
    assert roles == {
        (a.lineage_id, ThesisClaimRole.ASSUMPTION),
        (s.lineage_id, ThesisClaimRole.SUPPORTING),
        (r.lineage_id, ThesisClaimRole.RISK),
    }
    assert thesis.triggers[0].entity_id == tsm.id
    assert thesis.status is ThesisStatus.ACTIVE
    assert [h.reason for h in store.thesis_history(thesis.id)] == ["created"]


def test_thesis_follows_claim_correction(store: SQLiteBackend, graph: GraphFactory) -> None:
    nvda = graph.company("NVDA")
    a = graph.fact("v1", nvda)
    thesis = graph.thesis("t", nvda, a.id)
    v2 = store.supersede_claim(a.id, reason="update", text="v2")
    link = store.get_thesis(thesis.id).claims[0]
    current = store.current_claim(link.claim_lineage_id)
    assert current is not None and current.id == v2.id


def test_cannot_link_retracted_claim(store: SQLiteBackend, graph: GraphFactory) -> None:
    nvda = graph.company("NVDA")
    a = graph.fact("x", nvda)
    store.retract_claim(a.id, reason="wrong")
    with pytest.raises(NotCurrentError):
        graph.thesis("t", nvda, a.id)
    assert store.counts()["theses"] == 0


def test_link_unknown_claim_fails(store: SQLiteBackend, graph: GraphFactory) -> None:
    with pytest.raises(NotFoundError):
        graph.thesis("t", graph.company("NVDA"), "missing")


def test_theses_found_via_subject_or_claims(store: SQLiteBackend, graph: GraphFactory) -> None:
    nvda, tsm, amd = graph.company("NVDA"), graph.company("TSM"), graph.company("AMD")
    a = graph.fact("TSMC supplies NVDA", nvda, tsm)
    thesis = graph.thesis("t", nvda, a.id)
    assert [t.id for t in store.theses(entity_id=nvda.id)] == [thesis.id]
    assert [t.id for t in store.theses(entity_id=tsm.id)] == [thesis.id]
    assert store.theses(entity_id=amd.id) == []
    assert store.theses(statuses=[ThesisStatus.BROKEN]) == []


@pytest.mark.parametrize(
    ("path", "ok"),
    [
        ([ThesisStatus.AT_RISK, ThesisStatus.ACTIVE], True),
        ([ThesisStatus.AT_RISK, ThesisStatus.BROKEN, ThesisStatus.ARCHIVED], True),
        ([ThesisStatus.CONFIRMED, ThesisStatus.ARCHIVED], True),
        ([ThesisStatus.BROKEN, ThesisStatus.ACTIVE], False),
        ([ThesisStatus.ARCHIVED, ThesisStatus.ACTIVE], False),
        ([ThesisStatus.CONFIRMED, ThesisStatus.AT_RISK], False),
    ],
)
def test_thesis_status_transitions(
    store: SQLiteBackend, graph: GraphFactory, path: list[ThesisStatus], ok: bool
) -> None:
    thesis = graph.thesis("t", graph.company("NVDA"))
    *allowed, last = path
    for status in allowed:
        store.update_thesis(thesis.id, reason="step", status=status)
    if ok:
        assert store.update_thesis(thesis.id, reason="last", status=last).status is last
        assert len(store.thesis_history(thesis.id)) == len(path) + 1
    else:
        with pytest.raises(InvalidTransitionError):
            store.update_thesis(thesis.id, reason="last", status=last)
        assert len(store.thesis_history(thesis.id)) == len(path)


def test_archived_is_terminal() -> None:
    assert THESIS_TRANSITIONS[ThesisStatus.ARCHIVED] == frozenset()


def test_noop_update_writes_no_history(store: SQLiteBackend, graph: GraphFactory) -> None:
    thesis = graph.thesis("t", graph.company("NVDA"))
    store.update_thesis(thesis.id, reason="same", confidence=thesis.confidence)
    assert len(store.thesis_history(thesis.id)) == 1


def test_thesis_state_as_of(store: SQLiteBackend, graph: GraphFactory, clock: ManualClock) -> None:
    before_creation = clock.now()
    clock.advance(hours=1)
    thesis = graph.thesis("t", graph.company("NVDA"))
    created = clock.now()
    clock.advance(days=7)
    store.update_thesis(
        thesis.id,
        reason="TSMC 8-K: CoWoS delay",
        status=ThesisStatus.AT_RISK,
        confidence=0.5,
        alert_id="alert-1",
    )
    assert store.thesis_state_as_of(thesis.id, before_creation) is None
    then = store.thesis_state_as_of(thesis.id, created)
    assert then is not None and then.confidence == 0.7 and then.status is ThesisStatus.ACTIVE
    now = store.thesis_state_as_of(thesis.id, clock.now())
    assert now is not None and now.status is ThesisStatus.AT_RISK and now.alert_id == "alert-1"


def test_link_thesis_claim_later(store: SQLiteBackend, graph: GraphFactory) -> None:
    nvda = graph.company("NVDA")
    thesis = graph.thesis("t", nvda)
    risk = graph.fact("Export controls tightened", nvda)
    updated = store.link_thesis_claim(thesis.id, risk.id, ThesisClaimRole.RISK, weight=0.5)
    assert updated.claims[0].role is ThesisClaimRole.RISK
    assert updated.claims[0].weight == 0.5


def test_rejected_hypotheses(store: SQLiteBackend, graph: GraphFactory) -> None:
    nvda = graph.company("NVDA")
    hyp = store.record_rejected_hypothesis(
        "NVDA inventory build signals demand slowdown",
        reason="build is CoWoS-related WIP, not finished goods",
        entity_ids=[nvda.id],
    )
    assert store.rejected_hypotheses(entity_id=nvda.id) == [hyp]
    assert hyp.entity_ids == (nvda.id,)
