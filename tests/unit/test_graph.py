from __future__ import annotations

from datetime import timedelta

import pytest

from conftest import GraphFactory
from torus.core.clock import ManualClock
from torus.core.graph import find_paths, neighbors, snapshot
from torus.core.impact import ingest_event
from torus.core.models import EventKind, RelationType
from torus.core.store import SQLiteBackend


@pytest.fixture
def chain(store: SQLiteBackend, graph: GraphFactory) -> dict[str, str]:
    """ASML -> TSM -> NVDA -> MSFT, AMD competes with NVDA, TSM -> AMD."""
    ids = {t: graph.company(t).id for t in ("ASML", "TSM", "NVDA", "MSFT", "AMD", "LONE")}
    s = RelationType.SUPPLIES
    store.record_relation(ids["ASML"], s, ids["TSM"], 0.9)
    store.record_relation(ids["TSM"], s, ids["NVDA"], 0.95)
    store.record_relation(ids["NVDA"], s, ids["MSFT"], 0.9)
    store.record_relation(ids["TSM"], s, ids["AMD"], 0.9)
    store.record_relation(ids["NVDA"], RelationType.COMPETES_WITH, ids["AMD"], 0.9)
    return ids


def _labels(sub: object, kind: str = "entity") -> set[str]:
    return {n.label for n in sub.nodes if n.kind == kind}  # type: ignore[attr-defined]


def test_neighbors_one_and_two_hops(store: SQLiteBackend, chain: dict[str, str]) -> None:
    one = neighbors(store, [chain["TSM"]], hops=1)
    assert _labels(one) == {"TSM Inc", "ASML Inc", "NVDA Inc", "AMD Inc"}
    assert one.focus == [chain["TSM"]]
    rels = {(e.source, e.kind, e.target) for e in one.edges}
    assert (chain["NVDA"], "COMPETES_WITH", chain["AMD"]) in rels  # link among neighbours
    two = neighbors(store, [chain["TSM"]], hops=2)
    assert "MSFT Inc" in _labels(two) and "LONE Inc" not in _labels(two)
    hops = {n.label: n.data["hops"] for n in two.nodes}
    assert hops["TSM Inc"] == 0 and hops["MSFT Inc"] == 2


def test_neighbors_relation_filter(store: SQLiteBackend, chain: dict[str, str]) -> None:
    only = neighbors(store, [chain["NVDA"]], rel_types=[RelationType.COMPETES_WITH])
    assert _labels(only) == {"NVDA Inc", "AMD Inc"}


def test_neighbors_attach_theses(
    store: SQLiteBackend, graph: GraphFactory, chain: dict[str, str]
) -> None:
    nvda = store.get_entity(chain["NVDA"])
    thesis = graph.thesis("NVDA supply-bound", nvda)
    sub = neighbors(store, [chain["TSM"]], hops=1)
    assert _labels(sub, "thesis") == {"NVDA supply-bound"}
    [edge] = [e for e in sub.edges if e.source == thesis.id]
    assert (edge.target, edge.kind) == (chain["NVDA"], "subject")
    assert _labels(neighbors(store, [chain["TSM"]], include_theses=False), "thesis") == set()


def test_neighbors_respect_validity(
    store: SQLiteBackend, chain: dict[str, str], clock: ManualClock
) -> None:
    [rel] = store.relations(chain["MSFT"], direction="in")
    store.supersede_relation(rel.id, valid_to=clock.now() + timedelta(days=1))
    later = clock.now() + timedelta(days=2)
    assert "MSFT Inc" not in _labels(neighbors(store, [chain["NVDA"]], valid_at=later))
    assert "MSFT Inc" in _labels(neighbors(store, [chain["NVDA"]], valid_at=clock.now()))


def test_find_paths_shortest_then_most_confident(
    store: SQLiteBackend, chain: dict[str, str]
) -> None:
    result = find_paths(store, chain["ASML"], chain["AMD"], max_hops=3)
    assert result.texts[0] == "ASML Inc -SUPPLIES-> TSM Inc -SUPPLIES-> AMD Inc"
    assert result.paths[0].confidence == pytest.approx(0.81)
    assert "ASML Inc -SUPPLIES-> TSM Inc -SUPPLIES-> NVDA Inc -COMPETES_WITH-> AMD Inc" in (
        result.texts
    )
    assert [len(p.steps) for p in result.paths] == sorted(len(p.steps) for p in result.paths)


def test_find_paths_direction_and_limits(store: SQLiteBackend, chain: dict[str, str]) -> None:
    back = find_paths(store, chain["MSFT"], chain["TSM"], max_hops=2)
    assert back.texts == ["MSFT Inc <-SUPPLIES- NVDA Inc <-SUPPLIES- TSM Inc"]
    assert find_paths(store, chain["ASML"], chain["MSFT"], max_hops=2).paths == []
    assert find_paths(store, chain["ASML"], chain["LONE"]).paths == []
    assert find_paths(store, chain["ASML"], chain["ASML"]).paths == []


def test_snapshot_skips_isolated_and_includes_events_and_alerts(
    store: SQLiteBackend, graph: GraphFactory, chain: dict[str, str], clock: ManualClock
) -> None:
    graph.thesis("NVDA view", store.get_entity(chain["NVDA"]))
    [alert] = ingest_event(store, EventKind.FILING, "TSMC 6-K", clock.now(), [chain["TSM"]]).alerts
    snap = snapshot(store)
    assert "LONE Inc" not in _labels(snap)
    assert "LONE Inc" in _labels(snapshot(store, include_isolated=True))
    [event] = [n for n in snap.nodes if n.kind == "event"]
    assert event.data["has_open_alert"] is True
    kinds = {e.kind for e in snap.edges}
    assert {"SUPPLIES", "COMPETES_WITH", "subject", "about", "alert"} <= kinds
    [alert_edge] = [e for e in snap.edges if e.kind == "alert"]
    assert alert_edge.id == alert.id and alert_edge.target == alert.thesis_id
