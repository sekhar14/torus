"""SEC EDGAR watcher: new filings by watched companies become events.

Reads ``https://data.sec.gov/submissions/CIK##########.json`` (the filer's recent
filings) with the SEC-required User-Agent, at most ~8 requests per second.

Incremental: per company it remembers the latest filing date seen and the
accession numbers filed on that date. The first check of a company ingests only
the last ``backfill_days`` of filings rather than its whole history. Events dedupe
on the filing URL, so a repeated check can never create duplicates.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta

from torus.core.clock import Clock
from torus.core.impact import ingest_event
from torus.core.models import Entity, EventKind
from torus.core.reference import ReferenceData, format_cik
from torus.core.store.base import Backend
from torus.core.text import display_name
from torus.watchers.http import Fetcher, RateLimiter, user_agent

SUBMISSIONS_URL = "https://data.sec.gov/submissions/CIK{cik}.json"
ARCHIVE_URL = "https://www.sec.gov/Archives/edgar/data/{cik}/{accession}/{document}"
MIN_INTERVAL = 0.125  # seconds between requests: 8/s, under the SEC's 10/s limit
DEFAULT_BACKFILL_DAYS = 7
DEFAULT_FORMS = frozenset(
    {"8-K", "8-K/A", "10-Q", "10-Q/A", "10-K", "10-K/A", "20-F", "20-F/A", "40-F", "6-K"}
)

# 8-K item numbers that say what the filing is about.
ITEM_TITLES = {
    "1.01": "material agreement",
    "1.02": "agreement terminated",
    "1.03": "bankruptcy or receivership",
    "1.05": "cybersecurity incident",
    "2.01": "acquisition or disposition completed",
    "2.02": "results of operations",
    "2.03": "new financial obligation",
    "2.05": "exit or restructuring costs",
    "2.06": "material impairment",
    "3.01": "delisting notice",
    "4.01": "auditor change",
    "4.02": "prior financials no longer reliable",
    "5.01": "change in control",
    "5.02": "executive or director change",
    "5.07": "shareholder vote results",
    "7.01": "Regulation FD disclosure",
    "8.01": "other events",
    "9.01": "financial statements and exhibits",
}
FORM_TITLES = {
    "10-Q": "quarterly report",
    "10-K": "annual report",
    "20-F": "annual report (foreign issuer)",
    "40-F": "annual report (Canadian issuer)",
    "6-K": "foreign issuer report",
}


@dataclass(frozen=True)
class Filing:
    cik: str  # 10-digit
    accession: str  # 0001045810-26-000123
    form: str
    filed: date
    accepted: datetime | None
    primary_document: str
    description: str
    items: tuple[str, ...]

    @property
    def url(self) -> str:
        return ARCHIVE_URL.format(
            cik=int(self.cik),
            accession=self.accession.replace("-", ""),
            document=self.primary_document,
        )

    @property
    def occurred_at(self) -> datetime:
        return self.accepted or datetime.combine(self.filed, datetime.min.time(), UTC)


@dataclass
class SourceReport:
    source: str
    checked: int = 0  # filings or feed items looked at
    new_events: int = 0
    alerts: int = 0
    skipped: int = 0  # filtered out: old, unmatched, or wrong form
    error: str | None = None
    notes: list[str] = field(default_factory=list)


def parse_submissions(payload: object) -> tuple[str, list[Filing]]:
    """(company name, recent filings) from a submissions JSON document."""
    if not isinstance(payload, dict) or "filings" not in payload:
        raise ValueError("not an EDGAR submissions document")
    cik = format_cik(str(payload.get("cik", "")))
    recent = payload["filings"].get("recent") or {}
    columns = (
        "accessionNumber",
        "filingDate",
        "form",
        "primaryDocument",
        "primaryDocDescription",
        "items",
        "acceptanceDateTime",
    )
    rows = zip(*(recent.get(c) or [] for c in columns), strict=False)
    filings = []
    for accession, filed, form, document, description, items, accepted in rows:
        try:
            filed_on = date.fromisoformat(str(filed))
        except ValueError:
            continue
        accepted_at = None
        if accepted:
            try:
                accepted_at = datetime.fromisoformat(str(accepted).replace("Z", "+00:00"))
            except ValueError:
                accepted_at = None
        filings.append(
            Filing(
                cik=cik,
                accession=str(accession),
                form=str(form),
                filed=filed_on,
                accepted=accepted_at,
                primary_document=str(document or ""),
                description=str(description or ""),
                items=tuple(i.strip() for i in str(items or "").split(",") if i.strip()),
            )
        )
    tickers = [str(t) for t in payload.get("tickers") or []]
    return display_name(str(payload.get("name") or cik), tickers), filings


def describe(filing: Filing, company: str) -> tuple[EventKind, str]:
    """Event kind and a readable title, e.g. "Nvidia Corp 8-K: results of operations"."""
    if filing.items:
        what = "; ".join(ITEM_TITLES.get(i, f"item {i}") for i in filing.items)
    else:
        base = filing.form.removesuffix("/A")
        what = FORM_TITLES.get(base) or filing.description or "filing"
    if filing.form.endswith("/A"):
        what += " (amendment)"
    earnings = "2.02" in filing.items or filing.form.removesuffix("/A") in ("10-Q", "10-K")
    kind = EventKind.EARNINGS if earnings else EventKind.FILING
    return kind, f"{company} {filing.form}: {what}"


def entity_cik(entity: Entity, reference: ReferenceData | None) -> str | None:
    """The entity's CIK from its canonical key, attributes, or tickers (reference)."""
    if entity.canonical_key and entity.canonical_key.startswith("cik:"):
        return format_cik(entity.canonical_key)
    if isinstance(entity.attrs.get("cik"), str):
        return format_cik(entity.attrs["cik"])
    if reference is not None:
        for alias in entity.aliases:
            if alias.alias_type.value == "ticker":
                company = reference.by_ticker(alias.alias)
                if company is not None:
                    return company.cik
    return None


