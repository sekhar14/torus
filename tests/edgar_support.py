"""Recorded SEC responses for tests that read filings (no network)."""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from pathlib import Path

from torus.sources.edgar_docs import EdgarDocs
from torus.watchers.http import RateLimiter, Response

FILINGS = Path(__file__).parent / "fixtures" / "filings"
ACME_CIK = "0000999001"
ACME_SUBMISSIONS = f"https://data.sec.gov/submissions/CIK{ACME_CIK}.json"
ARCHIVE = "https://www.sec.gov/Archives/edgar/data/999001"

# (accession, filed, form, document, fixture file)
ACME_FILINGS: Sequence[tuple[str, str, str, str, str]] = (
    ("0000999001-26-000040", "2026-04-28", "10-Q", "acme-20260331.htm", "acme-10q.htm"),
    ("0000999001-26-000010", "2026-02-20", "10-K", "acme-20251231.htm", "acme-10k-2025.htm"),
    ("0000999001-25-000012", "2025-02-21", "10-K", "acme-20241231.htm", "acme-10k-2024.htm"),
)


class FakeFetcher:
    """Replays recorded responses; unknown URLs are 404."""

    def __init__(self, routes: Mapping[str, bytes]) -> None:
        self.routes = dict(routes)
        self.calls: list[str] = []

    def get(self, url: str, headers: Mapping[str, str]) -> Response:
        self.calls.append(url)
        if url not in self.routes:
            return Response(url, 404, b"not found")
        return Response(url, 200, self.routes[url])


def acme_submissions() -> bytes:
    rows = list(ACME_FILINGS)
    recent = {
        "accessionNumber": [r[0] for r in rows],
        "filingDate": [r[1] for r in rows],
        "acceptanceDateTime": [f"{r[1]}T21:00:00.000Z" for r in rows],
        "form": [r[2] for r in rows],
        "primaryDocument": [r[3] for r in rows],
        "primaryDocDescription": [r[2] for r in rows],
        "items": ["" for _ in rows],
    }
    return json.dumps(
        {"cik": ACME_CIK, "name": "ACME CHIPS INC", "filings": {"recent": recent}}
    ).encode()


def acme_routes() -> dict[str, bytes]:
    routes = {ACME_SUBMISSIONS: acme_submissions()}
    for accession, _, _, document, fixture in ACME_FILINGS:
        url = f"{ARCHIVE}/{accession.replace('-', '')}/{document}"
        routes[url] = (FILINGS / fixture).read_bytes()
    return routes


def acme_docs(cache_dir: Path, fetcher: FakeFetcher | None = None) -> EdgarDocs:
    limiter = RateLimiter(0.0, monotonic=lambda: 0.0, sleep=lambda s: None)
    return EdgarDocs(
        fetcher or FakeFetcher(acme_routes()), "me@example.com", cache_dir, limiter=limiter
    )
