from __future__ import annotations

from datetime import UTC, datetime, timedelta, timezone

import pytest

from torus.core.clock import ManualClock, from_ts, to_ts
from torus.core.ids import IdGenerator


def test_timestamp_roundtrip_and_utc_normalisation() -> None:
    ist = timezone(timedelta(hours=5, minutes=30))
    local = datetime(2026, 10, 21, 15, 0, tzinfo=ist)
    ts = to_ts(local)
    assert ts == "2026-10-21T09:30:00.000000Z"
    assert from_ts(ts) == local


def test_timestamps_sort_lexicographically() -> None:
    a = datetime(2026, 1, 1, 9, 0, 0, 5, tzinfo=UTC)
    b = datetime(2026, 1, 1, 9, 0, 0, 50, tzinfo=UTC)
    c = datetime(2026, 1, 1, 10, 0, tzinfo=UTC)
    assert sorted([to_ts(c), to_ts(b), to_ts(a)]) == [to_ts(a), to_ts(b), to_ts(c)]


def test_naive_datetimes_rejected() -> None:
    with pytest.raises(ValueError):
        to_ts(datetime(2026, 1, 1))


def test_manual_clock_advances() -> None:
    clock = ManualClock(datetime(2026, 1, 1, tzinfo=UTC))
    clock.advance(days=2)
    assert clock.now() == datetime(2026, 1, 3, tzinfo=UTC)


def test_ids_are_26_chars_unique_and_sorted_within_same_ms() -> None:
    clock = ManualClock()
    gen = IdGenerator(clock)
    ids = [gen.new() for _ in range(1000)]
    assert all(len(i) == 26 for i in ids)
    assert len(set(ids)) == 1000
    assert ids == sorted(ids)


def test_ids_sort_by_time() -> None:
    clock = ManualClock()
    gen = IdGenerator(clock)
    first = gen.new()
    clock.advance(milliseconds=1)
    second = gen.new()
    assert first < second


def test_ids_stay_monotonic_when_clock_goes_backwards() -> None:
    clock = ManualClock(datetime(2026, 1, 2, tzinfo=UTC))
    gen = IdGenerator(clock)
    first = gen.new()
    clock.set(datetime(2026, 1, 1, tzinfo=UTC))
    assert gen.new() > first


def test_random_overflow_rolls_to_next_ms() -> None:
    clock = ManualClock()
    gen = IdGenerator(clock, randbits=lambda bits: (1 << bits) - 1)
    first = gen.new()
    second = gen.new()
    assert second > first
