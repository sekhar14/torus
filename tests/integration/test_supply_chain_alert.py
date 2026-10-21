"""The golden scenario: a supplier's filing reaches a customer thesis."""

from __future__ import annotations

from datetime import timedelta

import pytest

from conftest import GraphFactory
from torus.core.clock import ManualClock
from torus.core.errors import NotCurrentError
from torus.core.impact import DEFAULT_THRESHOLD, impact, ingest_event, reach
from torus.core.models import (
    AlertStatus,
    EventKind,
    RelationType,
    ThesisClaimRole,
    ThesisStatus,
    Verdict,
)
from torus.core.recall import recall, render_context
from torus.core.store import SQLiteBackend


@pytest.fixture
def scenario(store: SQLiteBackend, graph: GraphFactory, clock: ManualClock) -> dict[str, str]:
    nvda, tsm = graph.company("NVDA"), graph.company("TSM")
    store.record_relation(tsm.id, RelationType.SUPPLIES, nvda.id, 0.9)
    claim = graph.fact("TSMC CoWoS capacity constrains NVDA data-center shipments", nvda, tsm)
    thesis = graph.thesis("NVDA supply-bound, bullish 12m", nvda, claim.id)
    clock.advance(days=7)
    return {"nvda": nvda.id, "tsm": tsm.id, "claim": claim.id, "thesis": thesis.id}


def test_supplier_8k_raises_one_explained_alert(
    store: SQLiteBackend, clock: ManualClock, scenario: dict[str, str]
) -> None:
    result = ingest_event(
        store,
        EventKind.FILING,
        "TSMC 8-K: CoWoS capacity expansion delayed",
        clock.now(),
        [scenario["tsm"]],
        source="edgar",
        uri="https://www.sec.gov/Archives/edgar/data/1046179/example-8k.htm",
    )
    assert result.created
    [alert] = result.alerts
    assert alert.thesis_id == scenario["thesis"]
    assert alert.status is AlertStatus.OPEN
    assert alert.score > DEFAULT_THRESHOLD
    vias = {(p.route, p.via) for p in alert.paths}
    assert ("TSM Inc", "assumption claim") in vias  # the claim names TSMC directly
    assert ("TSM Inc -SUPPLIES-> NVDA Inc", "subject") in vias  # one hop to the subject
    assert alert.paths[0].claim_id == scenario["claim"]


def test_alert_shows_in_recall_and_resolution_updates_thesis(
    store: SQLiteBackend, clock: ManualClock, scenario: dict[str, str]
) -> None:
    before = clock.now()
    [alert] = ingest_event(
        store, EventKind.FILING, "TSMC 8-K: CoWoS delay", clock.now(), [scenario["tsm"]]
    ).alerts

    result = recall(store, clock, entity_ids=[scenario["nvda"]])
    assert [a.id for a in result.alerts] == [alert.id]
    text = render_context(result, now=clock.now())
    lines = text.splitlines()
    assert lines.index("### Open alerts - review with /torus:review") < lines.index("### Theses")

    clock.advance(hours=2)
    resolved = store.resolve_alert(
        alert.id, Verdict.WEAKENS, "Delay pushes CoWoS relief into 2027", confidence=0.5
    )
    assert resolved.status is AlertStatus.RESOLVED and resolved.verdict is Verdict.WEAKENS
    thesis = store.get_thesis(scenario["thesis"])
    assert (thesis.status, thesis.confidence) == (ThesisStatus.AT_RISK, 0.5)
    last = store.thesis_history(thesis.id)[-1]
    assert last.alert_id == alert.id and "CoWoS delay" in (last.reason or "")
    then = store.thesis_state_as_of(thesis.id, before)
    assert then is not None and then.confidence == 0.7  # the past is preserved
    assert recall(store, clock, entity_ids=[scenario["nvda"]]).alerts == []
    with pytest.raises(NotCurrentError):
        store.resolve_alert(alert.id, Verdict.SUPPORTS, "again")


@pytest.mark.parametrize(
    ("verdict", "status", "alert_status"),
    [
        (Verdict.BREAKS, ThesisStatus.BROKEN, AlertStatus.RESOLVED),
        (Verdict.SUPPORTS, ThesisStatus.ACTIVE, AlertStatus.RESOLVED),
        (Verdict.UNRELATED, ThesisStatus.ACTIVE, AlertStatus.DISMISSED),
    ],
)
def test_verdicts(
    store: SQLiteBackend,
    clock: ManualClock,
    scenario: dict[str, str],
    verdict: Verdict,
    status: ThesisStatus,
    alert_status: AlertStatus,
) -> None:
    [alert] = ingest_event(store, EventKind.NEWS, "x", clock.now(), [scenario["tsm"]]).alerts
    resolved = store.resolve_alert(alert.id, verdict, "because")
    assert resolved.status is alert_status
    assert store.get_thesis(scenario["thesis"]).status is status
    history = len(store.thesis_history(scenario["thesis"]))
    assert history == (1 if verdict in (Verdict.SUPPORTS, Verdict.UNRELATED) else 2)


