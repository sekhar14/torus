"""Capture receipts (Stop hook) and undoing what a session recorded."""

from __future__ import annotations

import json
from pathlib import Path

from conftest import GraphFactory
from torus.core.models import ClaimType, RelationType, Stance, ThesisStatus
from torus.core.starter import seed
from torus.core.store import SQLiteBackend
from torus.core.undo import recent, undo
from torus.hooks import run_hook


def _session(store: SQLiteBackend, external: str = "s1") -> str:
    return store.start_session("claude-code", external).id


def _note(store: SQLiteBackend, session_id: str, kind: str, object_id: str) -> None:
    store.adopt_into_session(session_id, kind, object_id)
    store.note_session_record(session_id, kind, object_id)


def test_undo_reverts_each_kind(store: SQLiteBackend, graph: GraphFactory) -> None:
    nvda, tsm, acme = graph.company("NVDA"), graph.company("TSM"), graph.company("ACME")
    seed(store, "semis")  # no reference data: the pack reuses the ticker-keyed companies
    nvda_pack = store.get_entity_by_key("ticker:NVDA")
    tsm_pack = store.get_entity_by_key("ticker:TSM")
    assert nvda_pack is not None and tsm_pack is not None
    before = graph.fact("NVDA ships Blackwell in Q4", nvda)
    sid = _session(store)

    ev = store.record_evidence("https://example.com/a")
    claim = store.record_claim("TSMC adds CoWoS capacity", ClaimType.FACT, 0.8, [tsm.id], [ev.id])
    corrected = store.supersede_claim(before.id, "date slipped", text="NVDA ships Blackwell in Q1")
    thesis = store.record_thesis("T", "S", Stance.BULLISH, 0.6, session_id=sid)
    new_link = store.record_relation(acme.id, RelationType.SUPPLIES, nvda.id, 0.9)
    confirmed = store.record_relation(tsm_pack.id, RelationType.SUPPLIES, nvda_pack.id, 0.95)
    hyp = store.record_rejected_hypothesis("Intel wins NVDA foundry", "no evidence", [nvda.id])
    for kind, oid in [
        ("evidence", ev.id),
        ("claim", claim.id),
        ("claim", corrected.id),
        ("thesis", thesis.id),
        ("relation", new_link.id),
        ("relation", confirmed.id),
        ("hypothesis", hyp.id),
    ]:
        _note(store, sid, kind, oid)

    [session] = recent(store)
    assert session.session_id == sid
    states = {(r.type, r.id): (r.state, r.undoable) for r in session.records}
    assert states[("claim", corrected.id)] == ("correction", True)
    assert states[("evidence", ev.id)] == ("kept", False)
    assert states[("thesis", thesis.id)] == ("created", True)

    items = [(r.type, r.id) for r in session.records]
    outcomes = {(o.type, o.id): o for o in undo(store, items, session_id=sid)}
    assert outcomes[("claim", claim.id)].result == "retracted"
    assert store.current_claim(claim.lineage_id) is None
    assert outcomes[("claim", corrected.id)].done
    current = store.current_claim(before.lineage_id)
    assert current is not None and current.text == "NVDA ships Blackwell in Q4"
    assert store.get_thesis(thesis.id).status is ThesisStatus.ARCHIVED
    assert outcomes[("relation", new_link.id)].result == "link removed"
    assert store.relations(acme.id) == []
    restored = [r for r in store.relations(tsm_pack.id) if r.dst_id == nvda_pack.id]
    assert [r.source for r in restored] == ["starter:semis@1"]
    assert store.rejected_hypotheses(entity_id=nvda.id) == []
    assert not outcomes[("evidence", ev.id)].done

    again = undo(store, [("claim", claim.id), ("hypothesis", hyp.id)], session_id=sid)
    assert [o.done for o in again] == [False, False]


def test_thesis_from_another_session_is_not_archived(
    store: SQLiteBackend, graph: GraphFactory
) -> None:
    earlier = _session(store, "s0")
    thesis = store.record_thesis("T", "S", Stance.BULLISH, 0.6, session_id=earlier)
    sid = _session(store, "s1")
    store.update_thesis(thesis.id, "more confident", confidence=0.8)
    store.note_session_record(sid, "thesis", thesis.id)
    [session] = recent(store)
    assert [(r.state, r.undoable) for r in session.records] == [("revised", False)]
    [outcome] = undo(store, [("thesis", thesis.id)], session_id=sid)
    assert not outcome.done and "update_thesis" in outcome.result


def _hook(event: str, home: Path, **payload: object) -> str:
    return run_hook(
        event, json.dumps({"session_id": "h1", **payload}), env={"TORUS_HOME": str(home)}
    )


def test_stop_hook_prints_a_receipt_once(torus_home: Path) -> None:
    _hook("session-start", torus_home)
    with SQLiteBackend.open(torus_home / "torus.db") as store:
        sid = store.start_session("claude-code", "h1").id
        ev = store.record_evidence("https://example.com/a")
        nvda = GraphFactory(store).company("NVDA")
        claim = store.record_claim("x", ClaimType.FACT, 0.8, [nvda.id], [ev.id])
        thesis = store.record_thesis("T", "S", Stance.BULLISH, 0.6)
        for kind, oid in [("evidence", ev.id), ("claim", claim.id), ("thesis", thesis.id)]:
            store.note_session_record(sid, kind, oid)
    out = json.loads(_hook("stop", torus_home))
    assert out["systemMessage"].startswith("Torus recorded 1 thesis, 1 claim and 1 source")
    assert "/torus:undo" in out["systemMessage"]
    assert _hook("stop", torus_home) == ""  # nothing new since the last receipt

    with SQLiteBackend.open(torus_home / "torus.db") as store:
        store.set_setting("capture_receipts", "off")
        store.note_session_record(sid, "claim", claim.id + "x")
    assert _hook("stop", torus_home) == ""
