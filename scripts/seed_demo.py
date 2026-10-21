"""Seed a Torus home with a small, clearly labelled demo graph.

    python scripts/seed_demo.py --home <TORUS_HOME> [--reset]

Creates NVIDIA and TSMC, a supply relation, a few claims with evidence, a thesis
that assumes TSMC packaging capacity, and a rejected hypothesis. Every text is
prefixed "[demo]" so it can't be mistaken for real research.
"""

from __future__ import annotations

import argparse
from datetime import UTC, datetime
from pathlib import Path

from torus.config import Paths
from torus.core.models import (
    Alias,
    AliasType,
    ClaimEntity,
    ClaimEntityRole,
    ClaimType,
    EntityKind,
    EvidenceKind,
    RelationType,
    Stance,
)
from torus.core.store import SQLiteBackend


def seed(home: Path, reset: bool) -> None:
    p = Paths(home).ensure()
    if reset:
        for suffix in ("", "-wal", "-shm"):
            Path(str(p.db) + suffix).unlink(missing_ok=True)
    with SQLiteBackend.open(p.db) as store:
        if store.counts()["claims"] and not reset:
            print(f"{p.db} already has claims; pass --reset to start over.")
            return
        session = store.start_session("demo-seed")
        nvda = store.upsert_entity(
            EntityKind.COMPANY,
            "NVIDIA CORP",
            canonical_key="cik:0001045810",
            aliases=[
                Alias(alias="NVDA", alias_type=AliasType.TICKER),
                Alias(alias="Nvidia", alias_type=AliasType.NAME),
            ],
        )
        tsm = store.upsert_entity(
            EntityKind.COMPANY,
            "TAIWAN SEMICONDUCTOR MANUFACTURING CO LTD",
            canonical_key="cik:0001046179",
            aliases=[
                Alias(alias="TSM", alias_type=AliasType.TICKER),
                Alias(alias="TSMC", alias_type=AliasType.ABBREVIATION),
            ],
        )
        call = store.record_evidence(
            "https://example.com/demo/nvda-earnings-call",
            kind=EvidenceKind.TRANSCRIPT,
            title="[demo] NVIDIA earnings call (illustrative)",
            quote="[demo] supply of advanced packaging remains the gating factor",
            published_at=datetime(2026, 8, 27, tzinfo=UTC),
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
            entities=[nvda.id, ClaimEntity(entity_id=tsm.id, role=ClaimEntityRole.OBJECT)],
            evidence=[call.id],
            session_id=session.id,
        )
        expansion = store.record_claim(
            "[demo] TSMC plans to roughly double CoWoS capacity over the next year",
            ClaimType.FORECAST,
            0.6,
            entities=[tsm.id],
            evidence=[tsmc_report.id],
            session_id=session.id,
        )
        store.record_relation(tsm.id, RelationType.SUPPLIES, nvda.id, 0.95, claim_id=constraint.id)
        store.record_thesis(
            title="[demo] NVIDIA data-center growth is supply-bound through 2027",
            statement="[demo] Demand exceeds supply; shipments track TSMC packaging capacity.",
            stance=Stance.BULLISH,
            confidence=0.65,
            horizon="12m",
            subject_entity_id=nvda.id,
            assumptions=[constraint.id],
            supporting=[expansion.id],
            triggers=[("[demo] TSMC cuts or delays CoWoS capacity plans", tsm.id)],
            session_id=session.id,
        )
        store.record_rejected_hypothesis(
            "[demo] Rising NVIDIA inventory signals a demand slowdown",
            reason="[demo] The build is packaging work-in-progress, not finished goods",
            entity_ids=[nvda.id],
            session_id=session.id,
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
