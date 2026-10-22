"""Which theses does an event touch? Deterministic graph traversal, no LLM.

From the event's entities, walk relations (both directions) that were valid when
the event happened, up to ``MAX_HOPS``. Every entity reached at ``h`` hops carries
a reach factor ``product(relation confidences) * HOP_DECAY ** h``. A thesis gains
score for each way a reached entity bears on it:

* it is the thesis's subject                     factor * 1.0
* a current claim the thesis links mentions it   factor * claim confidence
                                                 * role weight (assumption 1.0,
                                                   risk 0.8, supporting 0.6)
                                                 * link weight
                                                 * mention weight (subject 1.0,
                                                   object 0.8, mentioned 0.6)
* a thesis trigger watches it                    factor * 1.0

Contributions add up to the thesis's *graph score*. The alert score then weighs
in what the event is (``torus.core.materiality``) and whether its text says what
one of the thesis's triggers warns about (``torus.core.triggers``)::

    score = graph score * materiality * (1 + best trigger match)

Theses scoring at least their threshold (the thesis's own, else the global
``alert_threshold`` setting, else ``DEFAULT_THRESHOLD``) get an alert that lists
the strongest paths, so the agent can see *why* before judging. Mutes on a thesis
suppress matching events. Only active and at-risk theses are considered.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime

from pydantic import BaseModel

from torus.core import triggers
from torus.core.materiality import assess
from torus.core.models import (
    Alert,
    ClaimEntityRole,
    Event,
    EventKind,
    ImpactPath,
    ThesisClaimRole,
    ThesisMute,
    ThesisStatus,
)
from torus.core.store.base import Backend

MAX_HOPS = 2
HOP_DECAY = 0.5
DEFAULT_THRESHOLD = 0.2
THRESHOLD_SETTING = "alert_threshold"
MAX_PATHS_PER_ALERT = 3
ROLE_WEIGHT = {
    ThesisClaimRole.ASSUMPTION: 1.0,
    ThesisClaimRole.RISK: 0.8,
    ThesisClaimRole.SUPPORTING: 0.6,
}
MENTION_WEIGHT = {
    ClaimEntityRole.SUBJECT: 1.0,
    ClaimEntityRole.OBJECT: 0.8,
    ClaimEntityRole.MENTIONED: 0.6,
}
OPEN_STATUSES = (ThesisStatus.ACTIVE, ThesisStatus.AT_RISK)


@dataclass(frozen=True)
class Reach:
    entity_id: str
    hops: int
    confidence: float  # product of relation confidences along the path
    route: str

    @property
    def factor(self) -> float:
        return self.confidence * HOP_DECAY**self.hops


class ImpactHit(BaseModel):
    thesis_id: str
    thesis_title: str
    score: float
    paths: list[ImpactPath]
    graph_score: float
    materiality: float = 1.0
    trigger_match: float = 0.0
    matched_trigger: str | None = None


@dataclass
class IngestResult:
    event: Event
    created: bool
    alerts: list[Alert] = field(default_factory=list)
    muted: int = 0  # theses that would have been alerted but for a mute


def global_threshold(store: Backend) -> float:
    """The ``alert_threshold`` setting, or ``DEFAULT_THRESHOLD``."""
    value = store.get_setting(THRESHOLD_SETTING)
    try:
        return float(value) if value is not None else DEFAULT_THRESHOLD
    except ValueError:
        return DEFAULT_THRESHOLD


def is_muted(mute: ThesisMute, event: Event) -> bool:
    """Whether ``mute`` covers ``event`` (every field it sets must match)."""
    if mute.until is not None and event.occurred_at >= mute.until:
        return False
    if mute.entity_id is not None and mute.entity_id not in event.entity_ids:
        return False
    if mute.event_kind is not None and mute.event_kind is not event.kind:
        return False
    if mute.form:
        form = str(event.payload.get("form") or "").upper().removesuffix("/A")
        if form != mute.form.upper().removesuffix("/A") and f" {mute.form.upper()}" not in (
            f" {event.title.upper()}"
        ):
            return False
    return True


def _names(store: Backend, entity_ids: Sequence[str]) -> list[str]:
    """Names and aliases of the event's entities (left out of trigger matching)."""
    names = []
    for entity_id in entity_ids:
        entity = store.get_entity(store.live_entity_id(entity_id))
        names.append(entity.name)
        names += [a.alias for a in entity.aliases]
    return names


def reach(
    store: Backend, start: Sequence[str], at: datetime, max_hops: int = MAX_HOPS
) -> dict[str, Reach]:
    """Best (highest-factor) way to reach each entity within ``max_hops``."""
    names = {eid: name for eid, (_, name) in store.entity_index().items()}
    best: dict[str, Reach] = {}
    frontier = []
    for entity_id in dict.fromkeys(store.live_entity_id(e) for e in start):
        r = Reach(entity_id, 0, 1.0, names.get(entity_id, entity_id))
        best[entity_id] = r
        frontier.append(r)
    for hop in range(1, max_hops + 1):
        nxt: list[Reach] = []
        for here in frontier:
            for rel in store.relations(here.entity_id, valid_at=at):
                outgoing = rel.src_id == here.entity_id
                other = rel.dst_id if outgoing else rel.src_id
                arrow = f" -{rel.rel.value}-> " if outgoing else f" <-{rel.rel.value}- "
                candidate = Reach(
                    other,
                    hop,
                    here.confidence * rel.confidence,
                    here.route + arrow + names.get(other, other),
                )
                if other not in best or candidate.factor > best[other].factor:
                    best[other] = candidate
                    nxt.append(candidate)
        frontier = nxt
    return best


