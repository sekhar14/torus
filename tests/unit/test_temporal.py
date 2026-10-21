"""Bi-temporal behaviour: what was believed when (as_of) vs. what was true when (valid_at)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from hypothesis import given, settings
from hypothesis import strategies as st

from conftest import T0, GraphFactory
from torus.core.clock import ManualClock
from torus.core.ids import IdGenerator
from torus.core.models import ClaimType, RelationType
from torus.core.store import SQLiteBackend


def test_supersede_keeps_history(
    store: SQLiteBackend, graph: GraphFactory, clock: ManualClock
) -> None:
    old = graph.fact("v1", graph.company("NVDA"))
    clock.advance(hours=1)
    new = store.supersede_claim(old.id, reason="update", text="v2")
    history = store.claim_history(old.lineage_id)
    assert [c.text for c in history] == ["v1", "v2"]
    assert store.current_claim(old.lineage_id) == new


def test_as_of_returns_belief_at_time(
    store: SQLiteBackend, graph: GraphFactory, clock: ManualClock
) -> None:
    t0 = clock.now()
    clock.advance(hours=1)
    t1 = clock.now()
    claim = graph.fact("v1", graph.company("NVDA"))
    clock.advance(hours=1)
    t2 = clock.now()
    store.supersede_claim(claim.id, reason="update", text="v2")

    def text_at(t: datetime) -> str | None:
        c = store.current_claim(claim.lineage_id, as_of=t)
        return c.text if c else None

    assert text_at(t0) is None
    assert text_at(t1) == "v1"
    assert text_at(t1 + timedelta(minutes=30)) == "v1"
    assert text_at(t2) == "v2"
    assert text_at(t2 + timedelta(days=1)) == "v2"


def test_as_of_before_retraction_still_sees_claim(
    store: SQLiteBackend, graph: GraphFactory, clock: ManualClock
) -> None:
    nvda = graph.company("NVDA")
    claim = graph.fact("x", nvda)
    before = clock.advance(hours=1)
    clock.advance(hours=1)
    store.retract_claim(claim.id, reason="wrong")
    assert [c.id for c in store.claims(entity_id=nvda.id, as_of=before)] == [claim.id]
    assert store.claims(entity_id=nvda.id) == []


def test_valid_at_window_for_claims(store: SQLiteBackend) -> None:
    claim = store.record_claim(
        "Fed funds target 4.25-4.50%",
        ClaimType.OPINION,
        0.9,
        valid_from=datetime(2025, 1, 1, tzinfo=UTC),
        valid_to=datetime(2025, 9, 17, tzinfo=UTC),
    )
    inside = store.claims(valid_at=datetime(2025, 6, 1, tzinfo=UTC))
    after = store.claims(valid_at=datetime(2025, 10, 1, tzinfo=UTC))
    at_end = store.claims(valid_at=datetime(2025, 9, 17, tzinfo=UTC))
    assert [c.id for c in inside] == [claim.id]
    assert after == [] and at_end == []


def test_valid_at_window_for_relations(
    store: SQLiteBackend, graph: GraphFactory, clock: ManualClock
) -> None:
    tsm, nvda = graph.company("TSM"), graph.company("NVDA")
    rel = store.record_relation(
        tsm.id,
        RelationType.SUPPLIES,
        nvda.id,
        0.9,
        valid_from=datetime(2024, 1, 1, tzinfo=UTC),
        valid_to=datetime(2025, 6, 1, tzinfo=UTC),
    )
    jan25 = datetime(2025, 1, 1, tzinfo=UTC)
    jul25 = datetime(2025, 7, 1, tzinfo=UTC)
    assert [r.id for r in store.relations(nvda.id, valid_at=jan25)] == [rel.id]
    assert store.relations(nvda.id, valid_at=jul25) == []


def test_supersede_relation_extends_validity(
    store: SQLiteBackend, graph: GraphFactory, clock: ManualClock
) -> None:
    tsm, nvda = graph.company("TSM"), graph.company("NVDA")
    rel = store.record_relation(tsm.id, RelationType.SUPPLIES, nvda.id, 0.9)
    before = clock.advance(hours=1)
    clock.advance(hours=1)
    ended = store.supersede_relation(rel.id, valid_to=datetime(2026, 6, 1, tzinfo=UTC))
    assert ended.lineage_id == rel.lineage_id
    assert store.relations(nvda.id, direction="in", as_of=before)[0].valid_to is None
    assert store.relations(nvda.id, direction="in")[0].valid_to == datetime(2026, 6, 1, tzinfo=UTC)
    assert store.relations(tsm.id, direction="in") == []
    assert len(store.relations(tsm.id, direction="out", rel=RelationType.SUPPLIES)) == 1


# -- property tests -----------------------------------------------------------

# Each step: (minutes to advance, action) where action is supersede or retract.
_steps = st.lists(
    st.tuples(st.integers(min_value=0, max_value=120), st.sampled_from(["supersede", "retract"])),
    min_size=1,
    max_size=12,
)


@settings(max_examples=60, deadline=None)
@given(steps=_steps, probes=st.lists(st.integers(min_value=-10, max_value=2000), max_size=20))
def test_prop_as_of_never_returns_two_versions(
    steps: list[tuple[int, str]], probes: list[int]
) -> None:
    clock = ManualClock(T0)
    with SQLiteBackend.open(":memory:", clock=clock, ids=IdGenerator(clock)) as store:
        nvda = GraphFactory(store).company("NVDA")
        lineages = [
            store.record_claim(f"claim {i}", ClaimType.OPINION, 0.5, entities=[nvda.id]).lineage_id
            for i in range(2)
        ]
        for n, (minutes, action) in enumerate(steps):
            clock.advance(minutes=minutes)
            lineage = lineages[n % 2]
            current = store.current_claim(lineage)
            if current is None:
                continue
            if action == "supersede":
                store.supersede_claim(current.id, reason="r", text=f"{lineage} v{n}")
            else:
                store.retract_claim(current.id, reason="r")

        for offset in probes:
            t = T0 + timedelta(minutes=offset)
            seen = store.claims(entity_id=nvda.id, as_of=t)
            per_lineage = [c.lineage_id for c in seen]
            assert len(per_lineage) == len(set(per_lineage))
            for c in seen:
                assert c.recorded_at <= t
                assert c.superseded_at is None or c.superseded_at > t


@settings(max_examples=40, deadline=None)
@given(
    actions=st.lists(
        st.sampled_from(["claim", "supersede", "retract", "thesis_update"]), max_size=25
    )
)
def test_prop_history_is_append_only(actions: list[str]) -> None:
    clock = ManualClock(T0)
    with SQLiteBackend.open(":memory:", clock=clock, ids=IdGenerator(clock)) as store:
        g = GraphFactory(store)
        nvda = g.company("NVDA")
        thesis = g.thesis("NVDA wins", nvda)
        last = store.counts()
        for i, action in enumerate(actions):
            clock.advance(minutes=1)
            current = store.claims(entity_id=nvda.id, limit=1)
            if action == "claim" or not current:
                g.fact(f"fact {i}", nvda)
            elif action == "supersede":
                store.supersede_claim(current[0].id, reason="r", text=f"rev {i}")
            elif action == "retract":
                store.retract_claim(current[0].id, reason="r")
            else:
                store.update_thesis(thesis.id, reason="r", confidence=(i % 10) / 10)
            now = store.counts()
            assert now["claims"] >= last["claims"]
            assert now["thesis_history"] >= last["thesis_history"]
            last = now
