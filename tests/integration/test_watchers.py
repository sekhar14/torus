"""Watchers against recorded EDGAR and feed responses (no network)."""

from __future__ import annotations

import json
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path

import pytest

from conftest import FIXTURES, GraphFactory
from torus.core.clock import ManualClock
from torus.core.models import Alias, AliasType, EntityKind, EventKind, RelationType
from torus.core.reference import ReferenceData
from torus.core.store import SQLiteBackend
from torus.watchers import run_watchers
from torus.watchers.edgar import EdgarWatcher, describe, parse_submissions
from torus.watchers.http import FetchError, RateLimiter, Response
from torus.watchers.rss import RssWatcher, parse_feed

NOW = datetime(2026, 1, 1, 9, 0, tzinfo=UTC)
NVDA_URL = "https://data.sec.gov/submissions/CIK0001045810.json"
TSM_URL = "https://data.sec.gov/submissions/CIK0001046179.json"
FEED_URL = "https://news.example.com/feed.rss"
ATOM_URL = "https://notes.example.com/feed.atom"


class FakeFetcher:
    """Replays recorded responses; unknown URLs are 404, ``errors`` raise."""

    def __init__(self, routes: Mapping[str, bytes], errors: set[str] | None = None) -> None:
        self.routes = dict(routes)
        self.errors = errors or set()
        self.calls: list[tuple[str, dict[str, str]]] = []

    def get(self, url: str, headers: Mapping[str, str]) -> Response:
        self.calls.append((url, dict(headers)))
        if url in self.errors:
            raise FetchError(f"{url}: connection refused")
        if url not in self.routes:
            return Response(url, 404, b"not found")
        return Response(url, 200, self.routes[url])


def _fixture(*parts: str) -> bytes:
    return FIXTURES.joinpath(*parts).read_bytes()


ROUTES = {
    NVDA_URL: _fixture("edgar", "CIK0001045810.json"),
    TSM_URL: _fixture("edgar", "CIK0001046179.json"),
    FEED_URL: _fixture("feeds", "semis.rss"),
    ATOM_URL: _fixture("feeds", "semis.atom"),
}


@pytest.fixture
def clock() -> ManualClock:
    return ManualClock(NOW)


@pytest.fixture
def companies(store: SQLiteBackend) -> dict[str, str]:
    def company(name: str, ticker: str, cik: str, abbreviation: str | None = None) -> str:
        aliases = [Alias(alias=ticker, alias_type=AliasType.TICKER)]
        if abbreviation:
            aliases.append(Alias(alias=abbreviation, alias_type=AliasType.ABBREVIATION))
        entity = store.upsert_entity(
            EntityKind.COMPANY, name, canonical_key=f"cik:{cik}", aliases=aliases
        )
        return entity.id

    nvda = company("NVIDIA CORP", "NVDA", "0001045810")
    store.add_alias(nvda, "Nvidia", AliasType.NAME)
    tsm = company("TAIWAN SEMICONDUCTOR MANUFACTURING CO LTD", "TSM", "0001046179", "TSMC")
    return {"nvda": nvda, "tsm": tsm}


def _edgar(store: SQLiteBackend, fetcher: FakeFetcher, clock: ManualClock) -> EdgarWatcher:
    limiter = RateLimiter(0.0, monotonic=lambda: 0.0, sleep=lambda s: None)
    return EdgarWatcher(store, fetcher, "me@example.com", clock, limiter=limiter)


# -- EDGAR ---------------------------------------------------------------------------


def test_parse_submissions_and_describe() -> None:
    name, filings = parse_submissions(json.loads(ROUTES[NVDA_URL]))
    assert name == "NVIDIA CORP" and len(filings) == 5
    first = filings[0]
    assert first.items == ("2.02", "9.01")
    assert first.url == (
        "https://www.sec.gov/Archives/edgar/data/1045810/000104581025000301/nvda-20251230.htm"
    )
    assert first.occurred_at == datetime(2025, 12, 30, 21, 5, 11, tzinfo=UTC)
    kind, title = describe(first, name)
    assert kind is EventKind.EARNINGS
    assert title == "NVIDIA CORP 8-K: results of operations; financial statements and exhibits"
    assert describe(filings[2], name) == (EventKind.EARNINGS, "NVIDIA CORP 10-Q: quarterly report")
    with pytest.raises(ValueError):
        parse_submissions({"nope": 1})


