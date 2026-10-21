from __future__ import annotations

from conftest import GraphFactory
from torus.core.store import SQLiteBackend


def test_fts_finds_claim_text(store: SQLiteBackend, graph: GraphFactory) -> None:
    claim = graph.fact("TSMC CoWoS capacity constrains shipments", graph.company("NVDA"))
    hits = store.search("cowos capacity", object_types=["claim"])
    assert [h.object_id for h in hits] == [claim.id]
    assert "[CoWoS]" in hits[0].snippet


def test_fts_only_current_versions(store: SQLiteBackend, graph: GraphFactory) -> None:
    old = graph.fact("guidance is 200 billion", graph.company("NVDA"))
    new = store.supersede_claim(old.id, reason="raised", text="guidance is 210 billion")
    hits = store.search("guidance", object_types=["claim"])
    assert [h.object_id for h in hits] == [new.id]
    store.retract_claim(new.id, reason="wrong")
    assert store.search("guidance", object_types=["claim"]) == []


def test_search_evidence_and_theses(store: SQLiteBackend, graph: GraphFactory) -> None:
    ev = store.record_evidence(
        "https://example.com/8k", title="TSMC 8-K", quote="advanced packaging expansion delayed"
    )
    thesis = graph.thesis("Packaging bottleneck persists", graph.company("NVDA"))
    types = {(h.object_type, h.object_id) for h in store.search("packaging")}
    assert ("evidence", ev.id) in types
    assert ("thesis", thesis.id) in types


def test_search_is_injection_safe(store: SQLiteBackend, graph: GraphFactory) -> None:
    graph.fact("NVDA beat estimates", graph.company("NVDA"))
    for q in ['"', "NEAR(", "a OR", "*", "col:val", "-", ""]:
        store.search(q)  # must not raise


def test_search_limit(store: SQLiteBackend, graph: GraphFactory) -> None:
    nvda = graph.company("NVDA")
    for i in range(5):
        graph.fact(f"memory pricing update {i}", nvda)
    assert len(store.search("memory pricing", limit=3)) == 3


def test_match_any_drops_question_words(store: SQLiteBackend, graph: GraphFactory) -> None:
    claim = graph.fact("CoWoS capacity doubles in 2026", graph.company("TSM"))
    assert store.search("what is the CoWoS outlook", object_types=["claim"]) == []
    hits = store.search("what is the CoWoS outlook", object_types=["claim"], match_any=True)
    assert [h.object_id for h in hits] == [claim.id]
    assert store.search("what is the", match_any=True) == []
