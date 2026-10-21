"""The real urllib fetcher against a throwaway local HTTP server (no internet)."""

from __future__ import annotations

import threading
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import ClassVar

import pytest

from torus.watchers.http import FetchError, UrllibFetcher, user_agent


class _Handler(BaseHTTPRequestHandler):
    seen_agents: ClassVar[list[str]] = []

    def do_GET(self) -> None:
        _Handler.seen_agents.append(self.headers.get("User-Agent", ""))
        if self.path == "/feed":
            body = b"<rss><channel/></rss>"
            self.send_response(200)
        else:
            body = b"missing"
            self.send_response(404)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args: object) -> None:  # keep test output quiet
        pass


@pytest.fixture
def server() -> Iterator[str]:
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{httpd.server_address[1]}"
    httpd.shutdown()
    httpd.server_close()


def test_fetch_ok_and_not_found(server: str) -> None:
    fetcher = UrllibFetcher(timeout=5)
    ok = fetcher.get(f"{server}/feed", {"User-Agent": user_agent("me@example.com")})
    assert (ok.status, ok.ok, ok.body) == (200, True, b"<rss><channel/></rss>")
    missing = fetcher.get(f"{server}/nope", {})
    assert (missing.status, missing.ok) == (404, False)
    assert _Handler.seen_agents[0].endswith(" me@example.com")


def test_connection_failure_raises_fetch_error() -> None:
    with pytest.raises(FetchError):
        UrllibFetcher(timeout=2).get("http://127.0.0.1:9/unreachable", {})