def test_edgar_incremental(
    store: SQLiteBackend, clock: ManualClock, companies: dict[str, str]
) -> None:
    fetcher = FakeFetcher(ROUTES)
    watcher = _edgar(store, fetcher, clock)
    nvda = store.get_entity(companies["nvda"])

    first = watcher.check(nvda)
    # 7-day backfill: 8-K (12-30), 10-Q (12-26); Form 4 and SC 13G/A filtered; 12-10 too old.
    assert (first.checked, first.new_events, first.skipped, first.error) == (5, 2, 2, None)
    titles = sorted(e.title for e in store.events())
    assert titles == [
        "NVIDIA CORP 10-Q: quarterly report",
        "NVIDIA CORP 8-K: results of operations; financial statements and exhibits",
    ]
    assert all(e.source == "edgar" and e.entity_ids == (nvda.id,) for e in store.events())

    assert watcher.check(nvda).new_events == 0  # nothing new

    data = json.loads(ROUTES[NVDA_URL])
    recent = data["filings"]["recent"]
    for column, value in {
        "accessionNumber": "0001045810-25-000302",
        "filingDate": "2025-12-30",  # same day as the last one seen
        "reportDate": "2025-12-30",
        "acceptanceDateTime": "2025-12-30T23:00:00.000Z",
        "form": "8-K",
        "primaryDocument": "nvda-20251230b.htm",
        "primaryDocDescription": "8-K",
        "items": "5.02",
    }.items():
        recent[column].insert(0, value)
    fetcher.routes[NVDA_URL] = json.dumps(data).encode()
    third = watcher.check(nvda)
    assert third.new_events == 1
    assert store.events(limit=1)[0].title == "NVIDIA CORP 8-K: executive or director change"


def test_edgar_sets_user_agent_and_rate_limits(
    store: SQLiteBackend, clock: ManualClock, companies: dict[str, str]
) -> None:
    fetcher = FakeFetcher(ROUTES)
    ticks = iter([0.0, 0.02, 0.125])  # first request at 0.0, second asks at 0.02
    slept: list[float] = []
    limiter = RateLimiter(0.125, monotonic=lambda: next(ticks), sleep=slept.append)
    watcher = EdgarWatcher(store, fetcher, "me@example.com", clock, limiter=limiter)
    watcher.check(store.get_entity(companies["nvda"]))
    watcher.check(store.get_entity(companies["tsm"]))
    assert [url for url, _ in fetcher.calls] == [NVDA_URL, TSM_URL]
    assert all(h["User-Agent"].endswith(" me@example.com") for _, h in fetcher.calls)
    assert slept == [pytest.approx(0.105)]


def test_edgar_requires_contact_email(store: SQLiteBackend, clock: ManualClock) -> None:
    with pytest.raises(ValueError):
        EdgarWatcher(store, FakeFetcher({}), "anonymous", clock)


def test_supplier_filing_alerts_customer_thesis(
    store: SQLiteBackend, clock: ManualClock, companies: dict[str, str]
) -> None:
    store.record_relation(companies["tsm"], RelationType.SUPPLIES, companies["nvda"], 0.9)
    thesis = store.record_thesis(
        "NVDA supply-bound", "s", "bullish", 0.7, subject_entity_id=companies["nvda"]
    )
    report = _edgar(store, FakeFetcher(ROUTES), clock).check(store.get_entity(companies["tsm"]))
    assert (report.new_events, report.alerts) == (1, 1)
    [alert] = store.alerts()
    assert alert.thesis_id == thesis.id
    event = store.get_event(alert.event_id)
    assert event.title == ("TAIWAN SEMICONDUCTOR MANUFACTURING CO LTD 6-K: foreign issuer report")
    assert event.summary == "CoWoS capacity update"


def test_edgar_http_error_writes_nothing(
    store: SQLiteBackend, clock: ManualClock, companies: dict[str, str]
) -> None:
    report = _edgar(store, FakeFetcher({}), clock).check(store.get_entity(companies["nvda"]))
    assert report.error == "HTTP 404 for CIK 0001045810"
    assert store.events() == [] and store.get_state("edgar:0001045810") is None


