from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from torus.core.store import SQLiteBackend
from torus.core.store import migrate as m

EXPECTED_TABLES = {
    "meta",
    "sessions",
    "entities",
    "entity_aliases",
    "evidence",
    "claims",
    "claim_entities",
    "claim_evidence",
    "claim_derivations",
    "relations",
    "theses",
    "thesis_claims",
    "thesis_triggers",
    "thesis_history",
    "rejected_hypotheses",
    "rejected_hypothesis_entities",
    "claims_fts",
    "evidence_fts",
    "theses_fts",
    "entity_merges",
    "source_candidates",
    "session_entities",
    "session_records",
    "queue",
    "events",
    "event_entities",
    "alerts",
    "events_fts",
    "watchlist",
    "feeds",
    "watcher_state",
    "settings",
}


def _tables(conn: sqlite3.Connection) -> set[str]:
    rows = conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'").fetchall()
    return {r[0] for r in rows}


def test_init_creates_schema_and_version(store: SQLiteBackend) -> None:
    assert _tables(store._conn) >= EXPECTED_TABLES
    assert store.schema_version() == m.latest_version()


def test_migrations_are_idempotent() -> None:
    conn = sqlite3.connect(":memory:", isolation_level=None)
    assert m.migrate(conn) == m.latest_version()
    before = _tables(conn)
    assert m.migrate(conn) == m.latest_version()
    assert _tables(conn) == before


def test_reopening_file_db_keeps_data(tmp_path: Path) -> None:
    path = tmp_path / "t.db"
    with SQLiteBackend.open(path) as s:
        s.record_evidence("https://example.com/a")
    with SQLiteBackend.open(path) as s:
        assert s.counts()["evidence"] == 1
        assert s.schema_version() == m.latest_version()


def test_failed_migration_rolls_back(monkeypatch: pytest.MonkeyPatch) -> None:
    conn = sqlite3.connect(":memory:", isolation_level=None)
    m.migrate(conn)
    bad = m.Migration(
        version=m.latest_version() + 1,
        name="9999_bad.sql",
        sql="CREATE TABLE half_done (x INT); THIS IS NOT SQL;",
    )
    good = m.available()
    monkeypatch.setattr(m, "available", lambda: [*good, bad])
    with pytest.raises(sqlite3.Error):
        m.migrate(conn)
    assert "half_done" not in _tables(conn)
    assert m.current_version(conn) == len(good)
    assert not conn.in_transaction


def test_migration_files_are_contiguous() -> None:
    versions = [mig.version for mig in m.available()]
    assert versions == list(range(1, len(versions) + 1))


def test_0002_backfills_alias_keys(monkeypatch: pytest.MonkeyPatch) -> None:
    conn = sqlite3.connect(":memory:", isolation_level=None)
    all_migrations = m.available()
    monkeypatch.setattr(m, "available", lambda: all_migrations[:1])
    m.migrate(conn)
    conn.execute(
        "INSERT INTO entities (id, kind, name, created_at, updated_at)"
        " VALUES ('e1', 'company', 'NVIDIA CORP', 't', 't')"
    )
    conn.execute(
        "INSERT INTO entity_aliases (entity_id, alias, alias_norm, alias_type)"
        " VALUES ('e1', 'NVIDIA CORP', 'nvidia corp', 'name')"
    )
    monkeypatch.setattr(m, "available", lambda: all_migrations)
    m.migrate(conn)
    row = conn.execute("SELECT alias_key FROM entity_aliases WHERE entity_id = 'e1'").fetchone()
    assert row[0] == "nvidia"
