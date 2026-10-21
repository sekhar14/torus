from __future__ import annotations

from datetime import timedelta

from conftest import GraphFactory
from torus.core.clock import ManualClock
from torus.core.models import ClaimEntity, ClaimEntityRole, ClaimType, ThesisStatus
from torus.core.recall import recall, render_context
from torus.core.store import SQLiteBackend


def test_recall_returns_theses_claims_and_hypotheses(
    store: SQLiteBackend, graph: GraphFactory, clock: ManualClock
) -> None:
    nvda, tsm = graph.company("NVDA"), graph.company("TSM")
    a = graph.fact("TSMC CoWoS capacity constrains NVDA shipments", nvda, tsm)
    thesis = graph.thesis("NVDA supply-bound through 2027", nvda, a.id)
    store.record_rejected_hypothesis("Inventory build means demand slowdown", "WIP", [nvda.id])
    result = recall(store, clock, entity_ids=[nvda.id])
    assert [t.id for t in result.theses] == [thesis.id]
    assert result.theses[0].claims[0].text == a.text
    assert [c.id for c in result.claims] == [a.id]
    assert result.claims[0].evidence[0].uri.startswith("https://example.com/")
    assert result.claims[0].entities == ["NVDA Inc", "TSM Inc"]
    assert result.rejected_hypotheses[0].reason == "WIP"
    assert result.entities[0].tickers == ["NVDA"]
    assert result.entities[0].last_researched == a.recorded_at


def test_recall_orders_theses_at_risk_first(store: SQLiteBackend, graph: GraphFactory) -> None:
    nvda = graph.company("NVDA")
    first = graph.thesis("first", nvda)
    second = graph.thesis("second", nvda)
    confirmed = graph.thesis("third", nvda)
    store.update_thesis(first.id, reason="event", status=ThesisStatus.AT_RISK)
    store.update_thesis(confirmed.id, reason="done", status=ThesisStatus.CONFIRMED)
    result = recall(store, store._clock, entity_ids=[nvda.id])
    assert [t.title for t in result.theses] == ["first", "second", "third"]
    assert result.theses[0].status is ThesisStatus.AT_RISK
    assert second.id in {t.id for t in result.theses}


def test_recall_excludes_archived_unless_asked(store: SQLiteBackend, graph: GraphFactory) -> None:
    nvda = graph.company("NVDA")
    t = graph.thesis("old idea", nvda)
    store.update_thesis(t.id, reason="stale", status=ThesisStatus.ARCHIVED)
    assert recall(store, store._clock, entity_ids=[nvda.id]).theses == []
    assert recall(store, store._clock, entity_ids=[nvda.id], include_archived=True).theses


def test_recall_prefers_recent_and_confident(
    store: SQLiteBackend, graph: GraphFactory, clock: ManualClock
) -> None:
    nvda = graph.company("NVDA")
    old_sure = graph.fact("old but certain", nvda, confidence=0.95)
    clock.advance(days=365)
    new_unsure = graph.fact("new but shaky", nvda, confidence=0.3)
    new_sure = graph.fact("new and solid", nvda, confidence=0.9)
    result = recall(store, clock, entity_ids=[nvda.id])
    assert [c.id for c in result.claims] == [new_sure.id, new_unsure.id, old_sure.id]
    scores = [c.score for c in result.claims]
    assert scores == sorted(scores, reverse=True)


def test_recall_prefers_subject_over_mention(store: SQLiteBackend, graph: GraphFactory) -> None:
    nvda, amd = graph.company("NVDA"), graph.company("AMD")
    ev = graph.evidence()
    mention = store.record_claim(
        "AMD gains share, NVDA mentioned",
        ClaimType.FACT,
        0.9,
        entities=[amd.id, ClaimEntity(entity_id=nvda.id, role=ClaimEntityRole.MENTIONED)],
        evidence=[ev.id],
    )
    subject = graph.fact("NVDA guidance raised", nvda, confidence=0.6)
    result = recall(store, store._clock, entity_ids=[nvda.id])
    assert [c.id for c in result.claims] == [subject.id, mention.id]


def test_recall_excludes_superseded_and_retracted(
    store: SQLiteBackend, graph: GraphFactory
) -> None:
    nvda = graph.company("NVDA")
    old = graph.fact("v1", nvda)
    new = store.supersede_claim(old.id, reason="update", text="v2")
    gone = graph.fact("wrong", nvda)
    store.retract_claim(gone.id, reason="misread")
    result = recall(store, store._clock, entity_ids=[nvda.id])
    assert [c.id for c in result.claims] == [new.id]


def test_recall_as_of_past(store: SQLiteBackend, graph: GraphFactory, clock: ManualClock) -> None:
    nvda = graph.company("NVDA")
    claim = graph.fact("guide is $200B", nvda)
    thesis = graph.thesis("t", nvda, claim.id)
    before = clock.advance(days=1)
    clock.advance(days=1)
    store.supersede_claim(claim.id, reason="raised", text="guide is $210B")
    store.update_thesis(thesis.id, reason="event", status=ThesisStatus.AT_RISK, confidence=0.4)
    then = recall(store, clock, entity_ids=[nvda.id], as_of=before)
    assert [c.text for c in then.claims] == ["guide is $200B"]
    assert then.theses[0].status is ThesisStatus.ACTIVE
    assert then.theses[0].confidence == 0.7
    assert then.theses[0].claims[0].text == "guide is $200B"
    now = recall(store, clock, entity_ids=[nvda.id])
    assert now.theses[0].claims[0].text == "guide is $210B"
    assert now.theses[0].confidence == 0.4