def test_edgar_cik_from_reference_by_ticker(
    store: SQLiteBackend, clock: ManualClock, reference: ReferenceData
) -> None:
    plain = store.upsert_entity(
        EntityKind.COMPANY,
        "Nvidia",
        aliases=[Alias(alias="NVDA", alias_type=AliasType.TICKER)],
    )
    no_ref = _edgar(store, FakeFetcher(ROUTES), clock).check(plain)
    assert no_ref.error is not None and "no CIK" in no_ref.error
    watcher = EdgarWatcher(
        store,
        FakeFetcher(ROUTES),
        "me@example.com",
        clock,
        limiter=RateLimiter(0.0),
        reference=reference,
    )
    assert watcher.check(plain).new_events == 2


# -- RSS / Atom ---------------------------------------------------------------------


def test_parse_rss_and_atom() -> None:
    rss = parse_feed(ROUTES[FEED_URL])
    assert [i.title for i in rss][:2] == [
        "TSMC lifts CoWoS output as AI demand stays hot",
        "Nvidia shares slip after export-rule chatter",
    ]
    assert rss[0].summary == "Taiwan's foundry leader is adding advanced packaging lines."
    assert rss[1].published == datetime(2025, 12, 31, 15, 30, tzinfo=UTC)
    atom = parse_feed(ROUTES[ATOM_URL])
    assert atom[0].title == "NVDA supplier & TSMC capex rises"
    assert atom[0].link == "https://notes.example.com/capex"  # alternate, not self
    assert atom[1].published == datetime(2025, 12, 31, 11, 0, tzinfo=UTC)
    assert atom[1].summary == "Rates and the dollar."


@pytest.mark.parametrize(
    "body",
    [
        b'<?xml version="1.0"?><!DOCTYPE lolz [<!ENTITY lol "lol">]><rss><channel/></rss>',
        b"<rss><channel><item><title>unterminated",
    ],
)
def test_parse_feed_rejects_dtds_and_garbage(body: bytes) -> None:
    with pytest.raises(ValueError):
        parse_feed(body)


def test_rss_watcher_maps_items_to_entities(
    store: SQLiteBackend, clock: ManualClock, companies: dict[str, str]
) -> None:
    watcher = RssWatcher(store, FakeFetcher(ROUTES), clock)
    report = watcher.check(FEED_URL, "Semis Wire")
    # TSMC + Nvidia items match; coffee is unrelated; the Dec 1 story is too old.
    assert (report.checked, report.new_events, report.skipped) == (4, 2, 2)
    by_title = {e.title: e for e in store.events()}
    assert by_title["TSMC lifts CoWoS output as AI demand stays hot"].entity_ids == (
        companies["tsm"],
    )
    assert by_title["Nvidia shares slip after export-rule chatter"].entity_ids == (
        companies["nvda"],
    )
    assert {e.source for e in store.events()} == {"rss:Semis Wire"}
    assert watcher.check(FEED_URL, "Semis Wire").new_events == 0  # links dedupe


def test_rss_second_run_skips_items_older_than_last_run(
    store: SQLiteBackend, clock: ManualClock, companies: dict[str, str]
) -> None:
    watcher = RssWatcher(store, FakeFetcher(ROUTES), clock)
    watcher.check(FEED_URL)
    clock.advance(days=3)
    again = watcher.check(FEED_URL)
    # Only items newer than (last run - 1 day of slack) are looked at again: the
    # Dec 31 Nvidia story is re-matched but dedupes; coffee and older items are skipped.
    assert (again.new_events, again.skipped) == (0, 3)


def test_atom_item_mentioning_two_entities(
    store: SQLiteBackend, clock: ManualClock, companies: dict[str, str]
) -> None:
    RssWatcher(store, FakeFetcher(ROUTES), clock).check(ATOM_URL)
    [event] = store.events()
    assert set(event.entity_ids) == {companies["nvda"], companies["tsm"]}


# -- runner -------------------------------------------------------------------------


def test_run_watchers_follows_open_theses_and_feeds(
    store: SQLiteBackend, clock: ManualClock, companies: dict[str, str]
) -> None:
    store.record_thesis("NVDA view", "s", "bullish", 0.6, subject_entity_id=companies["nvda"])
    store.set_setting("sec_contact", "me@example.com")
    store.add_feed(FEED_URL, "Semis Wire")
    fetcher = FakeFetcher(ROUTES)
    report = run_watchers(store, fetcher, clock, limiter=RateLimiter(0.0), edgar=True, rss=True)
    assert [s.source for s in report.sources] == ["edgar:NVIDIA CORP", "rss:Semis Wire"]
    assert report.errors == []
    assert report.new_events == 4  # 2 filings + 2 feed items
    assert report.alerts >= 1  # the Nvidia filing and news reach the NVDA thesis


