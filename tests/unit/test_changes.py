from __future__ import annotations

from conftest import GraphFactory
from torus.core.changes import ChangeKind, timeline, what_changed
from torus.core.clock import ManualClock
from torus.core.models import ThesisStatus
from torus.core.store import SQLiteBackend


def test_timeline_chronological(
    store: SQLiteBackend, graph: GraphFactory, clock: ManualClock
) -> None:
    nvda, amd = graph.company("NVDA"), graph.company("AMD")
    a = graph.fact("v1", nvda)
    clock.advance(hours=1)
    thesis = graph.thesis("t", nvda, a.id)
    clock.advance(hours=1)
    store.supersede_claim(a.id, reason="update", text="v2")
    clock.advance(hours=1)
    store.update_thesis(thesis.id, reason="event", status=ThesisStatus.AT_RISK)
    graph.fact("unrelated", amd)

    log = timeline(store, nvda.id)
    kinds = [c.kind for c in log.changes]
    assert kinds == [
        ChangeKind.CLAIM_RECORDED,
        ChangeKind.THESIS_CREATED,
        ChangeKind.CLAIM_SUPERSEDED,
        ChangeKind.CLAIM_RECORDED,
        ChangeKind.THESIS_CHANGED,
    ]
    assert [c.at for c in log.changes] == sorted(c.at for c in log.changes)
    assert log.changes[2].reason == "update"
    assert log.changes[4].reason == "event"
    assert "unrelated" not in {c.summary for c in log.changes}


def test_timeline_window(store: SQLiteBackend, graph: GraphFactory, clock: ManualClock) -> None:
    nvda = graph.company("NVDA")
    graph.fact("before", nvda)
    start = clock.advance(days=1)
    graph.fact("inside", nvda)
    end = clock.advance(days=1)
    graph.fact("after", nvda)
    log = timeline(store, nvda.id, since=start, until=end)
    assert [c.summary for c in log.changes] == ["fact (0.80): inside"]


def test_what_changed_since(store: SQLiteBackend, graph: GraphFactory, clock: ManualClock) -> None:
    graph.company("NVDA")
    since = clock.advance(days=1)
    tsm = graph.company("TSM")
    claim = graph.fact("new fact", tsm)
    clock.advance(hours=1)
    store.retract_claim(claim.id, reason="wrong")
    log = what_changed(store, since)
    assert [c.kind for c in log.changes] == [
        ChangeKind.ENTITY_ADDED,
        ChangeKind.CLAIM_RECORDED,
        ChangeKind.CLAIM_RETRACTED,
    ]
    assert log.changes[0].object_id == tsm.id
    assert log.changes[2].reason == "wrong"


def test_what_changed_includes_events(store: SQLiteBackend, clock: ManualClock) -> None:
    from torus.core.models import EventKind

    since = clock.now()
    event, _ = store.add_event(EventKind.NEWS, "TSMC raises prices", clock.now())
    [change] = what_changed(store, since).changes
    assert change.kind is ChangeKind.EVENT_ADDED and change.object_id == event.id
    assert change.summary.endswith("TSMC raises prices")
