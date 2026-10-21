"""M4.1: revising what a thesis rests on, triggers, relation dedupe, session adoption."""

from __future__ import annotations

import pytest

from conftest import GraphFactory
from torus.core.clock import ManualClock
from torus.core.errors import NotFoundError
from torus.core.models import RelationType, ThesisClaimRole
from torus.core.recall import recall
from torus.core.store import SQLiteBackend


def test_replace_assumption_keeps_history(
    store: SQLiteBackend, graph: GraphFactory, clock: ManualClock
) -> None:
    nvda = graph.company("NVDA")
    demo = graph.fact("[demo] packaging constrains supply", nvda)
    thesis = graph.thesis("supply-bound", nvda, demo.id)
    before = clock.advance(days=1)
    clock.advance(days=1)
    sourced = graph.fact("CEO: demand far exceeds CoWoS supply (Q3 call)", nvda)

    revised = store.revise_thesis(
        thesis.id,
        reason="replace demo assumption with transcript claim",
        add_claims=[(sourced.id, ThesisClaimRole.ASSUMPTION, 1.0)],
        remove_claims=[demo.id],
    )

    assert [c.claim_lineage_id for c in revised.claims] == [sourced.lineage_id]
    then = store.thesis_links_as_of(thesis.id, before)
    assert [c.claim_lineage_id for c in then] == [demo.lineage_id]
    history = store.thesis_history(thesis.id)
    assert history[-1].reason == "replace demo assumption with transcript claim"
    assert history[-1].confidence == thesis.confidence  # links changed, not confidence


def test_recall_as_of_uses_links_of_that_time(
    store: SQLiteBackend, graph: GraphFactory, clock: ManualClock
) -> None:
    nvda = graph.company("NVDA")
    old = graph.fact("old assumption", nvda)
    thesis = graph.thesis("t", nvda, old.id)
    before = clock.advance(days=1)
    clock.advance(days=1)
    new = graph.fact("new assumption", nvda)
    store.revise_thesis(
        thesis.id,
        "swap",
        add_claims=[(new.id, ThesisClaimRole.ASSUMPTION, 1.0)],
        remove_claims=[old.id],
    )
    then = recall(store, clock, entity_ids=[nvda.id], as_of=before).theses[0]
    now = recall(store, clock, entity_ids=[nvda.id]).theses[0]
    assert [c.text for c in then.claims] == ["old assumption"]
    assert [c.text for c in now.claims] == ["new assumption"]


def test_relink_with_new_role_versions_the_link(store: SQLiteBackend, graph: GraphFactory) -> None:
    nvda = graph.company("NVDA")
    claim = graph.fact("x", nvda)
    thesis = graph.thesis("t", nvda, claim.id)
    revised = store.link_thesis_claim(thesis.id, claim.id, ThesisClaimRole.RISK)
    assert [(c.role, c.weight) for c in revised.claims] == [(ThesisClaimRole.RISK, 1.0)]
    assert len(store.thesis_history(thesis.id)) == 2


def test_relinking_identically_is_a_noop(store: SQLiteBackend, graph: GraphFactory) -> None:
    nvda = graph.company("NVDA")
    claim = graph.fact("x", nvda)
    thesis = graph.thesis("t", nvda, claim.id)
    store.revise_thesis(
        thesis.id, "again", add_claims=[(claim.id, ThesisClaimRole.ASSUMPTION, 1.0)]
    )
    assert len(store.thesis_history(thesis.id)) == 1


def test_unlink_unknown_claim_fails_atomically(store: SQLiteBackend, graph: GraphFactory) -> None:
    nvda = graph.company("NVDA")
    claim = graph.fact("x", nvda)
    other = graph.fact("not linked", nvda)
    thesis = graph.thesis("t", nvda, claim.id)
    with pytest.raises(NotFoundError):
        store.revise_thesis(thesis.id, "r", confidence=0.1, remove_claims=[other.id])
    assert store.get_thesis(thesis.id).confidence == thesis.confidence


def test_add_and_remove_triggers(store: SQLiteBackend, graph: GraphFactory) -> None:
    nvda, tsm = graph.company("NVDA"), graph.company("TSM")
    thesis = graph.thesis("t", nvda)
    added = store.revise_thesis(
        thesis.id,
        "new warning signs",
        add_triggers=[("TSMC says the CoWoS gap has closed", tsm.id), ("lead times shorten", None)],
    )
    assert [t.description for t in added.triggers] == [
        "TSMC says the CoWoS gap has closed",
        "lead times shorten",
    ]
    removed = store.revise_thesis(thesis.id, "obsolete", remove_triggers=[added.triggers[1].id])
    assert [t.entity_id for t in removed.triggers] == [tsm.id]
    with pytest.raises(NotFoundError):
        store.revise_thesis(thesis.id, "again", remove_triggers=[added.triggers[1].id])


def test_record_relation_reuses_current_link(store: SQLiteBackend, graph: GraphFactory) -> None:
    tsm, nvda = graph.company("TSM"), graph.company("NVDA")
    first = store.record_relation(tsm.id, RelationType.SUPPLIES, nvda.id, 0.95)
    same = store.record_relation(tsm.id, RelationType.SUPPLIES, nvda.id, 0.95)
    assert same.id == first.id
    claim = graph.fact("TSMC is NVIDIA's sole CoWoS supplier", tsm, nvda)
    revised = store.record_relation(tsm.id, RelationType.SUPPLIES, nvda.id, 0.9, claim_id=claim.id)
    assert revised.lineage_id == first.lineage_id and revised.id != first.id
    assert (revised.confidence, revised.claim_id) == (0.9, claim.id)
    current = store.relations(nvda.id, direction="in")
    assert [r.id for r in current] == [revised.id]
    other = store.record_relation(tsm.id, RelationType.COMPETES_WITH, nvda.id, 0.2)
    assert other.lineage_id != first.lineage_id


def test_adopt_into_session_only_takes_anonymous_records(
    store: SQLiteBackend, graph: GraphFactory
) -> None:
    nvda = graph.company("NVDA")
    mcp = store.start_session("claude-code")  # the MCP server's anonymous session
    hook = store.start_session("claude-code", "sess-1")
    other = store.start_session("claude-code", "sess-2")
    ev = graph.evidence()
    from torus.core.models import ClaimType

    by_mcp = store.record_claim("x", ClaimType.FACT, 0.8, [nvda.id], [ev.id], session_id=mcp.id)
    by_other = store.record_claim("y", ClaimType.FACT, 0.8, [nvda.id], [ev.id], session_id=other.id)
    assert store.adopt_into_session(hook.id, "claim", by_mcp.id) == "adopted"
    assert store.adopt_into_session(hook.id, "claim", by_mcp.id) == "own"
    assert store.adopt_into_session(hook.id, "claim", by_other.id) == "other"
    assert store.adopt_into_session(hook.id, "evidence", ev.id) == "untracked"
    assert store.adopt_into_session(hook.id, "claim", "missing") == "untracked"
    assert store.get_claim(by_mcp.id).session_id == hook.id
    assert store.get_claim(by_other.id).session_id == other.id
