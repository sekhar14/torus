"""Seed a Torus home with a small, clearly labelled demo graph.

    python scripts/seed_demo.py --home <TORUS_HOME> [--reset] [--pack semis]

The research itself lives in ``torus.core.demo`` (shared with ``torus demo``).
For the README screenshots, pass ``--pack semis`` with SEC reference data in the
home (so companies share CIK keys) and run ``torus explore``.
"""

from __future__ import annotations

import argparse
from pathlib import Path

from torus.config import Paths
from torus.core.demo import seed_demo
from torus.core.reference import SEC_TICKERS_FILENAME, ReferenceData


def seed(home: Path, reset: bool, pack: str | None) -> None:
    p = Paths(home).ensure()
    if reset:
        for suffix in ("", "-wal", "-shm"):
            Path(str(p.db) + suffix).unlink(missing_ok=True)
    reference = ReferenceData.load(p.reference / SEC_TICKERS_FILENAME)
    try:
        counts = seed_demo(p.db, pack=pack, reference=reference)
    except ValueError:
        print(f"{p.db} already has claims; pass --reset to start over.")
        return
    print(f"Seeded demo graph in {p.db}: {counts}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--home", type=Path, required=True, help="TORUS_HOME to seed")
    parser.add_argument("--reset", action="store_true", help="delete the database first")
    parser.add_argument("--pack", help="also add a starter pack (e.g. semis)")
    args = parser.parse_args()
    seed(args.home, args.reset, args.pack)


if __name__ == "__main__":
    main()
