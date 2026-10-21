from __future__ import annotations

import random
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

import pytest

from torus.core.clock import ManualClock
from torus.core.ids import IdGenerator
from torus.core.models import (
    Alias,
    AliasType,
    Claim,
    ClaimType,
    Entity,
    EntityKind,
    Evidence,
    Stance,
    Thesis,
)
from torus.core.reference import ReferenceData
from torus.core.resolve import Resolver
from torus.core.store import SQLiteBackend
from torus.core.store.sqlite import normalize_alias

T0 = datetime(2026, 1, 1, 9, 0, tzinfo=UTC)


@pytest.fixture
def torus_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / "torus-home"
    monkeypatch.setenv("TORUS_HOME", str(home))
    return home


@pytest.fixture
def clock() -> ManualClock:
    return ManualClock(T0)


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.fixture
def store(clock: ManualClock) -> Iterator[SQLiteBackend]:
    # Seeded randomness keeps ids (and anything rendered with them) reproducible.
    ids = IdGenerator(clock, randbits=random.Random(0).getrandbits)
    backend = SQLiteBackend.open(":memory:", clock=clock, ids=ids)
    yield backend
    backend.close()


@dataclass
class GraphFactory:
    """Small builders so tests read as scenarios, not boilerplate."""

    store: SQLiteBackend

    def company(self, ticker: str, name: str | None = None, cik: str | None = None) -> Entity:
        return self.store.upsert_entity(
            EntityKind.COMPANY,
            name or f"{ticker} Inc",
            canonical_key=f"cik:{cik}" if cik else f"ticker:{ticker}",
            aliases=[Alias(alias=ticker, alias_type=AliasType.TICKER)],
        )

    def evidence(self, uri: str = "https://example.com/source") -> Evidence:
        return self.store.record_evidence(uri)

    def fact(self, text: str, *entities: Entity, confidence: float = 0.8) -> Claim:
        ev = self.evidence(f"https://example.com/{normalize_alias(text).replace(' ', '-')}")
        return self.store.record_claim(
            text,
            ClaimType.FACT,
            confidence,
            entities=[e.id for e in entities],
            evidence=[ev.id],
        )

    def thesis(self, title: str, subject: Entity, *assumption_ids: str) -> Thesis:
        return self.store.record_thesis(
            title=title,
            statement=f"{title} (statement)",
            stance=Stance.BULLISH,
            confidence=0.7,
            horizon="12m",
            subject_entity_id=subject.id,
            assumptions=list(assumption_ids),
        )


@pytest.fixture
def graph(store: SQLiteBackend) -> GraphFactory:
    return GraphFactory(store)


FIXTURES = Path(__file__).parent / "fixtures"

# Companies a typical user has already researched, with the extra aliases an agent
# would have recorded. Used by detection tests and the labelled-prompt eval.
SEEDED_TICKERS = ("NVDA", "TSM", "AMD", "AAPL", "MSFT", "GOOGL", "INTC", "ASML", "META", "AVGO")
SEEDED_ALIASES = {
    "TSM": [("TSMC", AliasType.ABBREVIATION)],
    "GOOGL": [("Google", AliasType.FORMER_NAME)],
    "META": [("Facebook", AliasType.FORMER_NAME)],
    "AVGO": [("Broadcom", AliasType.NAME)],
}


@pytest.fixture(scope="session")
def reference() -> ReferenceData:
    return ReferenceData.load(FIXTURES / "sec_company_tickers.json")


@pytest.fixture
def resolver(store: SQLiteBackend, reference: ReferenceData) -> Resolver:
    return Resolver(store, reference)


@pytest.fixture
def seeded(store: SQLiteBackend, reference: ReferenceData, resolver: Resolver) -> dict[str, Entity]:
    """Graph with SEEDED_TICKERS added from reference data, keyed by ticker."""
    entities = {}
    for ticker in SEEDED_TICKERS:
        company = reference.by_ticker(ticker)
        assert company is not None, ticker
        entity = resolver.ensure_company(company)
        for alias, alias_type in SEEDED_ALIASES.get(ticker, []):
            entity = store.add_alias(entity.id, alias, alias_type)
        entities[ticker] = entity
    return entities