def test_recall_as_of_before_thesis_existed(
    store: SQLiteBackend, graph: GraphFactory, clock: ManualClock
) -> None:
    nvda = graph.company("NVDA")
    early = clock.now()
    clock.advance(days=1)
    graph.thesis("later", nvda)
    assert recall(store, clock, entity_ids=[nvda.id], as_of=early).theses == []


def test_recall_by_query_only(store: SQLiteBackend, graph: GraphFactory) -> None:
    nvda, amd = graph.company("NVDA"), graph.company("AMD")
    hit = graph.fact("HBM supply is sold out through 2026", nvda)
    graph.fact("Gaming revenue flat", amd)
    thesis = store.record_thesis("HBM shortage persists", "s", "bullish", 0.6)
    result = recall(store, store._clock, query="What do we know about HBM supply?")
    assert [c.id for c in result.claims] == [hit.id]  # "flat" claim shares no terms
    assert [t.id for t in result.theses] == [thesis.id]  # matches "HBM" alone
    assert result.entities == []


def test_recall_query_ranks_more_matching_terms_first(
    store: SQLiteBackend, graph: GraphFactory
) -> None:
    nvda = graph.company("NVDA")
    partial = graph.fact("HBM pricing is stable", nvda)
    full = graph.fact("HBM supply is tight", nvda)
    result = recall(store, store._clock, query="HBM supply")
    assert [c.id for c in result.claims] == [full.id, partial.id]


def test_recall_merged_entity_id(store: SQLiteBackend, graph: GraphFactory) -> None:
    keep, dup = graph.company("NVDA"), graph.company("NVDA-dup")
    claim = graph.fact("on the duplicate", dup)
    store.merge_entities(keep.id, dup.id, reason="dup")
    result = recall(store, store._clock, entity_ids=[dup.id])
    assert [e.id for e in result.entities] == [keep.id]
    assert [c.id for c in result.claims] == [claim.id]


def test_recall_limit_reports_total(store: SQLiteBackend, graph: GraphFactory) -> None:
    nvda = graph.company("NVDA")
    for i in range(30):
        graph.fact(f"fact {i}", nvda)
    result = recall(store, store._clock, entity_ids=[nvda.id], limit=5)
    assert len(result.claims) == 5
    assert result.total_claims == 30


# -- rendering ------------------------------------------------------------------


def test_render_sections_in_priority_order(
    store: SQLiteBackend, graph: GraphFactory, clock: ManualClock
) -> None:
    nvda = graph.company("NVDA", name="NVIDIA Corp")
    a = graph.fact("TSMC CoWoS constrains supply", nvda)
    thesis = graph.thesis("Supply-bound growth", nvda, a.id)
    store.update_thesis(thesis.id, reason="8-K", status=ThesisStatus.AT_RISK, confidence=0.5)
    store.record_rejected_hypothesis("Demand slowdown", "inventory is WIP", [nvda.id])
    clock.advance(days=3)
    text = render_context(recall(store, clock, entity_ids=[nvda.id]), now=clock.now())
    lines = text.splitlines()
    assert lines[0] == "## Torus: prior research"
    assert lines[1].startswith("- NVIDIA Corp (NVDA) - last researched 2026-01-01 (3d ago)")
    order = [lines.index("### Theses"), lines.index("### Claims")]
    order.append(lines.index("### Rejected hypotheses"))
    assert order == sorted(order)
    assert (
        f"- [at_risk] Supply-bound growth - bullish, conf 0.50, 12m (thesis {thesis.id})" in lines
    )
    assert "  - assumes: TSMC CoWoS constrains supply" in lines
    assert any(f"[claim {a.id}] src: example.com" in line for line in lines)


def test_render_marks_retracted_assumption(store: SQLiteBackend, graph: GraphFactory) -> None:
    nvda = graph.company("NVDA")
    a = graph.fact("wrong assumption", nvda)
    graph.thesis("t", nvda, a.id)
    store.retract_claim(a.id, reason="wrong")
    text = render_context(recall(store, store._clock, entity_ids=[nvda.id]), now=store._clock.now())
    assert "  - assumes: (retracted)" in text


def test_render_respects_token_budget(
    store: SQLiteBackend, graph: GraphFactory, clock: ManualClock
) -> None:
    nvda = graph.company("NVDA")
    for i in range(300):
        graph.fact(f"claim number {i} about data-center demand and supply dynamics", nvda)
        clock.advance(minutes=1)
    result = recall(store, clock, entity_ids=[nvda.id], limit=100)
    text = render_context(result, now=clock.now(), budget_tokens=400)
    assert len(text) / 4 <= 400
    assert text.splitlines()[-1].startswith("(")
    assert "more items not shown" in text


def test_render_empty_is_empty_string(store: SQLiteBackend) -> None:
    assert render_context(recall(store, store._clock), now=store._clock.now()) == ""


def test_render_entity_without_research(store: SQLiteBackend, graph: GraphFactory) -> None:
    nvda = graph.company("NVDA")
    text = render_context(recall(store, store._clock, entity_ids=[nvda.id]), now=store._clock.now())
    assert "no research recorded yet" in text


def test_render_as_of_header(store: SQLiteBackend, graph: GraphFactory, clock: ManualClock) -> None:
    nvda = graph.company("NVDA")
    graph.fact("x", nvda)
    as_of = clock.now() + timedelta(hours=1)
    text = render_context(recall(store, clock, entity_ids=[nvda.id], as_of=as_of), now=as_of)
    assert text.startswith("## Torus: prior research (as of 2026-01-01)")
