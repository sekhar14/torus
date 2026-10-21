from __future__ import annotations

import pytest

from torus.core.detect import EntityDetector
from torus.core.models import AliasType, Entity, EntityKind
from torus.core.reference import ReferenceData
from torus.core.store import SQLiteBackend


@pytest.fixture
def detector(
    store: SQLiteBackend, reference: ReferenceData, seeded: dict[str, Entity]
) -> EntityDetector:
    return EntityDetector(store, reference)


def _tickers(detector: EntityDetector, reference: ReferenceData, text: str) -> list[str]:
    by_key = {c.canonical_key: c.tickers[0] for c in reference.companies.values()}
    return [by_key[d.canonical_key] for d in detector.detect(text).detections if d.canonical_key]


def test_detect_dollar_tickers(detector: EntityDetector, reference: ReferenceData) -> None:
    result = detector.detect("$NVDA vs $AMD")
    assert [d.method for d in result.detections] == ["cashtag", "cashtag"]
    assert _tickers(detector, reference, "$NVDA vs $AMD") == ["NVDA", "AMD"]
    assert result.detections[0].surface == "$NVDA"


def test_detect_ignores_stoplist(detector: EntityDetector) -> None:
    assert detector.detect("AI capex by the CEO in the USA").detections == ()


def test_cashtag_overrides_stoplist(detector: EntityDetector, reference: ReferenceData) -> None:
    assert _tickers(detector, reference, "bear case for $AI and $NOW") == ["AI", "NOW"]


def test_detect_bare_uppercase_only_if_known(
    detector: EntityDetector, reference: ReferenceData
) -> None:
    assert _tickers(detector, reference, "TSM supply") == ["TSM"]
    assert detector.detect("XYZQ supply").detections == ()


def test_bare_ticker_confidence_graph_vs_reference(detector: EntityDetector) -> None:
    graph_hit, ref_hit = detector.detect("NVDA and AMZN").detections
    assert graph_hit.entity_id is not None and graph_hit.confidence > ref_hit.confidence
    assert ref_hit.entity_id is None and ref_hit.canonical_key == "cik:0001018724"


def test_names_prefer_longest_span(detector: EntityDetector) -> None:
    [d] = detector.detect("Taiwan Semiconductor Manufacturing capex").detections
    assert d.surface == "Taiwan Semiconductor Manufacturing"
    assert d.method == "name"


def test_possessive_and_lowercase_abbreviation(
    detector: EntityDetector, seeded: dict[str, Entity]
) -> None:
    assert detector.detect("what about tsmc's pricing?").entity_ids == [seeded["TSM"].id]
    assert detector.detect("Nvidia's margins").entity_ids == [seeded["NVDA"].id]


def test_lowercase_common_words_not_matched(detector: EntityDetector) -> None:
    assert detector.detect("the apple harvest was weak").detections == ()
    assert detector.detect("The apple harvest was weak").detections == ()


def test_single_word_reference_names_below_threshold(detector: EntityDetector) -> None:
    assert detector.detect("Target price for the stock").detections == ()
    low = detector.detect("Target price for the stock", min_confidence=0.5).detections
    assert [d.canonical_key for d in low] == ["cik:0000027419"]


def test_shouting_disables_bare_tickers(detector: EntityDetector) -> None:
    assert detector.detect("WHAT IS THE OUTLOOK FOR RATES AND THE DOLLAR").detections == ()
    shouted = detector.detect("WHAT IS THE OUTLOOK FOR $NVDA THIS YEAR").detections
    assert [d.method for d in shouted] == ["cashtag"]


def test_one_detection_per_entity(detector: EntityDetector, seeded: dict[str, Entity]) -> None:
    result = detector.detect("NVDA, Nvidia and $NVDA again")
    assert result.entity_ids == [seeded["NVDA"].id]
    assert result.detections[0].method == "cashtag"  # highest confidence wins


def test_ambiguous_surface_is_reported_not_guessed(
    store: SQLiteBackend, reference: ReferenceData
) -> None:
    store.upsert_entity(EntityKind.COMPANY, "Acme Holdings Corp", canonical_key="x:1")
    store.upsert_entity(EntityKind.COMPANY, "Acme Holdings Inc", canonical_key="x:2")
    result = EntityDetector(store, reference).detect("Acme Holdings guidance cut")
    assert result.detections == ()
    assert [a.surface for a in result.ambiguous] == ["Acme Holdings"]
    assert len(result.ambiguous[0].candidates) == 2


def test_refresh_picks_up_new_aliases(
    store: SQLiteBackend, detector: EntityDetector, seeded: dict[str, Entity]
) -> None:
    assert detector.detect("Team Green earnings").detections == ()
    store.add_alias(seeded["NVDA"].id, "Team Green", AliasType.ABBREVIATION)
    detector.refresh()
    assert detector.detect("Team Green earnings").entity_ids == [seeded["NVDA"].id]


def test_spans_point_into_text(detector: EntityDetector) -> None:
    text = "Compare $AMD with Nvidia and TSMC"
    for d in detector.detect(text).detections:
        assert text[d.start : d.end] == d.surface


def test_works_without_reference(store: SQLiteBackend, seeded: dict[str, Entity]) -> None:
    detector = EntityDetector(store)
    assert detector.detect("NVDA and TSLA").entity_ids == [seeded["NVDA"].id]
