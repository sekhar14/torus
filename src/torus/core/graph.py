"""Graph queries over the research graph: neighbourhoods, connecting paths, and a
whole-graph snapshot for the visual explorer.

Entities are joined by typed relations (SUPPLIES, COMPETES_WITH, ...). Theses hang
off entities as subjects, through the entities their claims mention, and through
triggers. Events attach to the entities they're about, and open alerts join events
to theses. Relations are walked in both directions; ``valid_at`` restricts them to
links true at a date, ``as_of`` to links Torus believed at a date.
"""

from __future__ import annotations

from collections import deque
from collections.abc import Sequence
from datetime import datetime
from typing import Any

from pydantic import BaseModel

from torus.core.models import AlertStatus, Relation, RelationType, ThesisStatus
from torus.core.store.base import Backend

MAX_HOPS = 4
MAX_PATHS = 10


class GraphNode(BaseModel):
    id: str
    kind: str  # entity | thesis | event
    label: str
    data: dict[str, Any] = {}


class GraphEdge(BaseModel):
    id: str
    source: str
    target: str
    kind: str  # a relation type, or subject | claim | trigger | about | alert
    label: str
    confidence: float | None = None
    provenance: str | None = None


class Subgraph(BaseModel):
    focus: list[str]
    nodes: list[GraphNode]
    edges: list[GraphEdge]


class PathStep(BaseModel):
    source: str
    source_name: str
    rel: RelationType
    direction: str  # "->" along the relation, "<-" against it
    target: str
    target_name: str
    confidence: float
    relation_id: str


class GraphPath(BaseModel):
    steps: list[PathStep]
    confidence: float  # product of step confidences

    @property
    def text(self) -> str:
        if not self.steps:
            return ""
        parts = [self.steps[0].source_name]
        for s in self.steps:
            arrow = f"-{s.rel.value}->" if s.direction == "->" else f"<-{s.rel.value}-"
            parts += [arrow, s.target_name]
        return " ".join(parts)


class PathResult(BaseModel):
    source: str
    target: str
    paths: list[GraphPath]
    texts: list[str]


def _adjacency(
    relations: Sequence[Relation], rel_types: Sequence[RelationType] = ()
) -> dict[str, list[tuple[str, Relation, str]]]:
    """entity -> [(neighbour, relation, direction)] over both directions."""
    allowed = set(rel_types)
    adj: dict[str, list[tuple[str, Relation, str]]] = {}
    for r in relations:
        if allowed and r.rel not in allowed:
            continue
        adj.setdefault(r.src_id, []).append((r.dst_id, r, "->"))
        adj.setdefault(r.dst_id, []).append((r.src_id, r, "<-"))
    return adj


def neighbors(
    store: Backend,
    entity_ids: Sequence[str],
    hops: int = 1,
    rel_types: Sequence[RelationType] = (),
    as_of: datetime | None = None,
    valid_at: datetime | None = None,
    include_theses: bool = True,
) -> Subgraph:
    """Entities within ``hops`` of the focus entities, the relations among them,
    and (optionally) the theses about any of them."""
    hops = max(0, min(hops, MAX_HOPS))
    focus = list(dict.fromkeys(store.live_entity_id(e) for e in entity_ids))
    all_rels = store.relations(as_of=as_of, valid_at=valid_at)
    adj = _adjacency(all_rels, rel_types)
    distance = {e: 0 for e in focus}
    queue = deque(focus)
    while queue:
        here = queue.popleft()
        if distance[here] >= hops:
            continue
        for other, _, _ in adj.get(here, []):
            if other not in distance:
                distance[other] = distance[here] + 1
                queue.append(other)
    # Every link among the neighbourhood, including ones between two outer nodes.
    allowed = set(rel_types)
    rels = [
        r
        for r in all_rels
        if r.src_id in distance and r.dst_id in distance and (not allowed or r.rel in allowed)
    ]
    return _subgraph(store, focus, list(distance), rels, include_theses, distance)


def find_paths(
    store: Backend,
    source: str,
    target: str,
    max_hops: int = 3,
    rel_types: Sequence[RelationType] = (),
    as_of: datetime | None = None,
    valid_at: datetime | None = None,
    limit: int = MAX_PATHS,
) -> PathResult:
    """Simple paths from ``source`` to ``target`` (either direction per hop),
    shortest first, then most confident."""
    src, dst = store.live_entity_id(source), store.live_entity_id(target)
    max_hops = max(1, min(max_hops, MAX_HOPS))
    names = {eid: name for eid, (_, name) in store.entity_index().items()}
    adj = _adjacency(store.relations(as_of=as_of, valid_at=valid_at), rel_types)
    found: list[GraphPath] = []

    def walk(node: str, visited: set[str], steps: list[PathStep]) -> None:
        if len(found) >= limit * 5:  # enough candidates to rank
            return
        if node == dst and steps:
            confidence = 1.0
            for s in steps:
                confidence *= s.confidence
            found.append(GraphPath(steps=list(steps), confidence=round(confidence, 4)))
            return
        if len(steps) >= max_hops:
            return
        for other, rel, direction in adj.get(node, []):
            if other in visited:
                continue
            step = PathStep(
                source=node,
                source_name=names.get(node, node),
                rel=rel.rel,
                direction=direction,
                target=other,
                target_name=names.get(other, other),
                confidence=rel.confidence,
                relation_id=rel.id,
            )
            walk(other, visited | {other}, [*steps, step])

    if src != dst:
        walk(src, {src}, [])
    found.sort(key=lambda p: (len(p.steps), -p.confidence))
    paths = found[:limit]
    return PathResult(source=src, target=dst, paths=paths, texts=[p.text for p in paths])


