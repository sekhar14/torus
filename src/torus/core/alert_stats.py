"""How useful are the alerts? Verdicts per kind of alert, no LLM.

Every judged alert gets a verdict; ``unrelated`` means Torus connected an event
to a thesis it didn't bear on. The share of judged alerts that were *not*
unrelated is the alerts' precision. Broken down by source, form, hop count and
materiality band, it shows which weights to tune, and whether alerts help at all.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Callable
from datetime import datetime

from pydantic import BaseModel

from torus.core.models import Alert, AlertStatus, Event, Verdict
from torus.core.store.base import Backend


class Bucket(BaseModel):
    label: str
    alerts: int
    judged: int
    unrelated: int
    precision: float | None  # judged alerts that were not unrelated; None if none judged


class AlertStats(BaseModel):
    alerts: int
    open: int
    judged: int
    unrelated: int
    precision: float | None
    by_verdict: dict[str, int]
    by_source: list[Bucket]
    by_form: list[Bucket]
    by_hops: list[Bucket]
    by_materiality: list[Bucket]


def hops(alert: Alert) -> int:
    """Links between the event and the thesis on the alert's strongest path."""
    if not alert.paths:
        return 0
    route = alert.paths[0].route  # "TSMC -SUPPLIES-> NVIDIA" or "A <-SUPPLIES- B"
    return route.count("->") + route.count("<-")


def materiality_band(alert: Alert, event: Event) -> str:
    value = alert.materiality if alert.materiality is not None else event.materiality
    if value < 0.3:
        return "low (<0.3)"
    if value < 0.7:
        return "medium (0.3-0.7)"
    return "high (0.7+)"


def _source(event: Event) -> str:
    return event.source.split(":", 1)[0]


def _form(event: Event) -> str:
    form = str(event.payload.get("form") or "")
    return form.upper().removesuffix("/A") if form else event.kind.value


def _precision(judged: int, unrelated: int) -> float | None:
    return round((judged - unrelated) / judged, 3) if judged else None


def _buckets(pairs: list[tuple[Alert, Event]], key: Callable[[Alert, Event], str]) -> list[Bucket]:
    groups: dict[str, list[Alert]] = defaultdict(list)
    for alert, event in pairs:
        groups[key(alert, event)].append(alert)
    buckets = []
    for label, alerts in groups.items():
        judged = [a for a in alerts if a.verdict is not None]
        unrelated = sum(1 for a in judged if a.verdict is Verdict.UNRELATED)
        buckets.append(
            Bucket(
                label=label,
                alerts=len(alerts),
                judged=len(judged),
                unrelated=unrelated,
                precision=_precision(len(judged), unrelated),
            )
        )
    buckets.sort(key=lambda b: (-b.alerts, b.label))
    return buckets


def alert_stats(store: Backend, since: datetime | None = None) -> AlertStats:
    alerts = store.alerts(statuses=list(AlertStatus))
    if since is not None:
        alerts = [a for a in alerts if a.created_at >= since]
    events: dict[str, Event] = {}
    for alert in alerts:
        if alert.event_id not in events:
            events[alert.event_id] = store.get_event(alert.event_id)
    pairs = [(a, events[a.event_id]) for a in alerts]
    judged = [a for a in alerts if a.verdict is not None]
    unrelated = sum(1 for a in judged if a.verdict is Verdict.UNRELATED)
    by_verdict: dict[str, int] = defaultdict(int)
    for alert in judged:
        assert alert.verdict is not None
        by_verdict[alert.verdict.value] += 1
    return AlertStats(
        alerts=len(alerts),
        open=sum(1 for a in alerts if a.status is AlertStatus.OPEN),
        judged=len(judged),
        unrelated=unrelated,
        precision=_precision(len(judged), unrelated),
        by_verdict=dict(sorted(by_verdict.items())),
        by_source=_buckets(pairs, lambda a, e: _source(e)),
        by_form=_buckets(pairs, lambda a, e: _form(e)),
        by_hops=_buckets(pairs, lambda a, e: f"{hops(a)} hop(s)"),
        by_materiality=_buckets(pairs, materiality_band),
    )
