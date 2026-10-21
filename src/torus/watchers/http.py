"""Minimal HTTP for watchers: an injectable fetcher and a polite rate limiter.

Watchers never import urllib directly, so tests replay recorded responses and
run with no network.
"""

from __future__ import annotations

import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Protocol

from torus import __version__

MAX_RESPONSE_BYTES = 20_000_000
DEFAULT_TIMEOUT = 30.0


class FetchError(Exception):
    """The request could not be made or completed (DNS, timeout, refused...)."""


@dataclass(frozen=True)
class Response:
    url: str
    status: int
    body: bytes

    @property
    def ok(self) -> bool:
        return 200 <= self.status < 300


class Fetcher(Protocol):
    def get(self, url: str, headers: Mapping[str, str]) -> Response: ...


class UrllibFetcher:
    def __init__(self, timeout: float = DEFAULT_TIMEOUT) -> None:
        self.timeout = timeout

    def get(self, url: str, headers: Mapping[str, str]) -> Response:
        import urllib.error
        import urllib.request

        request = urllib.request.Request(url, headers=dict(headers))
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as resp:
                body = resp.read(MAX_RESPONSE_BYTES + 1)
                if len(body) > MAX_RESPONSE_BYTES:
                    raise FetchError(
                        f"response from {url} is larger than {MAX_RESPONSE_BYTES} bytes"
                    )
                return Response(url, int(resp.status), body)
        except urllib.error.HTTPError as exc:
            return Response(url, exc.code, exc.read(64_000) if exc.fp else b"")
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise FetchError(f"{url}: {exc}") from exc


class RateLimiter:
    """Space requests at least ``min_interval`` seconds apart (SEC: max 10/s)."""

    def __init__(
        self,
        min_interval: float,
        monotonic: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.min_interval = min_interval
        self._monotonic = monotonic
        self._sleep = sleep
        self._last: float | None = None

    def wait(self) -> None:
        if self._last is not None:
            remaining = self.min_interval - (self._monotonic() - self._last)
            if remaining > 0:
                self._sleep(remaining)
        self._last = self._monotonic()


def user_agent(contact: str | None = None) -> str:
    """``torus/<version> <contact>``; the SEC requires a contact email."""
    return f"torus/{__version__}" + (f" {contact}" if contact else "")
