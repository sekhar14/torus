from __future__ import annotations

import pytest

from torus.core.models import RelationType
from torus.core.reference import ReferenceData
from torus.core.starter import available_packs, load_pack, seed
from torus.core.store import SQLiteBackend


@pytest.mark.parametrize("name", ["semis", "software", "banks", "energy", "pharma"])
def test_pack_is_well_formed(name: str) -> None:
    assert name in available_packs()
    pack = load_pack(name)
    refs = {e["ref"] for e in pack["entities"]}
    assert len(refs) == len(pack["entities"])
    seen = set()
    for src, rel, dst, confidence, note in pack["relations"]:
        assert src in refs and dst in refs and src != dst
        RelationType(rel)
        assert 0 < confidence <= 1 and note
        assert (src, rel, dst) not in seen
        seen.add((src, rel, dst))
    for entity in pack["entities"]:
        assert ("ticker" in entity) != ("key" in entity)
        assert entity["name"]


def test_unknown_pack() -> None:
    with pytest.raises(ValueError, match="available"):
        load_pack("nope")


def test_seed_is_idempotent_and_marks_provenance(
    store: SQLiteBackend, reference: ReferenceData
) -> None:
    pack = load_pack("semis")
    first = seed(store, "semis", reference)
    assert first.entities_created == len(pack["entities"])
    assert first.relations_added == len(pack["relations"])
    again = seed(store, "semis", reference)
    assert (again.entities_created, again.relations_added) == (0, 0)
    assert again.relations_kept == len(pack["relations"])
    sources = {r.source for r in store.relations()}
    assert sources == {"starter:semis@1"}
    nvda = store.get_entity_by_key("cik:0001045810")  # resolved through SEC reference data
    assert nvda is not None
    assert "Nvidia" in {a.alias for a in nvda.aliases}
    samsung = store.get_entity_by_key("name:samsung-electronics")
    assert samsung is not None and samsung.name == "Samsung Electronics Co Ltd"


def test_seed_never_overrides_research(store: SQLiteBackend, reference: ReferenceData) -> None:
    from torus.core.resolve import Resolver

    resolver = Resolver(store, reference)
    tsm = resolver.resolve("TSM", create=True).entity
    nvda = resolver.resolve("NVDA", create=True).entity
    assert tsm is not None and nvda is not None
    mine = store.record_relation(tsm.id, RelationType.SUPPLIES, nvda.id, 0.99)
    seed(store, "semis", reference)
    [current] = [
        r
        for r in store.relations(tsm.id, direction="out")
        if r.dst_id == nvda.id and r.rel is RelationType.SUPPLIES
    ]
    assert (current.id, current.source, current.confidence) == (mine.id, "agent", 0.99)


def test_seed_without_reference_keys_by_ticker(store: SQLiteBackend) -> None:
    report = seed(store, "semis")
    assert report.entities_created == len(load_pack("semis")["entities"])
    assert store.get_entity_by_key("ticker:NVDA") is not None


def test_agent_re_recording_a_starter_link_takes_ownership(
    store: SQLiteBackend, reference: ReferenceData
) -> None:
    seed(store, "semis", reference)
    tsm = store.get_entity_by_key("cik:0001046179")
    nvda = store.get_entity_by_key("cik:0001045810")
    assert tsm is not None and nvda is not None
    confirmed = store.record_relation(tsm.id, RelationType.SUPPLIES, nvda.id, 0.95)
    assert confirmed.source == "agent"
    history = [
        r
        for r in store.relations(nvda.id, direction="in", as_of=confirmed.recorded_at)
        if r.src_id == tsm.id
    ]
    assert [r.source for r in history] == ["agent"]
