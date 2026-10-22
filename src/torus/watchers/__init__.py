"""Watchers turn the outside world into events: SEC filings for watched companies
(EDGAR) and RSS/Atom feed items that mention graph entities. Events go through
impact analysis, so a filing can raise alerts on theses without anyone asking.

Run one pass with ``torus watch run`` (from cron or Windows Task Scheduler).
Watchers use no LLM.
"""

from __future__ import annotations

import functools
import os
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from torus.core.clock import Clock, SystemClock
from torus.core.detect import EntityDetector
from torus.core.reference import ReferenceData
from torus.core.store.base import Backend
from torus.watchers.edgar import EdgarWatcher, Filing, SourceReport
from torus.watchers.http import Fetcher, FetchError, RateLimiter, UrllibFetcher
from torus.watchers.rss import RssWatcher

SEC_CONTACT_SETTING = "sec_contact"
FILING_DIFFS_SETTING = "filing_diffs"  # on (default) | off
ENV_SEC_CONTACT = "TORUS_SEC_CONTACT"

__all__ = ["SourceReport", "WatchReport", "run_watchers", "sec_contact"]


@dataclass
class WatchReport:
    sources: list[SourceReport] = field(default_factory=list)

    @property
    def new_events(self) -> int:
        return sum(s.new_events for s in self.sources)

    @property
    def alerts(self) -> int:
        return sum(s.alerts for s in self.sources)

    @property
    def errors(self) -> list[SourceReport]:
        return [s for s in self.sources if s.error]


def sec_contact(store: Backend) -> str | None:
    return store.get_setting(SEC_CONTACT_SETTING) or os.environ.get(ENV_SEC_CONTACT) or None


def run_watchers(
    store: Backend,
    fetcher: Fetcher | None = None,
    clock: Clock | None = None,
    reference: ReferenceData | None = None,
    limiter: RateLimiter | None = None,
    edgar: bool = True,
    rss: bool = True,
    home: Path | None = None,
    prices: bool = True,
) -> WatchReport:
    """One pass over every watch target and feed. One failing source never stops
    the others; its error is reported instead.

    With ``home``, new 10-K/10-Q events also summarise what changed in the risk
    factors since the previous filing (setting ``filing_diffs``, on by default).
    With ``prices``, closes for theses tracked on the scorecard are refreshed
    (setting ``price_source``: yahoo by default, or none)."""
    fetcher = fetcher or UrllibFetcher()
    clock = clock or SystemClock()
    report = WatchReport()

    if edgar:
        contact = sec_contact(store)
        targets = [e for e in store.watch_targets() if e.kind.value == "company"]
        if contact is None and targets:
            report.sources.append(
                SourceReport(
                    source="edgar",
                    error="no SEC contact email; set it with `torus config set sec_contact EMAIL`",
                )
            )
        elif contact is not None:
            differ: Callable[[str, Filing, list[Filing]], str | None] | None = None
            if home is not None and (store.get_setting(FILING_DIFFS_SETTING) or "on") != "off":
                from torus.sources.edgar_docs import EdgarDocs, risk_change_summary

                docs = EdgarDocs(fetcher, contact, home / "sources" / "edgar", limiter=limiter)
                differ = functools.partial(risk_change_summary, docs)

            watcher = EdgarWatcher(
                store,
                fetcher,
                contact,
                clock,
                limiter=limiter,
                reference=reference,
                differ=differ,
            )
            for entity in targets:
                report.sources.append(_safely(watcher.check, entity, f"edgar:{entity.name}"))

    if prices:
        report.sources += _refresh_prices(store, fetcher, clock)

    if rss:
        feeds = store.feeds()
        if feeds:
            rss_watcher = RssWatcher(store, fetcher, clock, EntityDetector(store, reference))
            for feed in feeds:
                url = str(feed["url"])
                report.sources.append(
                    _safely(rss_watcher.check, url, f"rss:{feed['name'] or url}", feed["name"])
                )
    return report


def _refresh_prices(store: Backend, fetcher: Fetcher, clock: Clock) -> list[SourceReport]:
    from torus.core.scorecard import PRICE_SOURCE_SETTING, refresh_prices, tracked
    from torus.sources.prices import YahooSource

    if (store.get_setting(PRICE_SOURCE_SETTING) or "yahoo") == "none" or not tracked(store):
        return []
    result = refresh_prices(store, YahooSource(fetcher), clock.now().date())
    failed = {s: str(v) for s, v in result.items() if isinstance(v, str)}
    report = SourceReport(source="prices", checked=len(result))
    report.notes = [f"{s}: {v}" for s, v in failed.items()]
    if failed and len(failed) == len(result):
        report.error = "; ".join(report.notes)
    return [report]


def _safely(check: object, arg: object, label: str, *extra: object) -> SourceReport:
    try:
        result = check(arg, *extra)  # type: ignore[operator]
        assert isinstance(result, SourceReport)
        return result
    except FetchError as exc:
        return SourceReport(source=label, error=f"network: {exc}")
    except Exception as exc:  # a bad source must not stop the rest
        return SourceReport(source=label, error=f"{type(exc).__name__}: {exc}")