class EdgarWatcher:
    def __init__(
        self,
        store: Backend,
        fetcher: Fetcher,
        contact: str,
        clock: Clock,
        limiter: RateLimiter | None = None,
        reference: ReferenceData | None = None,
        forms: Sequence[str] = tuple(DEFAULT_FORMS),
        backfill_days: int = DEFAULT_BACKFILL_DAYS,
        differ: Callable[[str, Filing, list[Filing]], str | None] | None = None,
    ) -> None:
        if "@" not in contact:
            raise ValueError("the SEC requires a contact email in the User-Agent")
        self.store = store
        self.fetcher = fetcher
        self.headers = {"User-Agent": user_agent(contact), "Accept": "application/json"}
        self.clock = clock
        self.limiter = limiter or RateLimiter(MIN_INTERVAL)
        self.reference = reference
        self.forms = frozenset(forms)
        self.backfill_days = backfill_days
        # Summarises what changed in a 10-K/10-Q's risk factors; None to skip.
        self.differ = differ

    def check(self, entity: Entity) -> SourceReport:
        cik = entity_cik(entity, self.reference)
        report = SourceReport(source=f"edgar:{entity.name}")
        if cik is None:
            report.error = "no CIK known (add SEC reference data or a cik: key)"
            return report
        self.limiter.wait()
        response = self.fetcher.get(SUBMISSIONS_URL.format(cik=cik), self.headers)
        if not response.ok:
            report.error = f"HTTP {response.status} for CIK {cik}"
            return report
        try:
            company, filings = parse_submissions(json.loads(response.body))
        except ValueError as exc:
            report.error = f"unreadable submissions for CIK {cik}: {exc}"
            return report

        key = f"edgar:{cik}"
        state = self.store.get_state(key)
        if state is None:
            cutoff = self.clock.now().date() - timedelta(days=self.backfill_days)
            new = [f for f in filings if f.filed >= cutoff]
        else:
            last = date.fromisoformat(state["last_filed"])
            seen = set(state.get("seen", []))
            new = [
                f
                for f in filings
                if f.filed > last or (f.filed == last and f.accession not in seen)
            ]
        report.checked = len(filings)

        # Risk-factor changes are read before the transaction: they need downloads.
        changes: dict[str, str] = {}
        if self.differ is not None:
            for filing in new:
                if filing.form in self.forms:
                    try:
                        summary = self.differ(company, filing, filings)
                    except Exception as exc:  # a diff is a bonus; never lose the event
                        report.notes.append(f"risk factor diff failed: {exc}")
                        summary = None
                    if summary:
                        changes[filing.accession] = summary

        # One transaction per company: all of its new filings land, or none do.
        with self.store.transaction():
            for filing in sorted(new, key=lambda f: (f.occurred_at, f.accession)):
                if filing.form not in self.forms:
                    report.skipped += 1
                    continue
                kind, title = describe(filing, company)
                result = ingest_event(
                    self.store,
                    kind,
                    title,
                    filing.occurred_at,
                    [entity.id],
                    source="edgar",
                    uri=filing.url,
                    summary=" ".join(
                        p for p in (filing.description, changes.get(filing.accession)) if p
                    )
                    or None,
                    payload={
                        "cik": cik,
                        "accession": filing.accession,
                        "form": filing.form,
                        "items": list(filing.items),
                    },
                )
                report.new_events += int(result.created)
                report.alerts += len(result.alerts)
            if filings:
                newest = max(f.filed for f in filings)
                previous_seen = (
                    set(state.get("seen", []))
                    if state and state["last_filed"] == newest.isoformat()
                    else set()
                )
                self.store.set_state(
                    key,
                    {
                        "last_filed": newest.isoformat(),
                        "seen": sorted(
                            previous_seen | {f.accession for f in filings if f.filed == newest}
                        ),
                    },
                )
        return report