def snapshot(
    store: Backend,
    as_of: datetime | None = None,
    include_isolated: bool = False,
) -> Subgraph:
    """The whole graph: entities that have relations or research, their relations,
    theses, recent events and open alerts. Feeds the visual explorer."""
    rels = store.relations(as_of=as_of)
    linked = {r.src_id for r in rels} | {r.dst_id for r in rels}
    researched = {e for t in store.theses() for e in _thesis_entities(store, t.id)}
    entity_ids = set(store.entity_index()) if include_isolated else linked | researched
    return _subgraph(store, [], sorted(entity_ids), rels, True, {}, with_events=True)


def _thesis_entities(store: Backend, thesis_id: str) -> set[str]:
    thesis = store.get_thesis(thesis_id)
    ids = {thesis.subject_entity_id} if thesis.subject_entity_id else set()
    for link in thesis.claims:
        claim = store.current_claim(link.claim_lineage_id)
        if claim:
            ids |= {ce.entity_id for ce in claim.entities}
    ids |= {t.entity_id for t in thesis.triggers if t.entity_id}
    return ids


def _subgraph(
    store: Backend,
    focus: list[str],
    entity_ids: list[str],
    rels: Sequence[Relation],
    include_theses: bool,
    distance: dict[str, int],
    with_events: bool = False,
) -> Subgraph:
    index = store.entity_index()
    members = set(entity_ids)
    nodes: list[GraphNode] = []
    edges: list[GraphEdge] = []
    for eid in entity_ids:
        if eid not in index:
            continue
        entity = store.get_entity(eid)
        nodes.append(
            GraphNode(
                id=eid,
                kind="entity",
                label=entity.name,
                data={
                    "entity_kind": entity.kind.value,
                    "canonical_key": entity.canonical_key,
                    "tickers": [a.alias for a in entity.aliases if a.alias_type.value == "ticker"],
                    "hops": distance.get(eid),
                },
            )
        )
    for r in rels:
        if r.src_id in members and r.dst_id in members:
            edges.append(
                GraphEdge(
                    id=r.id,
                    source=r.src_id,
                    target=r.dst_id,
                    kind=r.rel.value,
                    label=r.rel.value.replace("_", " ").lower(),
                    confidence=r.confidence,
                    provenance=r.source,
                )
            )

    thesis_ids: list[str] = []
    if include_theses:
        seen: set[str] = set()
        for eid in entity_ids:
            for thesis in store.theses(entity_id=eid):
                if thesis.id in seen or thesis.status is ThesisStatus.ARCHIVED:
                    continue
                seen.add(thesis.id)
                thesis_ids.append(thesis.id)
                nodes.append(
                    GraphNode(
                        id=thesis.id,
                        kind="thesis",
                        label=thesis.title,
                        data={
                            "status": thesis.status.value,
                            "stance": thesis.stance.value,
                            "confidence": thesis.confidence,
                            "horizon": thesis.horizon,
                        },
                    )
                )
                touched = _thesis_entities(store, thesis.id) & members
                for eid2 in sorted(touched):
                    kind = "subject" if eid2 == thesis.subject_entity_id else "claim"
                    if any(t.entity_id == eid2 for t in thesis.triggers) and kind != "subject":
                        kind = "trigger"
                    edges.append(
                        GraphEdge(
                            id=f"{thesis.id}:{eid2}",
                            source=thesis.id,
                            target=eid2,
                            kind=kind,
                            label=kind,
                        )
                    )

    if with_events:
        open_alerts = store.alerts(statuses=[AlertStatus.OPEN], thesis_ids=thesis_ids)
        alerted_events = {a.event_id for a in open_alerts}
        events = {e.id: e for e in store.events(limit=200)}
        for event_id in alerted_events - set(events):
            events[event_id] = store.get_event(event_id)
        for event in events.values():
            about = [e for e in event.entity_ids if e in members]
            if not about:
                continue
            nodes.append(
                GraphNode(
                    id=event.id,
                    kind="event",
                    label=event.title,
                    data={
                        "event_kind": event.kind.value,
                        "occurred_at": event.occurred_at.isoformat(),
                        "uri": event.uri,
                        "has_open_alert": event.id in alerted_events,
                    },
                )
            )
            for eid in about:
                edges.append(
                    GraphEdge(
                        id=f"{event.id}:{eid}",
                        source=event.id,
                        target=eid,
                        kind="about",
                        label="about",
                    )
                )
        for alert in open_alerts:
            if alert.event_id in events:
                edges.append(
                    GraphEdge(
                        id=alert.id,
                        source=alert.event_id,
                        target=alert.thesis_id,
                        kind="alert",
                        label=f"alert {alert.score:.2f}",
                        confidence=alert.score,
                    )
                )
    return Subgraph(focus=focus, nodes=nodes, edges=edges)
