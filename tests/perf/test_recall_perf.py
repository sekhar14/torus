"""Recall latency on a large synthetic graph.

Run with: pytest -m perf tests/perf
Size with TORUS_PERF_CLAIMS (default 100_000).

Claim text draws from a ~3k-word vocabulary with a Zipf-like skew, so the
query terms appear in a few percent of claims, as in real research notes. (With
a tiny vocabulary every claim matches every query and BM25 ranking alone costs
~40 ms at 100k claims.)
"""

from __future__ import annotations

import json
import os
import random
import shutil
import statistics
import subprocess
import sys
import time
from pathlib import Path

import pytest

from torus.core.clock import ManualClock
from torus.core.detect import EntityDetector
from torus.core.ids import IdGenerator
from torus.core.models import ClaimType, EntityKind, Stance
from torus.core.recall import recall, render_context
from torus.core.store import SQLiteBackend

pytestmark = pytest.mark.perf

N_CLAIMS = int(os.environ.get("TORUS_PERF_CLAIMS", "100000"))
N_ENTITIES = 2000
N_EVIDENCE = N_CLAIMS // 5
N_THESES = 2000
TOPIC_WORDS = [
    "supply",
    "demand",
    "margin",
    "capex",
    "guidance",
    "HBM",
    "CoWoS",
    "pricing",
    "share",
    "backlog",
]
FILLER = [f"w{i}" for i in range(3000)]
WEIGHTS = [1 / (rank + 1) for rank in range(len(FILLER))]


def _text(rng: random.Random, i: int) -> str:
    words = rng.choices(FILLER, weights=WEIGHTS, k=10)
    if rng.random() < 0.05:
        words[rng.randrange(10)] = rng.choice(TOPIC_WORDS)
    return " ".join(words) + f" #{i}"


@pytest.fixture(scope="module")
def big_store(tmp_path_factory: pytest.TempPathFactory) -> SQLiteBackend:
    # Laid out as a TORUS_HOME so hook processes can open the same database.
    path: Path = tmp_path_factory.mktemp("perf") / "torus.db"
    clock = ManualClock()
    store = SQLiteBackend.open(path, clock=clock, ids=IdGenerator(clock))
    rng = random.Random(7)
    with store.transaction():
        entities = [
            store.upsert_entity(EntityKind.COMPANY, f"Company {i}", canonical_key=f"t:{i}").id
            for i in range(N_ENTITIES)
        ]
        evidence = [store.record_evidence(f"https://example.com/{i}").id for i in range(N_EVIDENCE)]
        claims = []
        for i in range(N_CLAIMS):
            clock.advance(seconds=30)
            text = _text(rng, i)
            claim = store.record_claim(
                text,
                ClaimType.FACT,
                rng.random(),
                entities=rng.sample(entities, 2),
                evidence=[rng.choice(evidence)],
            )
            claims.append(claim.id)
        for i in range(N_THESES):
            store.record_thesis(
                f"Thesis {i}",
                "statement",
                Stance.BULLISH,
                0.6,
                subject_entity_id=rng.choice(entities),
                assumptions=rng.sample(claims, 3),
            )
    return store


def _p95(samples: list[float]) -> float:
    return statistics.quantiles(samples, n=20)[-1]


def _recall_p95(store: SQLiteBackend, query: str | None) -> float:
    clock = store._clock
    ids = list(store.entity_index())
    rng = random.Random(1)
    samples = []
    for _ in range(50):
        start = time.perf_counter()
        result = recall(store, clock, entity_ids=[rng.choice(ids)], query=query)
        render_context(result, now=clock.now())
        samples.append((time.perf_counter() - start) * 1000)
    return _p95(samples)


def test_recall_p95_under_100ms(big_store: SQLiteBackend) -> None:
    p95 = _recall_p95(big_store, "What is the HBM supply outlook?")
    print(f"recall with query p95: {p95:.1f} ms")
    assert p95 < 100, f"p95 {p95:.1f} ms"


def test_entity_only_recall_p95_under_30ms(big_store: SQLiteBackend) -> None:
    """The prompt hook's path: entities detected, no free-text query."""
    p95 = _recall_p95(big_store, None)
    print(f"entity-only recall p95: {p95:.1f} ms")
    assert p95 < 30, f"p95 {p95:.1f} ms"


def test_detector_build_and_detect_fast(big_store: SQLiteBackend) -> None:
    start = time.perf_counter()
    detector = EntityDetector(big_store)
    build_ms = (time.perf_counter() - start) * 1000
    start = time.perf_counter()
    for _ in range(100):
        detector.detect("How is Company 42 doing versus Company 1999 on HBM supply?")
    detect_ms = (time.perf_counter() - start) * 10
    print(f"detector build {build_ms:.1f} ms, detect {detect_ms:.2f} ms")
    assert build_ms < 200, f"build {build_ms:.1f} ms"
    assert detect_ms < 5, f"detect {detect_ms:.2f} ms"


def test_prompt_hook_process_p95_under_300ms(big_store: SQLiteBackend) -> None:
    """End to end, as Claude Code runs it: a fresh `torus hook` process per prompt."""
    home = Path(big_store._conn.execute("PRAGMA database_list").fetchone()[2]).parent
    torus = shutil.which("torus", path=str(Path(sys.executable).parent))
    command = [torus] if torus else [sys.executable, "-m", "torus"]
    env = {**os.environ, "TORUS_HOME": str(home)}
    payload = json.dumps({"session_id": "perf", "prompt": "How is Company 0 doing on HBM supply?"})
    samples = []
    for _ in range(20):
        start = time.perf_counter()
        result = subprocess.run(
            [*command, "hook", "user-prompt-submit"],
            input=payload,
            capture_output=True,
            text=True,
            env=env,
            timeout=30,
            check=True,
        )
        samples.append((time.perf_counter() - start) * 1000)
        assert "additionalContext" in result.stdout
    p95 = _p95(samples)
    print(f"prompt hook process p95: {p95:.0f} ms (median {statistics.median(samples):.0f} ms)")
    assert p95 < 300, f"p95 {p95:.0f} ms"
