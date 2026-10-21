from __future__ import annotations

import pytest

from conftest import GraphFactory
from torus.core.clock import ManualClock
from torus.core.errors import NotCurrentError
from torus.core.models import QueueStatus
from torus.core.store import SQLiteBackend
from torus.core.store.sqlite import MAX_ATTEMPTS

KIND = "distill_session"


def _enqueue(store: SQLiteBackend, key: str) -> str:
    return store.enqueue(KIND, key, {"session_id": key}).id


def test_pending_work_returns_oldest_first_and_leases(
    store: SQLiteBackend, clock: ManualClock
) -> None:
    first = _enqueue(store, "a")
    clock.advance(minutes=1)
    second = _enqueue(store, "b")
    [item] = store.lease_work(KIND)
    assert item.id == first
    assert item.status is QueueStatus.LEASED and item.attempts == 1
    assert item.lease_until is not None
    [nxt] = store.lease_work(KIND)
    assert nxt.id == second
    assert store.lease_work(KIND) == []  # both leased


def test_expired_lease_is_handed_out_again(store: SQLiteBackend, clock: ManualClock) -> None:
    item_id = _enqueue(store, "a")
    store.lease_work(KIND, lease_seconds=60)
    clock.advance(seconds=30)
    assert store.lease_work(KIND) == []
    clock.advance(seconds=31)
    [again] = store.lease_work(KIND)
    assert again.id == item_id and again.attempts == 2


def test_expired_leases_park_after_max_attempts(store: SQLiteBackend, clock: ManualClock) -> None:
    item_id = _enqueue(store, "a")
    for _ in range(MAX_ATTEMPTS):
        assert store.lease_work(KIND, lease_seconds=10)
        clock.advance(seconds=11)
    assert store.lease_work(KIND) == []
    parked = store.get_queue_item(item_id)
    assert parked.status is QueueStatus.PARKED
    assert parked.payload["errors"] == ["lease expired"]


def test_complete_stores_result(store: SQLiteBackend) -> None:
    item_id = _enqueue(store, "a")
    store.lease_work(KIND)
    done = store.complete_work(item_id, {"summary": "3 claims"})
    assert done.status is QueueStatus.DONE
    assert done.payload["result"] == {"summary": "3 claims"}
    assert done.payload["session_id"] == "a"
    with pytest.raises(NotCurrentError):
        store.complete_work(item_id, {})


def test_failed_distillation_retries_then_parks(store: SQLiteBackend) -> None:
    item_id = _enqueue(store, "a")
    for attempt in range(1, MAX_ATTEMPTS + 1):
        [item] = store.lease_work(KIND)
        assert item.attempts == attempt
        released = store.release_work(item_id, error=f"boom {attempt}")
    assert released.status is QueueStatus.PARKED
    assert released.payload["errors"] == [f"boom {i}" for i in range(1, MAX_ATTEMPTS + 1)]
    assert store.queue_counts() == {KIND: {"parked": 1}}


def test_new_activity_while_leased_queues_a_fresh_item(store: SQLiteBackend) -> None:
    old = _enqueue(store, "a")
    store.lease_work(KIND)
    fresh = _enqueue(store, "a")  # the session kept going while the curator worked
    assert fresh != old
    released = store.release_work(old, error="transcript changed")
    assert released.status is QueueStatus.DONE
    assert released.payload["merged_into"] == fresh
    assert [i.id for i in store.queue_items(statuses=[QueueStatus.PENDING])] == [fresh]


def test_recording_evidence_links_source_candidates(
    store: SQLiteBackend, graph: GraphFactory
) -> None:
    session = store.start_session("claude-code", "s1")
    store.add_source_candidate(session.id, "WebFetch", uri="https://e.com/a")
    store.add_source_candidate(session.id, "WebSearch", query="cowos")
    ev = store.record_evidence("https://e.com/a", title="A")
    linked = {c.uri: c.linked_evidence_id for c in store.source_candidates(session.id)}
    assert linked == {"https://e.com/a": ev.id, None: None}
    # Seen again in a later session, after the evidence exists: linked straight away.
    later = store.start_session("claude-code", "s2")
    store.add_source_candidate(later.id, "WebFetch", uri="https://e.com/a")
    assert store.source_candidates(later.id)[0].linked_evidence_id == ev.id
