"""Thesis scorecard: prices, returns, outcomes and calibration (no network)."""

from __future__ import annotations

import json
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import pytest

from conftest import GraphFactory
from edgar_support import FakeFetcher
from torus.cli import main
from torus.core.clock import ManualClock
from torus.core.memo import build_memo, render_markdown
from torus.core.models import Outcome, Stance, ThesisStatus
from torus.core.scorecard import (
    calibration,
    decide_outcomes,
    refresh_prices,
    score,
    scorecard,
)
from torus.core.store import SQLiteBackend
from torus.sources.prices import YahooSource, parse_yahoo, read_price_csv
from torus.watchers import run_watchers
from torus.watchers.http import RateLimiter

D0 = date(2026, 1, 1)


def _series(start: float, moves: list[float]) -> list[tuple[date, float]]:
    rows, price = [], start
    for i, move in enumerate(moves):
        price *= 1 + move
        rows.append((D0 + timedelta(days=i), round(price, 4)))
    return rows


class FakeSource:
    name = "fake"

    def __init__(self, data: dict[str, list[tuple[date, float]]]) -> None:
        self.data = data
        self.calls: list[tuple[str, date, date]] = []

    def daily(self, symbol: str, start: date, end: date) -> list[tuple[date, float]]:
        self.calls.append((symbol, start, end))
        if symbol not in self.data:
            raise ValueError("HTTP 404")
        return [(d, c) for d, c in self.data[symbol] if start <= d <= end]


def test_parse_yahoo_and_csv() -> None:
    payload = {
        "chart": {
            "result": [
                {
                    "timestamp": [1767225600, 1767312000, 1767398400],
                    "indicators": {
                        "quote": [{"close": [100.0, None, 103.0]}],
                        "adjclose": [{"adjclose": [99.5, None, 102.5]}],
                    },
                }
            ]
        }
    }
    assert parse_yahoo(payload) == [(date(2026, 1, 1), 99.5), (date(2026, 1, 3), 102.5)]
    with pytest.raises(ValueError):
        parse_yahoo({"chart": {"result": []}})
    text = "Date,Open,Close,Adj Close\n2026-01-02,1,2,1.9\n01/05/2026,1,2.5,2.4\nbad,1,2,3\n"
    assert read_price_csv(text) == [(date(2026, 1, 2), 1.9), (date(2026, 1, 5), 2.4)]
    assert read_price_csv("2026-01-02,10\n2026-01-03,$11\n") == [
        (date(2026, 1, 2), 10.0),
        (date(2026, 1, 3), 11.0),
    ]


def test_yahoo_source_requests(tmp_path: Path) -> None:
    body = json.dumps(
        {
            "chart": {
                "result": [{"timestamp": [1767225600], "indicators": {"quote": [{"close": [5.0]}]}}]
            }
        }
    ).encode()

    class AnyFetcher(FakeFetcher):
        def get(self, url, headers):  # type: ignore[no-untyped-def]
            self.calls.append(url)
            from torus.watchers.http import Response

            return Response(url, 200, body)

    fetcher = AnyFetcher({})
    source = YahooSource(fetcher, RateLimiter(0.0, monotonic=lambda: 0.0, sleep=lambda s: None))
    assert source.daily("BRK.B", D0, D0) == [(D0, 5.0)]
    assert "/chart/BRK-B?" in fetcher.calls[0]


@pytest.fixture
def tracked(store: SQLiteBackend, graph: GraphFactory, clock: ManualClock) -> dict[str, str]:
    clock.set(datetime(2026, 1, 1, 15, 0, tzinfo=UTC))
    nvda, intc = graph.company("NVDA"), graph.company("INTC")
    bull = store.record_thesis("NVDA up", "s", Stance.BULLISH, 0.8, "30 days", nvda.id)
    bear = store.record_thesis("INTC down", "s", Stance.BEARISH, 0.6, "30 days", intc.id)
    store.set_thesis_tracking(bull.id, "NVDA")
    store.set_thesis_tracking(bear.id, "INTC", entry_price=50.0)
    # NVDA +10% then -5% then +20%; INTC rallies then falls; SPY +2%.
    store.upsert_prices("NVDA", _series(100, [0, 0.10, -0.05, 0.20]), "test")
    store.upsert_prices("INTC", _series(50, [0, 0.10, -0.20, 0.0]), "test")
    store.upsert_prices("SPY", _series(400, [0, 0.01, 0.0, 0.0099]), "test")
    return {"bull": bull.id, "bear": bear.id}


