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

Contributions add up. Theses scoring at least ``threshold`` get an alert that
lists the strongest paths, so the agent can see *why* before judging.
Only active and at-risk theses are considered.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime

from pydantic import BaseModel

from torus.core.models import (
    Alert,
    ClaimEntityRole,
    Event,
    EventKind,
    ImpactPath,
    ThesisClaimRole,
    ThesisStatus,
)
from torus.core.store.base import Backend

MAX_HOPS = 2
HOP_DECAY = 0.5
DEFAULT_THRESHOLD = 0.2
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


@dataclass
class IngestResult:
    event: Event
    created: bool
    alerts: list[Alert] = field(default_factory=list)


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
) -> list[ImpactHit]:
    """Theses an event on ``entity_ids`` at time ``at`` may affect, best first."""
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

    hits = []
    for thesis_id, paths in contributions.items():
        score = round(sum(p.contribution for p in paths), 4)
        if score < threshold:
            continue
        paths.sort(key=lambda p: -p.contribution)
        hits.append(
            ImpactHit(
                thesis_id=thesis_id,
                thesis_title=store.get_thesis(thesis_id).title,
                score=score,
                paths=paths[:MAX_PATHS_PER_ALERT],
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
    threshold: float = DEFAULT_THRESHOLD,
) -> IngestResult:
    """Record an event and raise alerts on the theses it may affect.

    A repeated event (same URI, or same title and day) creates nothing new.
    """
    event, created = store.add_event(
        kind,
        title,
        occurred_at,
        entity_ids=entity_ids,
        source=source,
        uri=uri,
        summary=summary,
        payload=dict(payload or {}),
    )
    result = IngestResult(event=event, created=created)
    if not created:
        return result
    for hit in impact(store, event.entity_ids, event.occurred_at, threshold=threshold):
        alert, new = store.upsert_alert(event.id, hit.thesis_id, hit.score, hit.paths)
        if new:
            result.alerts.append(alert)
    return result
