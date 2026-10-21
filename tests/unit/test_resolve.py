from __future__ import annotations

import pytest

from conftest import GraphFactory
from torus.core.models import AliasType, Entity, EntityKind
from torus.core.reference import ReferenceData
from torus.core.resolve import ResolutionStatus, Resolver
from torus.core.store import SQLiteBackend

NVDA_KEY = "cik:0001045810"


@pytest.mark.parametrize(
    ("ref", "method"),
    [
        ("NVDA", "ticker"),
        ("$nvda", "ticker"),
        ("0001045810", "cik"),
        ("1045810", "cik"),
        ("cik:0001045810", "cik"),
        ("NVIDIA CORP", "alias"),
        ("Nvidia Corporation", "name"),
        ("NVIDIA, Inc.", "name"),
    ],
)
def test_resolve_by_ticker_cik_name_alias(
    resolver: Resolver, seeded: dict[str, Entity], ref: str, method: str
) -> None:
    result = resolver.resolve(ref)
    assert result.status is ResolutionStatus.RESOLVED, result
    assert result.entity is not None and result.entity.id == seeded["NVDA"].id
    assert result.method == method
    assert result.canonical_key == NVDA_KEY


def test_resolve_by_entity_id_and_canonical_key(
    resolver: Resolver, seeded: dict[str, Entity]
) -> None:
    nvda = seeded["NVDA"]
    assert resolver.resolve(nvda.id).entity == nvda
    assert resolver.resolve(NVDA_KEY).entity == nvda


def test_resolve_extra_aliases(resolver: Resolver, seeded: dict[str, Entity]) -> None:
    assert resolver.resolve("TSMC").entity == seeded["TSM"]
    assert resolver.resolve("tsmc").entity == seeded["TSM"]
    assert resolver.resolve("Google").entity == seeded["GOOGL"]
    assert resolver.resolve("GOOG").entity == seeded["GOOGL"]


def test_reference_only_is_not_added_without_create(
    resolver: Resolver, store: SQLiteBackend
) -> None:
    result = resolver.resolve("TSLA")
    assert result.status is ResolutionStatus.REFERENCE
    assert result.reference is not None and result.reference.title == "Tesla, Inc."
    assert result.canonical_key == "cik:0001318605"
    assert store.counts()["entities"] == 0


def test_create_adds_company_with_all_tickers(resolver: Resolver, store: SQLiteBackend) -> None:
    result = resolver.resolve("BRK.B", create=True)
    assert result.status is ResolutionStatus.RESOLVED
    entity = result.entity
    assert entity is not None
    assert entity.kind is EntityKind.COMPANY
    assert entity.canonical_key == "cik:0001067983"
    tickers = {a.alias for a in entity.aliases if a.alias_type is AliasType.TICKER}
    assert tickers == {"BRK-B", "BRK-A"}
    # Second time it comes from the graph, not reference data.
    again = resolver.resolve("BRK-A", create=True)
    assert again.entity is not None and again.entity.id == entity.id
    assert store.counts()["entities"] == 1


def test_reference_hit_prefers_existing_graph_entity(
    resolver: Resolver, store: SQLiteBackend, reference: ReferenceData
) -> None:
    tesla = reference.by_ticker("TSLA")
    assert tesla is not None
    entity = store.upsert_entity(EntityKind.COMPANY, "Tesla", canonical_key=tesla.canonical_key)
    # No TSLA ticker alias in the graph, but the CIK key matches.
    assert resolver.resolve("TSLA").entity == entity


def test_resolve_ambiguous_returns_candidates(resolver: Resolver, graph: GraphFactory) -> None:
    a = graph.store.upsert_entity(EntityKind.COMPANY, "Acme Holdings Corp", canonical_key="x:1")
    b = graph.store.upsert_entity(EntityKind.COMPANY, "Acme Holdings Inc", canonical_key="x:2")
    result = resolver.resolve("Acme Holdings")
    assert result.status is ResolutionStatus.AMBIGUOUS
    assert {c.id for c in result.candidates} == {a.id, b.id}
    assert result.entity is None


def test_reference_name_ambiguity(resolver: Resolver) -> None:
    result = resolver.resolve("Twin Name Holdings")
    assert result.status is ResolutionStatus.AMBIGUOUS
    assert {c.tickers[0] for c in result.reference_candidates} == {"TEST", "TWIN"}


@pytest.mark.parametrize("ref", ["", "   ", "XYZQ", "Nonexistent Widgets", "9999999999"])
def test_not_found(resolver: Resolver, ref: str) -> None:
    assert resolver.resolve(ref).status is ResolutionStatus.NOT_FOUND


def test_works_without_reference_data(store: SQLiteBackend, graph: GraphFactory) -> None:
    nvda = graph.company("NVDA", name="NVIDIA Corp")
    resolver = Resolver(store)
    assert resolver.resolve("nvda").entity == nvda
    assert resolver.resolve("TSLA").status is ResolutionStatus.NOT_FOUND


def test_follows_merge_redirect(resolver: Resolver, seeded: dict[str, Entity]) -> None:
    store = resolver.store
    dup = store.upsert_entity(EntityKind.COMPANY, "Nvidia (dup)", canonical_key="ticker:NVDA-dup")
    store.merge_entities(seeded["NVDA"].id, dup.id, reason="duplicate")
    result = resolver.resolve(dup.id)
    assert result.entity is not None and result.entity.id == seeded["NVDA"].id
