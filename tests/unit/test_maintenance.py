from __future__ import annotations

import io
import json
import shutil
import sys
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


def test_doctor_flags_a_stale_ticker_list(tmp_path: Path) -> None:
    import os
    import time

    from conftest import FIXTURES

    p = Paths(tmp_path / "home").ensure()
    ref = p.reference / "sec_company_tickers.json"
    shutil.copy(FIXTURES / "sec_company_tickers.json", ref)
    assert {c.name: c for c in doctor(p)}["SEC reference"].level is Level.OK
    old = time.time() - 45 * 86400
    os.utime(ref, (old, old))
    check = {c.name: c for c in doctor(p)}["SEC reference"]
    assert check.level is Level.WARN and "45 days ago" in check.detail


def test_fix_repairs_what_doctor_flags(tmp_path: Path) -> None:
    from test_automation import FakeRunner

    from torus.core.reference import ReferenceData
    from torus.maintenance import fix
    from torus.schedule import Scheduler
    from torus.setup import configure_codex

    p = Paths(tmp_path / "home").ensure()
    with SQLiteBackend.open(p.db) as store:
        g = GraphFactory(store)
        nvda = g.company("NVDA")
        store.watch_entity(nvda.id)
        g.fact("findable claim", nvda)
        store._conn.execute("DELETE FROM claims_fts_data WHERE id > 10")  # corrupt the index
    scheduler = Scheduler(p.home, platform="win32", runner=FakeRunner())
    claude_cfg = tmp_path / "claude" / "claude_desktop_config.json"
    claude_cfg.parent.mkdir()
    claude_cfg.write_text("{not json", "utf-8")
    codex_cfg = tmp_path / "codex" / "config.toml"
    configure_codex(codex_cfg, command=sys.executable, args=["-m", "torus", "serve"])
    shutil.rmtree(codex_cfg.parent / "skills")

    fetched: list[str] = []

    def fetch(dest: Path, contact: str) -> ReferenceData:
        fetched.append(contact)
        return ReferenceData.from_rows([(1045810, "NVDA", "NVIDIA CORP")])

    def run_doctor() -> dict[str, Level]:
        checks = doctor(p, scheduler=scheduler, claude_config=claude_cfg, codex_config=codex_cfg)
        return {c.name: c.level for c in checks}

    checks = doctor(p, scheduler=scheduler, claude_config=claude_cfg, codex_config=codex_cfg)
    levels = {c.name: c.level for c in checks}
    assert levels["search index"] is Level.FAIL
    assert levels["watchers"] is levels["SEC reference"] is levels["schedule"] is Level.WARN
    assert levels["Claude Desktop"] is levels["Codex"] is Level.WARN

    # Without an email, the SEC steps are skipped and everything else is fixed.
    done = fix(
        p,
        checks,
        scheduler=scheduler,
        claude_config=claude_cfg,
        codex_config=codex_cfg,
        fetch=fetch,
    )
    assert "search index: rebuilt" in done
    assert any(d.startswith("watchers: skipped") for d in done)
    assert any(d.startswith("SEC reference: skipped") for d in done)
    assert any(d.startswith("Claude Desktop:") and "by hand" in d for d in done)
    assert any(d.startswith("Codex: rewrote") for d in done)
    assert not fetched
    levels = run_doctor()
    assert levels["search index"] is levels["schedule"] is levels["Codex"] is Level.OK
    assert levels["Claude Desktop"] is Level.WARN  # invalid JSON is left for the user

    done = fix(
        p,
        doctor(p, scheduler=scheduler, claude_config=claude_cfg, codex_config=codex_cfg),
        contact="me@example.com",
        scheduler=scheduler,
        claude_config=claude_cfg,
        codex_config=codex_cfg,
        fetch=fetch,
    )
    assert "sec_contact: set to me@example.com" in done
    assert "SEC reference: downloaded 1 companies" in done
    assert fetched == ["me@example.com"]
    with SQLiteBackend.open(p.db) as store:
        assert store.get_setting("sec_contact") == "me@example.com"
    assert run_doctor()["watchers"] is Level.OK


def test_fix_does_nothing_on_a_healthy_home(tmp_path: Path) -> None:
    from torus.maintenance import fix

    p = Paths(tmp_path / "home").ensure()
    configs = {
        "claude_config": tmp_path / "claude" / "claude_desktop_config.json",
        "codex_config": tmp_path / "codex" / "config.toml",
    }
    checks = [c for c in doctor(p, **configs) if c.name != "SEC reference"]
    assert fix(p, checks, **configs) == []
