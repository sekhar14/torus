"""ULID generation (time-sortable, 26-char Crockford base32).

IDs generated within the same millisecond are monotonic: the random part is
incremented instead of redrawn, so sort order always matches creation order.
"""

from __future__ import annotations

import secrets
from collections.abc import Callable

from torus.core.clock import Clock, SystemClock

_ALPHABET = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"
_RANDOM_BITS = 80
_RANDOM_MAX = (1 << _RANDOM_BITS) - 1


def _encode(value: int, length: int) -> str:
    chars = []
    for _ in range(length):
        chars.append(_ALPHABET[value & 31])
        value >>= 5
    return "".join(reversed(chars))


class IdGenerator:
    def __init__(
        self,
        clock: Clock | None = None,
        randbits: Callable[[int], int] = secrets.randbits,
    ) -> None:
        self._clock = clock or SystemClock()
        self._randbits = randbits
        self._last_ms = -1
        self._last_rand = 0

    def new(self) -> str:
        ms = int(self._clock.now().timestamp() * 1000)
        if ms <= self._last_ms:
            # Same (or earlier) millisecond: stay monotonic.
            ms = self._last_ms
            if self._last_rand == _RANDOM_MAX:
                ms += 1
                rand = self._randbits(_RANDOM_BITS)
            else:
                rand = self._last_rand + 1
        else:
            rand = self._randbits(_RANDOM_BITS)
        self._last_ms, self._last_rand = ms, rand
        return _encode(ms, 10) + _encode(rand, 16)
