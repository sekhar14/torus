"""Seed a Torus home with a small, clearly labelled demo graph.

    python scripts/seed_demo.py --home <TORUS_HOME> [--reset]

Creates NVIDIA, TSMC, Micron and AMD, a supply relation, a few claims with
evidence, a thesis that assumes TSMC packaging capacity, two more theses, a
rejected hypothesis, and a TSMC filing that raises an alert. The research is
spread over the last four months (and one thesis breaks along the way), so the
explorer's time slider has something to replay. Every text is prefixed "[demo]"
so it can't be mistaken for real research.

For the README screenshots, seed the `semis` starter pack too (with SEC
reference data, so companies share CIK keys) and run `torus explore`.
"""

from __future__ import annotations

import argparse
from datetime import UTC, datetime, timedelta
from pathlib import Path

from torus.config import Paths
from torus.core.clock import ManualClock
from torus.core.impact import ingest_event
from torus.core.models import (
    Alias,
    AliasType,
    ClaimEntity,
    ClaimEntityRole,
    ClaimType,
    EntityKind,
    EventKind,
    EvidenceKind,
    RelationType,
    Stance,
    ThesisStatus,
)
from torus.core.store import SQLiteBackend

HISTORY_DAYS = 120


def _company(store: SQLiteBackend, name: str, cik: str, ticker: str, *aliases: str) -> str:
    entity = store.upsert_entity(
        EntityKind.COMPANY,
        name,
        canonical_key=f"cik:{cik}",
        aliases=[Alias(alias=ticker, alias_type=AliasType.TICKER)]
        + [Alias(alias=a, alias_type=AliasType.NAME) for a in aliases],
    )
    return entity.id


def seed(home: Path, reset: bool) -> None:
    p = Paths(home).ensure()
    if reset:
        for suffix in ("", "-wal", "-shm"):
            Path(str(p.db) + suffix).unlink(missing_ok=True)
    now = datetime.now(UTC).replace(microsecond=0)
    clock = ManualClock(now - timedelta(days=HISTORY_DAYS))
    with SQLiteBackend.open(p.db, clock=clock) as store:
        if store.counts()["claims"] and not reset:
            print(f"{p.db} already has claims; pass --reset to start over.")
            return
        session = store.start_session("demo-seed")
        nvda = _company(store, "NVIDIA CORP", "0001045810", "NVDA", "Nvidia")
        tsm = _company(
            store, "TAIWAN SEMICONDUCTOR MANUFACTURING CO LTD", "0001046179", "TSM", "TSMC"
        )
        mu = _company(store, "MICRON TECHNOLOGY INC", "0000723125", "MU", "Micron")
        amd = _company(store, "ADVANCED MICRO DEVICES INC", "0000002488", "AMD")

        # Month 1: the NVIDIA supply thesis.
        call = store.record_evidence(
            "https://example.com/demo/nvda-earnings-call",
            kind=EvidenceKind.TRANSCRIPT,
            title="[demo] NVIDIA earnings call (illustrative)",
            quote="[demo] supply of advanced packaging remains the gating factor",
            published_at=clock.now() - timedelta(days=2),
        )
        tsmc_report = store.record_evidence(
            "https://example.com/demo/tsmc-capacity",
            kind=EvidenceKind.WEB,
            title="[demo] TSMC CoWoS capacity report (illustrative)",
            quote="[demo] CoWoS capacity planned to roughly double year over year",
        )
        constraint = store.record_claim(
            "[demo] TSMC CoWoS packaging capacity constrains NVIDIA data-center GPU shipments",
            ClaimType.FACT,
            0.8,
            entities=[nvda, ClaimEntity(entity_id=tsm, role=ClaimEntityRole.OBJECT)],
            evidence=[call.id],
            session_id=session.id,
        )
        expansion = store.record_claim(
            "[demo] TSMC plans to roughly double CoWoS capacity over the next year",
            ClaimType.FORECAST,
            0.6,
            entities=[tsm],
            evidence=[tsmc_report.id],
            session_id=session.id,
        )
        store.record_relation(tsm, RelationType.SUPPLIES, nvda, 0.95, claim_id=constraint.id)
        store.record_thesis(
            title="[demo] NVIDIA data-center growth is supply-bound through 2027",
            statement="[demo] Demand exceeds supply; shipments track TSMC packaging capacity.",
            stance=Stance.BULLISH,
            confidence=0.65,
            horizon="12m",
            subject_entity_id=nvda,
            assumptions=[constraint.id],
            supporting=[expansion.id],
            triggers=[("[demo] TSMC cuts or delays CoWoS capacity plans", tsm)],
            session_id=session.id,
        )
        store.record_rejected_hypothesis(
            "[demo] Rising NVIDIA inventory signals a demand slowdown",
            reason="[demo] The build is packaging work-in-progress, not finished goods",
            entity_ids=[nvda],
            session_id=session.id,
        )

        # Month 2: a Micron thesis on HBM.
        clock.advance(days=30)
        hbm = store.record_evidence(
            "https://example.com/demo/hbm-supply",
            kind=EvidenceKind.WEB,
            title="[demo] HBM supply note (illustrative)",
            quote="[demo] HBM supply stays tight through next year",
        )
        tight = store.record_claim(
            "[demo] HBM memory supply stays tight through 2026",
            ClaimType.FORECAST,
            0.6,
            entities=[mu, ClaimEntity(entity_id=nvda, role=ClaimEntityRole.MENTIONED)],
            evidence=[hbm.id],
            session_id=session.id,
        )
        store.record_relation(mu, RelationType.SUPPLIES, nvda, 0.8)
        store.record_thesis(
            title="[demo] Micron gains HBM share",
            statement="[demo] Tight HBM supply favours Micron's capacity additions.",
            stance=Stance.BULLISH,
            confidence=0.55,
            horizon="FY2026",
            subject_entity_id=mu,
            assumptions=[tight.id],
            session_id=session.id,
        )

        # Month 3: an AMD thesis, which later breaks.
        clock.advance(days=25)
        amd_thesis = store.record_thesis(
            title="[demo] AMD MI400 wins hyperscaler share",
            statement="[demo] A second source for AI accelerators wins large orders.",
            stance=Stance.BULLISH,
            confidence=0.5,
            horizon="12m",
            subject_entity_id=amd,
            session_id=session.id,
        )
        store.record_relation(amd, RelationType.COMPETES_WITH, nvda, 0.8)

        # Month 4: a TSMC filing reaches the NVIDIA thesis through its trigger,
        # its assumption claim and the supply link. Left open for /torus:review.
        clock.advance(days=30)
        ingest_event(
            store,
            EventKind.FILING,
            "[demo] TSMC 8-K: CoWoS capacity expansion delayed",
            clock.now(),
            [tsm],
            source="edgar",
            uri="https://example.com/demo/tsmc-8k",
            summary="[demo] TSMC pushes back its CoWoS packaging expansion by two quarters.",
            payload={"form": "8-K", "items": ["8.01"]},
        )
        clock.advance(days=15)
        store.update_thesis(
            amd_thesis.id,
            "[demo] Orders went to custom accelerators instead",
            status=ThesisStatus.BROKEN,
            confidence=0.2,
        )
        print(f"Seeded demo graph in {p.db}: {store.counts()}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--home", type=Path, required=True, help="TORUS_HOME to seed")
    parser.add_argument("--reset", action="store_true", help="delete the database first")
    args = parser.parse_args()
    seed(args.home, args.reset)


if __name__ == "__main__":
    main()
