from __future__ import annotations

import io
import json
from pathlib import Path

import pytest

from conftest import GraphFactory
from torus.config import Paths
from torus.core.clock import ManualClock
from torus.core.impact import ingest_event
from torus.core.models import EventKind, RelationType
from torus.core.store import SQLiteBackend
from torus.maintenance import Level, doctor, export_graph, import_graph, repair_search_index


def _populate(store: SQLiteBackend, clock: ManualClock) -> None:
    g = GraphFactory(store)
    tsm, nvda = g.company("TSM"), g.company("NVDA")
    store.record_relation(tsm.id, RelationType.SUPPLIES, nvda.id, 0.9)
    claim = g.fact("v1", nvda, tsm)
    clock.advance(hours=1)
    store.supersede_claim(claim.id, reason="update", text="v2")
    thesis = g.thesis("t", nvda, claim.id)
    store.update_thesis(thesis.id, "news", confidence=0.4)
    ingest_event(store, EventKind.NEWS, "TSMC news", clock.now(), [tsm.id], uri="https://e.com/n")
    session = store.start_session("claude-code", "s1", "/t.jsonl")
    store.add_source_candidate(session.id, "WebFetch", uri="https://e.com/a")
    store.enqueue("distill_session", "distill:s1", {"session_id": session.id})
    store.set_setting("sec_contact", "me@example.com")


def _rows(text: str) -> list[str]:
    return text.splitlines()[1:]  # without the header (it has a timestamp)


def test_export_import_round_trip(store: SQLiteBackend, clock: ManualClock, tmp_path: Path) -> None:
    _populate(store, clock)
    first = io.StringIO()
    counts = export_graph(store, first)
    assert counts["claims"] == 2 and counts["alerts"] == 1 and counts["queue"] == 1

    with SQLiteBackend.open(tmp_path / "restored.db") as restored:
        import_graph(restored, io.StringIO(first.getvalue()))
        again = io.StringIO()
        export_graph(restored, again)
        assert _rows(again.getvalue()) == _rows(first.getvalue())
        # Derived structures work after import: search index, temporal history.
        assert [h.object_type for h in restored.search("v2")] == ["claim"]
        assert restored.search("v1", object_types=["claim"]) == []
        [thesis] = restored.theses()
        assert [h.confidence for h in restored.thesis_history(thesis.id)] == [0.7, 0.4]


def test_import_refuses_non_empty_database(store: SQLiteBackend, clock: ManualClock) -> None:
    _populate(store, clock)
    dump = io.StringIO()
    export_graph(store, dump)
    with pytest.raises(ValueError, match="not empty"):
        import_graph(store, io.StringIO(dump.getvalue()))


@pytest.mark.parametrize(
    ("text", "message"),
    [
        ("", "missing header"),
        ('{"hello": 1}\n', "not a Torus export"),
        ('{"torus_export": 1, "schema_version": 999}\n', "newer than this database"),
        ('{"torus_export": 1, "schema_version": 1}\nnot json\n', "not JSON"),
        ('{"torus_export": 1, "schema_version": 1}\n{"t": "nope", "r": {}}\n', "unknown table"),
        (
            '{"torus_export": 1, "schema_version": 1}\n'
            '{"t": "claim_entities",'
            ' "r": {"claim_id": "c", "entity_id": "e", "role": "subject"}}\n',
            "dangling references",
        ),
    ],
)
def test_bad_imports_change_nothing(store: SQLiteBackend, text: str, message: str) -> None:
    with pytest.raises(ValueError, match=message):
        import_graph(store, io.StringIO(text))
    assert sum(store.counts().values()) == 0


def test_doctor_on_fresh_home(tmp_path: Path) -> None:
    checks = {c.name: c for c in doctor(Paths(tmp_path / "home"))}
    assert checks["schema"].level is Level.OK
    assert checks["integrity"].level is Level.OK
    assert checks["search index"].level is Level.OK
    assert checks["SEC reference"].level is Level.WARN
    assert not [c for c in checks.values() if c.level is Level.FAIL]


def test_doctor_flags_and_repairs_broken_search_index(tmp_path: Path) -> None:
    p = Paths(tmp_path / "home")
    p.ensure()
    with SQLiteBackend.open(p.db) as store:
        GraphFactory(store).fact("findable claim", GraphFactory(store).company("NVDA"))
        store._conn.execute("DELETE FROM claims_fts_data WHERE id > 10")  # corrupt the index
    checks = {c.name: c for c in doctor(p)}
    assert checks["search index"].level is Level.FAIL
    repair_search_index(p)
    assert {c.name: c for c in doctor(p)}["search index"].level is Level.OK
    with SQLiteBackend.open(p.db) as store:
        assert store.search("findable")


def test_doctor_warns_about_hook_failures_and_missing_contact(tmp_path: Path) -> None:
    p = Paths(tmp_path / "home")
    p.ensure()
    with SQLiteBackend.open(p.db) as store:
        store.watch_entity(GraphFactory(store).company("NVDA").id)
    (p.logs / "hooks.log").write_text(
        "2026-01-01 INFO stop ok in 10 ms\n"
        "2026-01-01 ERROR user-prompt-submit failed after 12 ms: boom\n",
        "utf-8",
    )
    checks = {c.name: c for c in doctor(p)}
    assert checks["hooks"].level is Level.WARN and "boom" in checks["hooks"].detail
    assert checks["watchers"].level is Level.WARN


def test_export_file_is_json_lines(store: SQLiteBackend, clock: ManualClock) -> None:
    _populate(store, clock)
    out = io.StringIO()
    export_graph(store, out)
    header, *rows = out.getvalue().splitlines()
    assert json.loads(header)["torus_export"] == 1
    assert all({"t", "r"} == set(json.loads(r)) for r in rows)


def test_doctor_finds_and_repairs_duplicate_links(tmp_path: Path) -> None:
    from torus.maintenance import repair_duplicate_links

    p = Paths(tmp_path / "home")
    p.ensure()
    with SQLiteBackend.open(p.db) as store:
        g = GraphFactory(store)
        tsm, nvda = g.company("TSM"), g.company("NVDA")
        first = store.record_relation(tsm.id, RelationType.SUPPLIES, nvda.id, 0.9)
        # Simulate a pre-dedupe database: a second current copy of the same link.
        store._conn.execute(
            "INSERT INTO relations (id, lineage_id, src_id, rel, dst_id, confidence,"
            " recorded_at, source) SELECT 'dup', 'dup', src_id, rel, dst_id, confidence,"
            " '2026-06-01T00:00:00.000000Z', source FROM relations WHERE id = ?",
            (first.id,),
        )
    assert {c.name: c for c in doctor(p)}["duplicate links"].level is Level.WARN
    assert repair_duplicate_links(p) == 1
    assert {c.name: c for c in doctor(p)}["duplicate links"].level is Level.OK
    with SQLiteBackend.open(p.db) as store:
        # The copy dated June 2026 is older than the original, so the original stays.
        assert [r.id for r in store.relations(nvda.id, direction="in")] == [first.id]
