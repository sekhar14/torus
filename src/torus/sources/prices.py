"""Daily closing prices for the scorecard.

The default source is Yahoo Finance's public chart endpoint: no key, adjusted
closes, but unofficial, so it can change or rate-limit without notice. Prices can
also be imported from a CSV (``date,close`` per row, per symbol), and the setting
``price_source = none`` turns fetching off.
"""

from __future__ import annotations

import csv
import io
import json
import urllib.parse
from datetime import UTC, date, datetime
from typing import Protocol

from torus.watchers.http import Fetcher, RateLimiter, user_agent

YAHOO_URL = "https://query1.finance.yahoo.com/v8/finance/chart/{symbol}?{query}"
MIN_INTERVAL = 0.5


class PriceSource(Protocol):
    name: str

    def daily(self, symbol: str, start: date, end: date) -> list[tuple[date, float]]: ...


def yahoo_symbol(ticker: str) -> str:
    """Yahoo writes share classes with a dash: BRK.B -> BRK-B."""
    return ticker.strip().upper().replace(".", "-")


class YahooSource:
    name = "yahoo"

    def __init__(self, fetcher: Fetcher, limiter: RateLimiter | None = None) -> None:
        self.fetcher = fetcher
        self.limiter = limiter or RateLimiter(MIN_INTERVAL)
        self.headers = {"User-Agent": f"Mozilla/5.0 ({user_agent()})", "Accept": "application/json"}

    def daily(self, symbol: str, start: date, end: date) -> list[tuple[date, float]]:
        query = urllib.parse.urlencode(
            {
                "period1": int(datetime.combine(start, datetime.min.time(), UTC).timestamp()),
                "period2": int(datetime.combine(end, datetime.max.time(), UTC).timestamp()),
                "interval": "1d",
                "events": "div,split",
            }
        )
        self.limiter.wait()
        url = YAHOO_URL.format(symbol=urllib.parse.quote(yahoo_symbol(symbol)), query=query)
        response = self.fetcher.get(url, self.headers)
        if not response.ok:
            raise ValueError(f"HTTP {response.status} for {symbol} prices")
        return parse_yahoo(json.loads(response.body))


def parse_yahoo(payload: object) -> list[tuple[date, float]]:
    try:
        result = payload["chart"]["result"][0]  # type: ignore[index]
        stamps = result.get("timestamp") or []
        indicators = result["indicators"]
        closes = (indicators.get("adjclose") or [{}])[0].get("adjclose") or indicators["quote"][0][
            "close"
        ]
    except (KeyError, IndexError, TypeError) as exc:
        raise ValueError(f"unreadable price data: {exc}") from exc
    rows: dict[date, float] = {}
    for stamp, close in zip(stamps, closes, strict=False):
        if close is not None and close > 0:
            rows[datetime.fromtimestamp(int(stamp), UTC).date()] = float(close)
    return sorted(rows.items())


def read_price_csv(text: str) -> list[tuple[date, float]]:
    """``date,close`` rows (a header row and extra columns are fine; the close is
    the column named close/adj close, else the last numeric one)."""
    rows = list(csv.reader(io.StringIO(text)))
    if not rows:
        return []
    header = [c.strip().casefold() for c in rows[0]]
    column = next(
        (header.index(name) for name in ("adj close", "adj_close", "close") if name in header),
        None,
    )
    body = rows[1:] if column is not None or not _is_date(rows[0][0]) else rows
    found: dict[date, float] = {}
    for row in body:
        if not row or not _is_date(row[0]):
            continue
        cells = row[1:] if column is None else [row[column]]
        values = [_number(c) for c in cells]
        numeric = [v for v in values if v is not None]
        if numeric and numeric[-1] > 0:
            found[_parse_date(row[0])] = numeric[-1]
    return sorted(found.items())


def _number(cell: str) -> float | None:
    try:
        return float(cell.strip().lstrip("$").replace(",", ""))
    except ValueError:
        return None


def _is_date(cell: str) -> bool:
    try:
        _parse_date(cell)
    except ValueError:
        return False
    return True


def _parse_date(cell: str) -> date:
    text = cell.strip()
    for fmt in ("%Y-%m-%d", "%m/%d/%Y", "%d.%m.%Y", "%Y%m%d"):
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            continue
    raise ValueError(f"not a date: {cell!r}")