def test_score_bullish_and_bearish(
    store: SQLiteBackend, clock: ManualClock, tracked: dict[str, str]
) -> None:
    bull = score(store, store.get_thesis(tracked["bull"]), clock.now())
    assert bull.entry_price == 100 and bull.entry_day == D0
    assert bull.asset_return == pytest.approx(0.254, abs=1e-3)
    assert bull.benchmark_return == pytest.approx(0.02, abs=1e-3)
    assert bull.stance_return == pytest.approx(0.234, abs=1e-3)
    assert bull.max_drawdown == pytest.approx(-0.05, abs=1e-4)
    assert bull.days_to_horizon == 30 and bull.missing is None

    bear = score(store, store.get_thesis(tracked["bear"]), clock.now())
    assert bear.asset_return == pytest.approx(-0.12, abs=1e-3)
    assert bear.stance_return == pytest.approx(0.14, abs=1e-3)  # short INTC, long SPY
    assert bear.max_drawdown == pytest.approx(-0.0909, abs=1e-3)  # the rally hurt


def test_outcomes_and_calibration(
    store: SQLiteBackend, clock: ManualClock, tracked: dict[str, str], graph: GraphFactory
) -> None:
    assert decide_outcomes(store, clock.now()) == 0  # nothing due yet
    clock.advance(days=31)
    assert decide_outcomes(store, clock.now()) == 2  # both past horizon, both in the money
    outcomes = store.thesis_outcomes()
    assert {o.outcome for o in outcomes.values()} == {Outcome.RIGHT}
    assert {o.method for o in outcomes.values()} == {"horizon"}

    # Status beats price; a user outcome beats both.
    store.update_thesis(tracked["bear"], "thesis failed", status=ThesisStatus.BROKEN)
    decide_outcomes(store, clock.now())
    assert store.thesis_outcomes()[tracked["bear"]].outcome is Outcome.WRONG
    store.set_thesis_outcome(tracked["bear"], "right", "user", note="covered in time")
    decide_outcomes(store, clock.now())
    assert store.thesis_outcomes()[tracked["bear"]].method == "user"

    untracked = graph.thesis("Unpriced call", graph.company("ACME"))
    store.update_thesis(untracked.id, "played out", status=ThesisStatus.CONFIRMED)
    decide_outcomes(store, clock.now())
    cal = calibration(store)
    assert (cal.decided, cal.right) == (3, 3)
    # initial confidences 0.8, 0.6, 0.7; all right
    assert cal.brier == pytest.approx(((0.2**2) + (0.4**2) + (0.3**2)) / 3, abs=1e-4)
    bands = {b.label: b for b in cal.bands if b.theses}
    assert bands["80%-90%"].hit_rate == 1.0

    card = scorecard(store, clock.now())
    assert [s.symbol for s in card.theses] == ["NVDA", "INTC"]  # best stance return first


def test_refresh_prices_incremental(store: SQLiteBackend, tracked: dict[str, str]) -> None:
    source = FakeSource({"NVDA": _series(100, [0, 0.01, 0.01, 0.01, 0.01, 0.01])})
    result = refresh_prices(store, source, D0 + timedelta(days=5))
    # NVDA already had closes to day 3, so only days 3-5 are fetched.
    assert result["NVDA"] == 3 and str(result["SPY"]).startswith("error")
    first_start = next(start for sym, start, _ in source.calls if sym == "NVDA")
    assert first_start == D0 + timedelta(days=3)


def test_memo_includes_scorecard(
    store: SQLiteBackend, clock: ManualClock, tracked: dict[str, str]
) -> None:
    text = render_markdown(build_memo(store, tracked["bull"], clock.now()))
    assert "## Scorecard" in text and "Tracking **NVDA** against SPY" in text


def test_watcher_refreshes_tracked_prices(store: SQLiteBackend, tracked: dict[str, str]) -> None:
    report = run_watchers(store, fetcher=FakeFetcher({}), edgar=False, rss=False)
    [prices] = [s for s in report.sources if s.source == "prices"]
    assert prices.error is not None  # every symbol failed (404s), reported not raised
    store.set_setting("price_source", "none")
    report = run_watchers(store, fetcher=FakeFetcher({}), edgar=False, rss=False)
    assert not [s for s in report.sources if s.source == "prices"]


def test_cli_scorecard(
    torus_home: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert main(["init"]) == 0
    with SQLiteBackend.open(torus_home / "torus.db") as store:
        nvda = GraphFactory(store).company("NVDA")
        thesis = GraphFactory(store).thesis("NVDA thesis", nvda)
    prices = tmp_path / "nvda.csv"
    prices.write_text("date,close\n2026-01-01,100\n2026-01-02,110\n", encoding="utf-8")
    assert main(["scorecard"]) == 0
    assert "No theses tracked" in capsys.readouterr().out
    assert main(["scorecard", "track", thesis.id]) == 0
    assert main(["scorecard", "import", "NVDA", str(prices)]) == 0
    assert main(["scorecard", "outcome", thesis.id, "right", "--note", "early"]) == 0
    assert main(["scorecard"]) == 0
    out = capsys.readouterr().out
    assert "Tracking NVDA thesis as NVDA" in out and "Imported 2 closes" in out
    assert "Calibration: 1/1 right" in out
    assert main(["scorecard", "outcome", thesis.id, "clear"]) == 0
