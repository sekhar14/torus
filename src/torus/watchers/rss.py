"""RSS / Atom watcher: feed items that mention graph entities become events.

Parsing uses the standard library. Documents that declare a DTD are refused
outright (no entity-expansion tricks). Each item's title and summary go through
the entity detector, and only entities already in the graph count, since an
event can only reach a thesis through them. Items mentioning none are dropped.

The first read of a feed takes items from the last ``backfill_days``. Later reads
take items published since the previous run, with a day of slack for feeds whose
dates lag. Events dedupe on the item link.
"""

from __future__ import annotations

import html
import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from email.utils import parsedate_to_datetime
from urllib.parse import urlparse

from torus.core.clock import Clock, to_ts
from torus.core.detect import EntityDetector
from torus.core.impact import ingest_event
from torus.core.models import EventKind
from torus.core.store.base import Backend
from torus.watchers.edgar import SourceReport
from torus.watchers.http import Fetcher, user_agent

DEFAULT_BACKFILL_DAYS = 7
SLACK = timedelta(days=1)
SUMMARY_CHARS = 1_000
ATOM = "{http://www.w3.org/2005/Atom}"
RSS1 = "{http://purl.org/rss/1.0/}"
_TAG = re.compile(r"<[^>]+>")
_SPACE = re.compile(r"\s+")


@dataclass(frozen=True)
class FeedItem:
    title: str
    link: str | None
    published: datetime | None
    summary: str


def _clean(text: str | None) -> str:
    """HTML fragment -> plain text."""
    return _SPACE.sub(" ", html.unescape(_TAG.sub(" ", text or ""))).strip()


def _date(value: str | None) -> datetime | None:
    if not value:
        return None
    value = value.strip()
    try:
        parsed = parsedate_to_datetime(value)  # RSS: RFC 822
    except (TypeError, ValueError):
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))  # Atom: ISO 8601
        except ValueError:
            return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def _text(node: ET.Element, *paths: str) -> str | None:
    for path in paths:
        found = node.find(path)
        if found is not None and (found.text or "").strip():
            return found.text
    return None


def parse_feed(data: bytes) -> list[FeedItem]:
    """Items of an RSS 2.0, RSS 1.0 (RDF) or Atom document."""
    head = data[:2048].lower()
    if b"<!doctype" in head or b"<!entity" in data.lower():
        raise ValueError("feeds declaring a DTD or entities are not accepted")
    try:
        root = ET.fromstring(data)
    except ET.ParseError as exc:
        raise ValueError(f"not a valid XML feed: {exc}") from exc

    items: list[FeedItem] = []
    if root.tag == f"{ATOM}feed":
        for entry in root.iter(f"{ATOM}entry"):
            link = None
            for candidate in entry.findall(f"{ATOM}link"):
                if candidate.get("rel", "alternate") == "alternate" and candidate.get("href"):
                    link = candidate.get("href")
                    break
            items.append(
                FeedItem(
                    title=_clean(_text(entry, f"{ATOM}title")),
                    link=link,
                    published=_date(_text(entry, f"{ATOM}published", f"{ATOM}updated")),
                    summary=_clean(_text(entry, f"{ATOM}summary", f"{ATOM}content"))[
                        :SUMMARY_CHARS
                    ],
                )
            )
        return items

    for item in [*root.iter("item"), *root.iter(f"{RSS1}item")]:
        items.append(
            FeedItem(
                title=_clean(_text(item, "title", f"{RSS1}title")),
                link=(_text(item, "link", f"{RSS1}link") or "").strip() or None,
                published=_date(_text(item, "pubDate", "{http://purl.org/dc/elements/1.1/}date")),
                summary=_clean(_text(item, "description", f"{RSS1}description"))[:SUMMARY_CHARS],
            )
        )
    return items


class RssWatcher:
    def __init__(
        self,
        store: Backend,
        fetcher: Fetcher,
        clock: Clock,
        detector: EntityDetector | None = None,
        backfill_days: int = DEFAULT_BACKFILL_DAYS,
    ) -> None:
        self.store = store
        self.fetcher = fetcher
        self.clock = clock
        self.detector = detector or EntityDetector(store)
        self.backfill_days = backfill_days
        self.headers = {
            "User-Agent": user_agent(),
            "Accept": "application/rss+xml, application/atom+xml, application/xml, text/xml",
        }

    def check(self, url: str, name: str | None = None) -> SourceReport:
        label = name or urlparse(url).netloc or url
        report = SourceReport(source=f"rss:{label}")
        response = self.fetcher.get(url, self.headers)
        if not response.ok:
            report.error = f"HTTP {response.status} for {url}"
            return report
        try:
            items = parse_feed(response.body)
        except ValueError as exc:
            report.error = str(exc)
            return report

        key = f"rss:{url}"
        state = self.store.get_state(key)
        now = self.clock.now()
        if state is None:
            cutoff = now - timedelta(days=self.backfill_days)
        else:
            cutoff = datetime.fromisoformat(state["last_run"]) - SLACK
        report.checked = len(items)

        with self.store.transaction():
            for item in items:
                if not item.title or (item.published is not None and item.published < cutoff):
                    report.skipped += 1
                    continue
                entity_ids = self.detector.detect(f"{item.title}. {item.summary}").entity_ids
                if not entity_ids:
                    report.skipped += 1
                    continue
                result = ingest_event(
                    self.store,
                    EventKind.NEWS,
                    item.title,
                    item.published or now,
                    entity_ids,
                    source=f"rss:{label}",
                    uri=item.link,
                    summary=item.summary or None,
                    payload={"feed": url},
                )
                report.new_events += int(result.created)
                report.alerts += len(result.alerts)
            self.store.set_state(key, {"last_run": to_ts(now).replace("Z", "+00:00")})
        return report
