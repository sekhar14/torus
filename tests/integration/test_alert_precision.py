"""Alert precision end to end: materiality, trigger matching, thresholds, mutes, stats."""

from __future__ import annotations

from datetime import timedelta

import pytest

from conftest import GraphFactory
from torus.cli import main
from torus.core.alert_stats import alert_stats
from torus.core.clock import ManualClock
from torus.core.impact import ingest_event
from torus.core.models import EventKind, RelationType, Verdict
from torus.core.store import SQLiteBackend


@pytest.fixture
def scenario(store: SQLiteBackend, graph: GraphFactory, clock: ManualClock) -> dict[str, str]:
    """TSMC supplies NVIDIA; an NVIDIA thesis with a CoWoS trigger on TSMC."""
    nvda, tsm = graph.company("NVDA", "NVIDIA Corp"), graph.company("TSM", "TSMC")
    store.record_relation(tsm.id, RelationType.SUPPLIES, nvda.id, 0.9)
    thesis = store.record_thesis(
        title="NVDA data-center growth, bullish 12m",
        statement="Data-center demand keeps NVIDIA supply-bound",
        stance="bullish",
        confidence=0.7,
        subject_entity_id=nvda.id,
        triggers=[("TSMC CoWoS capacity slips into 2027", tsm.id)],
    )
    clock.advance(days=1)
    return {"nvda": nvda.id, "tsm": tsm.id, "thesis": thesis.id}


def _filing(
    store: SQLiteBackend,
    clock: ManualClock,
    entity: str,
    title: str,
    form: str,
    items: tuple[str, ...] = (),
    summary: str | None = None,
) -> list[str]:
    clock.advance(hours=1)
    result = ingest_event(
        store,
        EventKind.FILING,
        title,
        clock.now(),
        [entity],
        source="edgar",
        uri=f"https://www.sec.gov/{title.replace(' ', '-')}",
        summary=summary,
        payload={"form": form, "items": list(items)},
    )
    return [a.thesis_id for a in result.alerts]


def test_routine_supplier_10q_no_longer_alerts(
    store: SQLiteBackend, clock: ManualClock, scenario: dict[str, str]
) -> None:
    alerts = _filing(store, clock, scenario["tsm"], "TSMC 10-Q: quarterly report", "10-Q")
    event = store.events(limit=1)[0]
    assert (event.materiality, event.materiality_reason) == (0.4, "10-Q filing")
    # The trigger watches TSMC directly (1.0) and NVIDIA is one hop away (0.45):
    # 1.45 * 0.4 = 0.58 still alerts. Drop the trigger to see a pure supplier path.
    assert alerts == [scenario["thesis"]]
    [alert] = store.alerts()
    assert (alert.graph_score, alert.materiality, alert.trigger_match) == (1.45, 0.4, 0.0)
    assert alert.score == 0.58


def test_supplier_paths_need_a_material_event(
    store: SQLiteBackend, graph: GraphFactory, clock: ManualClock
) -> None:
    nvda, tsm = graph.company("NVDA", "NVIDIA Corp"), graph.company("TSM", "TSMC")
    store.record_relation(tsm.id, RelationType.SUPPLIES, nvda.id, 0.9)
    thesis = graph.thesis("NVDA supply-bound", nvda)
    assert _filing(store, clock, tsm.id, "TSMC 10-Q: quarterly report", "10-Q") == []
    assert _filing(store, clock, tsm.id, "TSMC 8-K: shareholder vote", "8-K", ("5.07",)) == []
    assert _filing(store, clock, tsm.id, "TSMC 8-K: agreement", "8-K", ("1.01",)) == [thesis.id]
    [alert] = store.alerts()
    assert (alert.graph_score, alert.materiality) == (0.45, 0.8)
    assert alert.score == 0.36


def test_trigger_wording_boosts_the_score(
    store: SQLiteBackend, clock: ManualClock, scenario: dict[str, str]
) -> None:
    clock.advance(hours=1)
    result = ingest_event(
        store,
        EventKind.NEWS,
        "TSMC delays CoWoS capacity expansion",
        clock.now(),
        [scenario["tsm"]],
        source="rss:wire",
    )
    [alert] = result.alerts
    assert alert.matched_trigger == "TSMC CoWoS capacity slips into 2027"
    assert alert.trigger_match == 0.75
    assert alert.materiality == 0.7  # "delay or shortage"
    assert alert.score == round(1.45 * 0.7 * 1.75, 4)


