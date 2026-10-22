"""Onboarding: from a list of tickers (or a portfolio CSV) to a graph with structure.

The deterministic part runs here: resolve each ticker against the SEC list, add
the companies, watch them, suggest starter packs that cover them, and queue one
``map_company`` job per company. The agent part runs later: a Haiku subagent
leases each job, reads the company's latest annual report through the
``filing_text`` tool, and records its suppliers, customers, competitors and
customer concentration, citing the filing.
"""

from __future__ import annotations

import csv
import io
import re
from collections.abc import Sequence
from dataclasses import dataclass, field

from torus.core.models import Alias, AliasType, EntityKind, QueueStatus
from torus.core.reference import ReferenceData
from torus.core.resolve import Resolution, ResolutionStatus, Resolver
from torus.core.starter import available_packs, load_pack
from torus.core.store.base import Backend

MAP_KIND = "map_company"
TICKER_SHAPE = re.compile(r"^[A-Z]{1,5}([.-][A-Z]{1,2})?$")
TICKER_COLUMNS = ("ticker", "symbol", "tickers", "symbols", "code")
# Forms that carry a business description, in order of preference.
ANNUAL_FORMS = ("10-K", "20-F", "40-F")


@dataclass
class Onboarded:
    ref: str
    entity_id: str
    name: str
    created: bool
    queued: bool


@dataclass
class OnboardReport:
    companies: list[Onboarded] = field(default_factory=list)
    unresolved: list[tuple[str, str]] = field(default_factory=list)  # (ref, why)
    packs: list[tuple[str, int]] = field(default_factory=list)  # (pack, companies covered)

    @property
    def queued(self) -> int:
        return sum(1 for c in self.companies if c.queued)


def read_tickers(text: str) -> list[str]:
    """Tickers from a CSV (a ticker/symbol column, else the first column) or from
    whitespace/comma separated text. Blank cells and duplicates are dropped."""
    lines = [line for line in text.splitlines() if line.strip()]
    if not lines:
        return []
    rows = list(csv.reader(io.StringIO("\n".join(lines))))
    header = [c.strip().casefold() for c in rows[0]]
    column = next((header.index(c) for c in TICKER_COLUMNS if c in header), None)
    if column is not None:
        values = [r[column] for r in rows[1:] if len(r) > column]
    elif len(rows) > 1 and all(len(r) > 1 and _is_number(r[1]) for r in rows):
        values = [r[0] for r in rows]  # no header: tickers then numbers (e.g. weights)
    else:
        values = [v for r in rows for cell in r for v in cell.split()]
    seen: dict[str, None] = {}
    for value in values:
        ticker = value.strip().lstrip("$").upper()
        if ticker and ticker not in seen and not ticker.startswith("#"):
            seen[ticker] = None
    return list(seen)


def _is_number(cell: str) -> bool:
    try:
        float(cell.strip().rstrip("%").replace(",", ""))
    except ValueError:
        return False
    return True


def suggest_packs(store: Backend, entity_ids: Sequence[str]) -> list[tuple[str, int]]:
    """Starter packs that cover some of these companies, most coverage first."""
    tickers = set()
    for entity_id in entity_ids:
        entity = store.get_entity(entity_id)
        tickers |= {a.alias.upper() for a in entity.aliases if a.alias_type.value == "ticker"}
    found = []
    for name in available_packs():
        pack = {e.get("ticker", "").upper() for e in load_pack(name)["entities"]}
        covered = len(tickers & pack)
        if covered:
            found.append((name, covered))
    found.sort(key=lambda p: (-p[1], p[0]))
    return found


def _ticker_entity(store: Backend, ref: str) -> Resolution | None:
    ticker = ref.strip().upper()
    if not TICKER_SHAPE.match(ticker):
        return None
    key = f"ticker:{ticker}"
    created = store.get_entity_by_key(key) is None
    entity = store.upsert_entity(
        EntityKind.COMPANY,
        ticker,
        canonical_key=key,
        aliases=[Alias(alias=ticker, alias_type=AliasType.TICKER)],
    )
    return Resolution(
        ResolutionStatus.RESOLVED, ref, "ticker" + ("+created" if created else ""), entity
    )


def onboard(
    store: Backend,
    refs: Sequence[str],
    reference: ReferenceData | None = None,
    watch: bool = True,
    map_companies: bool = True,
) -> OnboardReport:
    """Add and watch companies, and queue a mapping job for each. Safe to repeat:
    a company that already has a pending or finished job is not queued again."""
    report = OnboardReport()
    resolver = Resolver(store, reference)
    done = {
        str(item.payload.get("entity_id"))
        for item in store.queue_items(kind=MAP_KIND)
        if item.status is not QueueStatus.PARKED
    }
    with store.transaction():
        for ref in refs:
            result = resolver.resolve(ref, create=True)
            if result.entity is None and not (reference and len(reference)):
                # No SEC list to check against: take ticker-shaped refs at their word.
                result = _ticker_entity(store, ref) or result
            if result.status is not ResolutionStatus.RESOLVED or result.entity is None:
                why = (
                    "ambiguous"
                    if result.status is ResolutionStatus.AMBIGUOUS
                    else "not in the graph or the SEC list"
                )
                report.unresolved.append((ref, why))
                continue
            entity = result.entity
            if watch:
                store.watch_entity(entity.id, note="onboarded")
            queued = False
            if map_companies and entity.id not in done:
                store.enqueue(
                    MAP_KIND,
                    dedupe_key=f"map:{entity.id}",
                    payload={"entity_id": entity.id, "ref": ref, "forms": list(ANNUAL_FORMS)},
                )
                done.add(entity.id)
                queued = True
            report.companies.append(
                Onboarded(
                    ref=ref,
                    entity_id=entity.id,
                    name=entity.name,
                    created=result.method.endswith("+created"),
                    queued=queued,
                )
            )
    report.packs = suggest_packs(store, [c.entity_id for c in report.companies])
    return report