def impact(
    store: Backend,
    entity_ids: Sequence[str],
    at: datetime,
    max_hops: int = MAX_HOPS,
    threshold: float = DEFAULT_THRESHOLD,
    *,
    text: str | None = None,
    materiality: float = 1.0,
) -> list[ImpactHit]:
    """Theses an event on ``entity_ids`` at time ``at`` may affect, best first.

    ``text`` (the event's title and summary) is matched against thesis triggers;
    ``threshold`` applies to theses without a threshold of their own."""
    reached = reach(store, entity_ids, at, max_hops)
    contributions: dict[str, list[ImpactPath]] = {}

    def add(thesis_id: str, path: ImpactPath) -> None:
        if path.contribution > 0:
            contributions.setdefault(thesis_id, []).append(path)

    for entity_id, r in reached.items():
        for thesis in store.theses(entity_id=entity_id, statuses=OPEN_STATUSES):
            if thesis.subject_entity_id == entity_id:
                add(
                    thesis.id,
                    ImpactPath(
                        route=r.route,
                        via="subject",
                        detail=thesis.title,
                        contribution=round(r.factor, 4),
                    ),
                )
            for link in thesis.claims:
                claim = store.current_claim(link.claim_lineage_id)
                if claim is None:
                    continue
                mention = max(
                    (MENTION_WEIGHT[ce.role] for ce in claim.entities if ce.entity_id == entity_id),
                    default=0.0,
                )
                weight = r.factor * claim.confidence * ROLE_WEIGHT[link.role] * link.weight
                add(
                    thesis.id,
                    ImpactPath(
                        route=r.route,
                        via=f"{link.role.value} claim",
                        detail=claim.text,
                        claim_id=claim.id,
                        contribution=round(weight * mention, 4),
                    ),
                )
        for thesis_id, trigger in store.thesis_triggers_for_entity(entity_id):
            thesis = store.get_thesis(thesis_id)
            if thesis.status in OPEN_STATUSES:
                add(
                    thesis_id,
                    ImpactPath(
                        route=r.route,
                        via="trigger",
                        detail=trigger.description,
                        trigger_id=trigger.id,
                        contribution=round(r.factor, 4),
                    ),
                )

    exclude = _names(store, entity_ids) if text else []
    hits = []
    for thesis_id, paths in contributions.items():
        thesis = store.get_thesis(thesis_id)
        graph_score = round(sum(p.contribution for p in paths), 4)
        best, matched = 0.0, None
        for trigger in thesis.triggers if text else ():
            value = triggers.match(text or "", trigger.description, exclude)
            if value > best:
                best, matched = value, trigger.description
        score = round(graph_score * materiality * (1 + best), 4)
        limit = thesis.alert_threshold if thesis.alert_threshold is not None else threshold
        if score < limit:
            continue
        paths.sort(key=lambda p: -p.contribution)
        hits.append(
            ImpactHit(
                thesis_id=thesis_id,
                thesis_title=thesis.title,
                score=score,
                paths=paths[:MAX_PATHS_PER_ALERT],
                graph_score=graph_score,
                materiality=materiality,
                trigger_match=best,
                matched_trigger=matched,
            )
        )
    hits.sort(key=lambda h: (-h.score, h.thesis_id))
    return hits


def ingest_event(
    store: Backend,
    kind: EventKind,
    title: str,
    occurred_at: datetime,
    entity_ids: Sequence[str],
    source: str = "manual",
    uri: str | None = None,
    summary: str | None = None,
    payload: dict[str, object] | None = None,
    threshold: float | None = None,
    materiality: float | None = None,
) -> IngestResult:
    """Record an event and raise alerts on the theses it may affect.

    ``materiality`` defaults to an assessment of the source, form and wording;
    ``threshold`` to the global setting. A repeated event (same URI, or same
    title and day) creates nothing new.
    """
    if materiality is None:
        assessed = assess(source, title, summary, payload)
        weight, why = assessed.value, assessed.reason
    else:
        weight, why = materiality, "set when recorded"
    event, created = store.add_event(
        kind,
        title,
        occurred_at,
        entity_ids=entity_ids,
        source=source,
        uri=uri,
        summary=summary,
        payload=dict(payload or {}),
        materiality=weight,
        materiality_reason=why,
    )
    result = IngestResult(event=event, created=created)
    if not created:
        return result
    hits = impact(
        store,
        event.entity_ids,
        event.occurred_at,
        threshold=global_threshold(store) if threshold is None else threshold,
        text=f"{event.title}. {event.summary or ''}",
        materiality=event.materiality,
    )
    for hit in hits:
        if any(is_muted(m, event) for m in store.thesis_mutes(hit.thesis_id)):
            result.muted += 1
            continue
        alert, new = store.upsert_alert(
            event.id,
            hit.thesis_id,
            hit.score,
            hit.paths,
            graph_score=hit.graph_score,
            materiality=hit.materiality,
            trigger_match=hit.trigger_match,
            matched_trigger=hit.matched_trigger,
        )
        if new:
            result.alerts.append(alert)
    return result