def test_thesis_threshold_and_global_setting(
    store: SQLiteBackend, clock: ManualClock, scenario: dict[str, str]
) -> None:
    store.set_alert_threshold(scenario["thesis"], 0.9)
    assert _filing(store, clock, scenario["tsm"], "TSMC 10-Q: Q1", "10-Q") == []
    store.set_alert_threshold(scenario["thesis"], None)
    store.set_setting("alert_threshold", "0.7")
    assert _filing(store, clock, scenario["tsm"], "TSMC 10-Q: Q2", "10-Q") == []
    store.set_setting("alert_threshold", None)
    assert _filing(store, clock, scenario["tsm"], "TSMC 10-Q: Q3", "10-Q") == [scenario["thesis"]]
    with pytest.raises(ValueError):
        store.set_alert_threshold(scenario["thesis"], 11)


def test_mutes(store: SQLiteBackend, clock: ManualClock, scenario: dict[str, str]) -> None:
    mute = store.add_thesis_mute(scenario["thesis"], entity_id=scenario["tsm"], form="10-q")
    assert mute.form == "10-Q"
    assert _filing(store, clock, scenario["tsm"], "TSMC 10-Q: Q1", "10-Q") == []
    # The mute is specific: an 8-K from TSMC still alerts.
    assert _filing(store, clock, scenario["tsm"], "TSMC 8-K: deal", "8-K", ("1.01",)) == [
        scenario["thesis"]
    ]
    assert store.remove_thesis_mute(mute.id)
    assert not store.remove_thesis_mute(mute.id)
    assert _filing(store, clock, scenario["tsm"], "TSMC 10-Q: Q2", "10-Q") == [scenario["thesis"]]

    expiring = store.add_thesis_mute(
        scenario["thesis"], event_kind=EventKind.FILING, until=clock.now() + timedelta(days=1)
    )
    assert _filing(store, clock, scenario["tsm"], "TSMC 10-Q: Q3", "10-Q") == []
    clock.advance(days=2)
    assert _filing(store, clock, scenario["tsm"], "TSMC 10-Q: Q4", "10-Q") == [scenario["thesis"]]
    assert [m.id for m in store.thesis_mutes(scenario["thesis"])] == [expiring.id]
    with pytest.raises(ValueError, match="needs an entity"):
        store.add_thesis_mute(scenario["thesis"])


def test_alert_stats(store: SQLiteBackend, clock: ManualClock, scenario: dict[str, str]) -> None:
    _filing(store, clock, scenario["tsm"], "TSMC 10-Q: Q1", "10-Q")
    _filing(store, clock, scenario["tsm"], "TSMC 8-K: deal", "8-K", ("1.01",))
    _filing(store, clock, scenario["nvda"], "NVIDIA 8-K: results", "8-K", ("2.02",))
    q1, deal, results = sorted(store.alerts(), key=lambda a: a.created_at)
    store.resolve_alert(q1.id, Verdict.UNRELATED, "routine filing")
    store.resolve_alert(deal.id, Verdict.SUPPORTS, "more capacity")

    stats = alert_stats(store)
    assert (stats.alerts, stats.open, stats.judged, stats.unrelated) == (3, 1, 2, 1)
    assert stats.precision == 0.5
    assert stats.by_verdict == {"supports": 1, "unrelated": 1}
    forms = {b.label: b for b in stats.by_form}
    assert forms["10-Q"].precision == 0.0 and forms["8-K"].precision == 1.0
    assert forms["8-K"].alerts == 2 and forms["8-K"].judged == 1
    hops = {b.label for b in stats.by_hops}
    assert hops <= {"0 hop(s)", "1 hop(s)"}
    assert results.id in {a.id for a in store.alerts()}


def test_cli_alerts_commands(torus_home: object, capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["init"]) == 0
    assert main(["config", "set", "alert_threshold", "0.5"]) == 0
    assert main(["config", "set", "alert_threshold", "eleven"]) == 2
    assert main(["alerts", "threshold", "default"]) == 0
    assert main(["alerts", "list"]) == 0
    assert main(["alerts", "stats"]) == 0
    assert main(["alerts", "mutes"]) == 0
    out = capsys.readouterr().out
    assert "No open alerts." in out and "Precision n/a" in out and "No mutes." in out
