"""Injectable clocks and the canonical timestamp format.

Core code never calls ``datetime.now()`` directly; it asks a ``Clock``. Tests use
``ManualClock`` to make temporal behaviour deterministic.

Timestamps are stored as fixed-width UTC strings (``2026-10-21T09:30:00.000000Z``)
so lexicographic order equals chronological order in SQLite.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Protocol

_FORMAT = "%Y-%m-%dT%H:%M:%S.%fZ"


class Clock(Protocol):
    def now(self) -> datetime: ...


class SystemClock:
    def now(self) -> datetime:
        return datetime.now(UTC)


class ManualClock:
    """A clock that only moves when told to."""

    def __init__(self, start: datetime | None = None) -> None:
        self._now = require_aware(start or datetime(2026, 1, 1, tzinfo=UTC))

    def now(self) -> datetime:
        return self._now

    def set(self, value: datetime) -> None:
        self._now = require_aware(value)

    def advance(self, delta: timedelta | None = None, **kwargs: float) -> datetime:
        self._now += delta if delta is not None else timedelta(**kwargs)
        return self._now


def require_aware(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"timestamp must be timezone-aware: {value!r}")
    return value


def to_ts(value: datetime) -> str:
    return require_aware(value).astimezone(UTC).strftime(_FORMAT)


def to_ts_opt(value: datetime | None) -> str | None:
    return None if value is None else to_ts(value)


def from_ts(value: str) -> datetime:
    return datetime.strptime(value, _FORMAT).replace(tzinfo=UTC)


def from_ts_opt(value: str | None) -> datetime | None:
    return None if value is None else from_ts(value)