def test_same_event_twice_raises_nothing_new(
    store: SQLiteBackend, clock: ManualClock, scenario: dict[str, str]
) -> None:
    uri = "https://www.sec.gov/x/8-k.htm"
    first = ingest_event(
        store, EventKind.FILING, "TSMC 8-K", clock.now(), [scenario["tsm"]], uri=uri
    )
    again = ingest_event(
        store, EventKind.FILING, "TSMC 8-K (re-seen)", clock.now(), [scenario["tsm"]], uri=uri
    )
    assert first.created and not again.created
    assert again.event.id == first.event.id and again.alerts == []
    assert len(store.alerts()) == 1


def test_event_dedupe_by_title_and_day_without_uri(
    store: SQLiteBackend, clock: ManualClock
) -> None:
    a, created_a = store.add_event(EventKind.NEWS, "Fed holds rates", clock.now())
    b, created_b = store.add_event(
        EventKind.NEWS, "fed holds rates ", clock.now() + timedelta(hours=3)
    )
    c, created_c = store.add_event(
        EventKind.NEWS, "Fed holds rates", clock.now() + timedelta(days=1)
    )
    assert created_a and not created_b and created_c
    assert a.id == b.id != c.id


def test_impact_respects_relation_validity_at_event_time(
    store: SQLiteBackend, graph: GraphFactory, clock: ManualClock
) -> None:
    nvda, tsm = graph.company("NVDA"), graph.company("TSM")
    rel = store.record_relation(tsm.id, RelationType.SUPPLIES, nvda.id, 0.9)
    store.supersede_relation(rel.id, valid_to=clock.now() + timedelta(days=1))
    graph.thesis("NVDA thesis", nvda)
    assert impact(store, [tsm.id], clock.now())  # supplier at the time
    assert impact(store, [tsm.id], clock.now() + timedelta(days=2)) == []  # contract ended


def test_impact_max_two_hops(store: SQLiteBackend, graph: GraphFactory, clock: ManualClock) -> None:
    a, b, c, d = (graph.company(t) for t in ("AAA", "BBB", "CCC", "DDD"))
    for src, dst in ((a, b), (b, c), (c, d)):
        store.record_relation(src.id, RelationType.SUPPLIES, dst.id, 1.0)
    graph.thesis("on C", c)
    graph.thesis("on D", d)
    reached = reach(store, [a.id], clock.now())
    assert {r.hops for r in reached.values()} == {0, 1, 2}
    titles = [h.thesis_title for h in impact(store, [a.id], clock.now(), threshold=0.01)]
    assert titles == ["on C"]  # D is three hops away


def test_impact_scoring_monotonic(
    store: SQLiteBackend, graph: GraphFactory, clock: ManualClock
) -> None:
    nvda, tsm, amd = graph.company("NVDA"), graph.company("TSM"), graph.company("AMD")
    store.record_relation(tsm.id, RelationType.SUPPLIES, nvda.id, 0.9)
    store.record_relation(tsm.id, RelationType.SUPPLIES, amd.id, 0.4)
    direct = graph.thesis("on TSM itself", tsm)
    strong = graph.thesis("on NVDA (strong link)", nvda)
    weak = graph.thesis("on AMD (weak link)", amd)
    scores = {h.thesis_id: h.score for h in impact(store, [tsm.id], clock.now(), threshold=0)}
    assert scores[direct.id] > scores[strong.id] > scores[weak.id]


def test_assumption_outweighs_supporting(
    store: SQLiteBackend, graph: GraphFactory, clock: ManualClock
) -> None:
    tsm = graph.company("TSM")
    claim = graph.fact("TSMC raises prices", tsm)
    leans = store.record_thesis("leans on it", "s", "bullish", 0.6, assumptions=[claim.id])
    cites = store.record_thesis("cites it", "s", "bullish", 0.6, supporting=[claim.id])
    scores = {h.thesis_id: h.score for h in impact(store, [tsm.id], clock.now(), threshold=0)}
    assert scores[leans.id] > scores[cites.id]


def test_trigger_entity_raises_alert_without_any_claim(
    store: SQLiteBackend, graph: GraphFactory, clock: ManualClock
) -> None:
    nvda, asml = graph.company("NVDA"), graph.company("ASML")
    thesis = graph.thesis("NVDA thesis", nvda)
    store.revise_thesis(thesis.id, "watch ASML", add_triggers=[("ASML export ban", asml.id)])
    [hit] = impact(store, [asml.id], clock.now())
    assert hit.thesis_id == thesis.id
    assert hit.paths[0].via == "trigger" and hit.paths[0].detail == "ASML export ban"


def test_closed_theses_get_no_alerts(
    store: SQLiteBackend, clock: ManualClock, scenario: dict[str, str]
) -> None:
    store.update_thesis(scenario["thesis"], "done", status=ThesisStatus.CONFIRMED)
    assert ingest_event(store, EventKind.NEWS, "x", clock.now(), [scenario["tsm"]]).alerts == []


def test_risk_claim_link_reaches_thesis(
    store: SQLiteBackend, graph: GraphFactory, clock: ManualClock
) -> None:
    nvda, gov = graph.company("NVDA"), graph.company("GOV")
    thesis = graph.thesis("NVDA thesis", nvda)
    risk = graph.fact("Export controls may tighten", gov)
    store.revise_thesis(thesis.id, "risk", add_claims=[(risk.id, ThesisClaimRole.RISK, 1.0)])
    [hit] = impact(store, [gov.id], clock.now())
    assert hit.paths[0].via == "risk claim"
