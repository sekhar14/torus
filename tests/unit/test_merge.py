from __future__ import annotations

import pytest

from conftest import GraphFactory
from torus.core.errors import NotFoundError
from torus.core.models import AliasType, EntityKind, RelationType
from torus.core.store import SQLiteBackend


def test_merge_moves_everything_and_redirects(store: SQLiteBackend, graph: GraphFactory) -> None:
    keep = graph.company("NVDA", name="NVIDIA Corp", cik="0001045810")
    dup = store.upsert_entity(
        EntityKind.COMPANY, "Nvidia", attrs={"note": "from news"}, canonical_key=None
    )
    store.add_alias(dup.id, "Team Green", AliasType.ABBREVIATION)
    tsm = graph.company("TSM")

    claim = graph.fact("Nvidia ships Rubin in 2026", dup)
    both = graph.fact("NVDA and Nvidia are the same", keep, dup)
    rel = store.record_relation(tsm.id, RelationType.SUPPLIES, dup.id, 0.9)
    loop = store.record_relation(keep.id, RelationType.COMPETES_WITH, dup.id, 0.5)
    thesis = store.record_thesis(
        "t", "s", "bullish", 0.6, subject_entity_id=dup.id, triggers=[("x", dup.id)]
    )
    hyp = store.record_rejected_hypothesis("h", "r", entity_ids=[dup.id, keep.id])

    merged = store.merge_entities(keep.id, dup.id, reason="same company")

    assert merged.id == keep.id
    assert {"Nvidia", "Team Green", "NVDA"} <= {a.alias for a in merged.aliases}
    assert merged.attrs == {"note": "from news"}
    assert store.get_entity(dup.id).id == keep.id  # redirect
    assert store.live_entity_id(dup.id) == keep.id
    assert {c.id for c in store.claims(entity_id=keep.id)} == {claim.id, both.id}
    assert [e.entity_id for e in store.get_claim(both.id).entities] == [keep.id]
    assert [r.id for r in store.relations(keep.id, direction="in")] == [rel.id]
    assert store.get_relation(loop.id).superseded_at is not None  # self-loop closed
    t = store.get_thesis(thesis.id)
    assert t.subject_entity_id == keep.id and t.triggers[0].entity_id == keep.id
    assert store.rejected_hypotheses(entity_id=keep.id)[0].entity_ids == (keep.id,)
    assert store.rejected_hypotheses(entity_id=keep.id)[0].id == hyp.id
    assert store.counts()["entities"] == 2


def test_writes_with_merged_id_land_on_survivor(store: SQLiteBackend, graph: GraphFactory) -> None:
    keep, dup = graph.company("NVDA"), graph.company("NVDA2")
    store.merge_entities(keep.id, dup.id, reason="dup")
    claim = graph.fact("late write using old id", dup)
    assert [e.entity_id for e in claim.entities] == [keep.id]
    assert store.claims(entity_id=dup.id) == [claim]


def test_canonical_key_transfers_when_survivor_has_none(store: SQLiteBackend) -> None:
    keep = store.upsert_entity(EntityKind.COMPANY, "Nvidia")
    dup = store.upsert_entity(EntityKind.COMPANY, "NVIDIA CORP", canonical_key="cik:0001045810")
    store.merge_entities(keep.id, dup.id, reason="dup")
    found = store.get_entity_by_key("cik:0001045810")
    assert found is not None and found.id == keep.id


def test_merge_chains_redirect(store: SQLiteBackend) -> None:
    a = store.upsert_entity(EntityKind.COMPANY, "A")
    b = store.upsert_entity(EntityKind.COMPANY, "B")
    c = store.upsert_entity(EntityKind.COMPANY, "C")
    store.merge_entities(b.id, a.id, reason="r")
    store.merge_entities(c.id, b.id, reason="r")
    assert store.live_entity_id(a.id) == c.id


def test_merge_rejects_self_and_unknown(store: SQLiteBackend, graph: GraphFactory) -> None:
    nvda = graph.company("NVDA")
    with pytest.raises(ValueError):
        store.merge_entities(nvda.id, nvda.id, reason="r")
    with pytest.raises(NotFoundError):
        store.merge_entities(nvda.id, "missing", reason="r")


def test_failed_merge_changes_nothing(store: SQLiteBackend, graph: GraphFactory) -> None:
    nvda = graph.company("NVDA")
    before = store.counts()
    with pytest.raises(NotFoundError):
        store.merge_entities("missing", nvda.id, reason="r")
    assert store.counts() == before
    assert store.get_entity(nvda.id) == nvda
