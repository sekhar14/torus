"""Precision/recall of entity detection on a hand-labelled prompt set.

Labels are what a human would tag, including companies the detector skips on
purpose (single-word names that are not in the graph yet, like "Visa" or
"Netflix"). Two scores are checked:

* graph scope - only entities already in the graph. This is what prompt-time
  recall uses, so it carries the strict target.
* all labels  - everything, including reference-only companies. A lower recall
  floor that guards against regressions.
"""

from __future__ import annotations

import json
from dataclasses import dataclass

import pytest

from conftest import FIXTURES
from torus.core.detect import EntityDetector
from torus.core.models import Entity
from torus.core.reference import ReferenceData
from torus.core.store import SQLiteBackend


@dataclass
class Score:
    tp: int = 0
    fp: int = 0
    fn: int = 0

    @property
    def precision(self) -> float:
        return self.tp / (self.tp + self.fp) if self.tp + self.fp else 1.0

    @property
    def recall(self) -> float:
        return self.tp / (self.tp + self.fn) if self.tp + self.fn else 1.0

    def add(self, expected: set[str], got: set[str]) -> None:
        self.tp += len(expected & got)
        self.fp += len(got - expected)
        self.fn += len(expected - got)


def _cases() -> list[dict[str, object]]:
    lines = (FIXTURES / "prompts_labelled.jsonl").read_text("utf-8").splitlines()
    return [json.loads(line) for line in lines if line.strip()]


@pytest.fixture
def scores(
    store: SQLiteBackend, reference: ReferenceData, seeded: dict[str, Entity]
) -> tuple[Score, Score, list[str]]:
    detector = EntityDetector(store, reference)
    graph_keys = {e.canonical_key for e in seeded.values()}
    everything, graph_only, report = Score(), Score(), []
    for case in _cases():
        expected = set()
        for ticker in case["expected"]:  # type: ignore[attr-defined]
            company = reference.by_ticker(ticker)
            assert company is not None, ticker
            expected.add(company.canonical_key)
        got = {d.canonical_key for d in detector.detect(str(case["text"])).detections}
        got.discard(None)
        everything.add(expected, got)  # type: ignore[arg-type]
        graph_only.add(expected & graph_keys, got & graph_keys)  # type: ignore[operator]
        if got != expected:
            report.append(f"{case['text']!r}: missing={expected - got} extra={got - expected}")
    return everything, graph_only, report


def test_labelled_set_is_large_enough() -> None:
    assert len(_cases()) >= 100


def test_detect_precision_recall_graph_scope(scores: tuple[Score, Score, list[str]]) -> None:
    _, graph_only, report = scores
    detail = "\n".join(report)
    assert graph_only.precision >= 0.95, detail
    assert graph_only.recall >= 0.90, detail


def test_detect_precision_recall_all_labels(scores: tuple[Score, Score, list[str]]) -> None:
    everything, _, report = scores
    detail = "\n".join(report)
    assert everything.precision >= 0.95, detail
    assert everything.recall >= 0.80, detail
