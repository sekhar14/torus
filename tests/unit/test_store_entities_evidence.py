from __future__ import annotations

from datetime import UTC, datetime

import pytest

from torus.core.errors import NotFoundError
from torus.core.models import Alias, AliasType, EntityKind, EvidenceKind
from torus.core.store import SQLiteBackend
from torus.core.store.sqlite import normalize_alias


def test_entity_upsert_merges_by_canonical_key(store: SQLiteBackend) -> None:
    a = store.upsert_entity(
        EntityKind.COMPANY,
        "NVIDIA Corp",
        canonical_key="cik:0001045810",
        aliases=[Alias(alias="NVDA", alias_type=AliasType.TICKER)],
        attrs={"exchange": "NASDAQ"},
    )
    b = store.upsert_entity(
        EntityKind.COMPANY,
        "Nvidia",
        canonical_key="cik:0001045810",
        aliases=[Alias(alias="NVIDIA Corporation", alias_type=AliasType.FORMER_NAME)],
        attrs={"sector": "semis"},
    )
    assert a.id == b.id
    assert b.name == "NVIDIA Corp"
    assert b.attrs == {"exchange": "NASDAQ", "sector": "semis"}
    assert {x.alias for x in b.aliases} == {"NVIDIA Corp", "NVDA", "Nvidia", "NVIDIA Corporation"}
    assert store.counts()["entities"] == 1


def test_entities_without_key_are_not_merged(store: SQLiteBackend) -> None:
    store.upsert_entity(EntityKind.PERSON, "Jensen Huang")
    store.upsert_entity(EntityKind.PERSON, "Jensen Huang")
    assert store.counts()["entities"] == 2


def test_find_by_alias_is_normalised(store: SQLiteBackend) -> None:
    e = store.upsert_entity(
        EntityKind.COMPANY,
        "Taiwan Semiconductor Manufacturing Co., Ltd.",
        canonical_key="ticker:TSM",
        aliases=[Alias(alias="TSM", alias_type=AliasType.TICKER)],
    )
    assert [x.id for x in store.find_entities_by_alias("tsm")] == [e.id]
    assert [
        x.id for x in store.find_entities_by_alias("taiwan semiconductor manufacturing co ltd")
    ] == [e.id]
    assert store.find_entities_by_alias("TSMX") == []


def test_normalize_alias() -> None:
    assert normalize_alias("  NVIDIA, Corp. ") == "nvidia corp"
    assert normalize_alias("S&P 500") == "s p 500"


def test_add_alias_and_get_by_key(store: SQLiteBackend) -> None:
    e = store.upsert_entity(EntityKind.COMPANY, "Alphabet", canonical_key="ticker:GOOGL")
    store.add_alias(e.id, "Google", AliasType.FORMER_NAME)
    found = store.get_entity_by_key("ticker:GOOGL")
    assert found is not None and "Google" in {a.alias for a in found.aliases}
    assert store.get_entity_by_key("ticker:NOPE") is None


def test_get_missing_entity_raises(store: SQLiteBackend) -> None:
    with pytest.raises(NotFoundError):
        store.get_entity("nope")


def test_evidence_dedup_by_uri_and_hash(store: SQLiteBackend) -> None:
    a = store.record_evidence("https://ir.nvidia.com/q3", content_hash="h1", title="Q3")
    b = store.record_evidence("https://ir.nvidia.com/q3", content_hash="h1")
    c = store.record_evidence("https://ir.nvidia.com/q3", content_hash="h2")
    d = store.record_evidence("https://ir.nvidia.com/q3")
    e = store.record_evidence("https://ir.nvidia.com/q3")
    assert a.id == b.id
    assert len({a.id, c.id, d.id}) == 3
    assert d.id == e.id


def test_evidence_fields_roundtrip(store: SQLiteBackend, clock: object) -> None:
    published = datetime(2025, 11, 19, 21, 0, tzinfo=UTC)
    ev = store.record_evidence(
        "https://www.sec.gov/x/10-q.htm",
        kind=EvidenceKind.FILING,
        title="10-Q",
        publisher="SEC",
        published_at=published,
        quote="Data Center revenue was $51.2 billion",
        content_hash="abc",
    )
    got = store.get_evidence(ev.id)
    assert got.kind is EvidenceKind.FILING
    assert got.published_at == published
    assert got.retrieved_at == got.recorded_at
    assert got.quote == "Data Center revenue was $51.2 billion"


def test_sessions_are_idempotent_per_external_id(store: SQLiteBackend) -> None:
    a = store.start_session("claude-code", "abc", transcript_path="/t/1.jsonl")
    b = store.start_session("claude-code", "abc", transcript_path="/t/2.jsonl")
    c = store.start_session("codex", "abc")
    assert a.id == b.id != c.id
    assert b.transcript_path == "/t/2.jsonl"
    ended = store.end_session(a.id)
    assert ended.ended_at is not None