def test_one_failing_source_does_not_stop_the_rest(
    store: SQLiteBackend, clock: ManualClock, companies: dict[str, str]
) -> None:
    store.watch_entity(companies["nvda"])
    store.watch_entity(companies["tsm"])
    store.set_setting("sec_contact", "me@example.com")
    fetcher = FakeFetcher(ROUTES, errors={NVDA_URL})
    report = run_watchers(store, fetcher, clock, limiter=RateLimiter(0.0), rss=False)
    errors = {s.source: s.error for s in report.sources}
    assert errors["edgar:NVIDIA CORP"] is not None and "network" in errors["edgar:NVIDIA CORP"]
    assert errors["edgar:TAIWAN SEMICONDUCTOR MANUFACTURING CO LTD"] is None
    assert report.new_events == 1


def test_missing_contact_is_reported_not_raised(
    store: SQLiteBackend,
    clock: ManualClock,
    companies: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("TORUS_SEC_CONTACT", raising=False)
    store.watch_entity(companies["nvda"])
    report = run_watchers(store, FakeFetcher(ROUTES), clock, rss=False)
    [source] = report.sources
    assert source.error is not None and "sec_contact" in source.error


def test_watch_targets(store: SQLiteBackend, graph: GraphFactory) -> None:
    nvda, tsm, amd, asml = (graph.company(t) for t in ("NVDA", "TSM", "AMD", "ASML"))
    store.watch_entity(amd.id)
    thesis = graph.thesis("t", nvda)
    store.revise_thesis(thesis.id, "watch", add_triggers=[("TSMC delay", tsm.id)])
    closed = graph.thesis("closed", asml)
    store.update_thesis(closed.id, "done", status="confirmed")  # type: ignore[arg-type]
    assert {e.id for e in store.watch_targets()} == {nvda.id, tsm.id, amd.id}
    assert store.unwatch_entity(amd.id) and not store.unwatch_entity(amd.id)


# -- CLI ----------------------------------------------------------------------------


def test_cli_watch_feed_config_and_run(
    torus_home: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    import shutil

    import torus.watchers as watchers
    from torus.cli import main

    main(["init"])
    shutil.copy(
        FIXTURES / "sec_company_tickers.json", torus_home / "reference" / "sec_company_tickers.json"
    )
    assert main(["config", "set", "sec_contact", "not-an-email"]) == 2
    assert main(["config", "set", "sec_contact", "me@example.com"]) == 0
    assert main(["config", "get", "nope"]) == 2
    assert main(["watch", "add", "NVDA"]) == 0
    assert main(["feed", "add", FEED_URL, "--name", "Semis Wire"]) == 0
    capsys.readouterr()
    assert main(["watch", "list"]) == 0
    listing = capsys.readouterr().out
    assert "NVIDIA CORP" in listing and "(watchlist)" in listing and FEED_URL in listing

    fetcher = FakeFetcher(ROUTES)
    monkeypatch.setattr(watchers, "UrllibFetcher", lambda: fetcher)
    assert main(["watch", "run", "--json"]) == 0
    sources = {s["source"]: s for s in json.loads(capsys.readouterr().out)}
    # The CLI runs on the real clock, so these 2025 fixtures are outside the
    # backfill window: check that both sources were read without errors.
    assert (sources["edgar:NVIDIA CORP"]["error"], sources["edgar:NVIDIA CORP"]["checked"]) == (
        None,
        5,
    )
    assert (sources["rss:Semis Wire"]["error"], sources["rss:Semis Wire"]["checked"]) == (None, 4)

    fetcher.errors.add(FEED_URL)
    assert main(["watch", "run"]) == 1  # any source error -> non-zero exit
    assert main(["watch", "remove", "NVDA"]) == 0
    assert main(["watch", "remove", "NVDA"]) == 1
    assert main(["feed", "remove", FEED_URL]) == 0


def test_feed_url_must_be_http(store: SQLiteBackend) -> None:
    with pytest.raises(ValueError):
        store.add_feed("file:///etc/passwd")
