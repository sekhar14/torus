"""The visual explorer: one self-contained HTML file (no network, no CDN) showing
the research graph with entities, links, theses, events and alerts. Click a node
to see its claims, evidence, history and alerts.

``build_data`` collects a snapshot (or a neighbourhood around focus entities);
``render`` embeds it in the page template.
"""

from __future__ import annotations

import json
from datetime import datetime
from importlib import resources
from typing import Any

from torus import __version__
from torus.core.clock import Clock, SystemClock
from torus.core.graph import Subgraph, neighbors, snapshot
from torus.core.models import AlertStatus
from torus.core.store.base import Backend

CLAIMS_PER_ENTITY = 25
PLACEHOLDER = "__TORUS_DATA__"


def build_data(
    store: Backend,
    focus: list[str] | None = None,
    hops: int = 2,
    clock: Clock | None = None,
    include_isolated: bool = False,
) -> dict[str, Any]:
    clock = clock or SystemClock()
    if focus:
        sub: Subgraph = neighbors(store, focus, hops=hops)
        # Neighbourhood views show events and alerts too.
        full = snapshot(store, include_isolated=True)
        members = {n.id for n in sub.nodes}
        extra_nodes = [
            n
            for n in full.nodes
            if n.kind == "event"
            and n.id not in members
            and any(e.source == n.id and e.target in members for e in full.edges)
        ]
        members |= {n.id for n in extra_nodes}
        extra_edges = [
            e
            for e in full.edges
            if e.kind in ("about", "alert") and e.source in members and e.target in members
        ]
        sub = Subgraph(
            focus=sub.focus, nodes=[*sub.nodes, *extra_nodes], edges=[*sub.edges, *extra_edges]
        )
    else:
        sub = snapshot(store, include_isolated=include_isolated)

    details: dict[str, Any] = {}
    names = {eid: name for eid, (_, name) in store.entity_index().items()}
    for node in sub.nodes:
        if node.kind == "entity":
            details[node.id] = _entity_details(store, node.id)
        elif node.kind == "thesis":
            details[node.id] = _thesis_details(store, node.id, names)
        elif node.kind == "event":
            details[node.id] = _event_details(store, node.id, names)

    return {
        "meta": {
            "version": __version__,
            "generated_at": clock.now().isoformat(timespec="seconds"),
            "focus": [names.get(f, f) for f in sub.focus],
            "counts": {
                kind: sum(1 for n in sub.nodes if n.kind == kind)
                for kind in ("entity", "thesis", "event")
            },
            "links": sum(1 for e in sub.edges if e.kind.isupper()),
            "open_alerts": sum(1 for e in sub.edges if e.kind == "alert"),
        },
        "nodes": [n.model_dump() for n in sub.nodes],
        "edges": [e.model_dump() for e in sub.edges],
        "details": details,
    }


def render(data: dict[str, Any]) -> str:
    template = resources.files("torus.explorer").joinpath("template.html").read_text("utf-8")
    # "<" escaped so text in the data can never close the <script> element.
    payload = json.dumps(data, default=_json_default, separators=(",", ":"))
    payload = payload.replace("<", "\\u003c").replace(">", "\\u003e").replace("&", "\\u0026")
    if template.count(PLACEHOLDER) != 1:
        raise RuntimeError("explorer template must contain exactly one data placeholder")
    return template.replace(PLACEHOLDER, payload)


def _json_default(value: object) -> str:
    if isinstance(value, datetime):
        return value.isoformat()
    raise TypeError(f"not JSON serialisable: {type(value)}")


def _evidence(store: Backend, evidence_ids: list[str]) -> list[dict[str, Any]]:
    out = []
    for eid in evidence_ids:
        ev = store.get_evidence(eid)
        out.append({"uri": ev.uri, "title": ev.title, "quote": ev.quote})
    return out


def _entity_details(store: Backend, entity_id: str) -> dict[str, Any]:
    entity = store.get_entity(entity_id)
    claims = store.claims(entity_id=entity_id, limit=CLAIMS_PER_ENTITY)
    return {
        "name": entity.name,
        "kind": entity.kind.value,
        "canonical_key": entity.canonical_key,
        "aliases": sorted({a.alias for a in entity.aliases} - {entity.name}),
        "claims": [
            {
                "id": c.id,
                "text": c.text,
                "type": c.claim_type.value,
                "confidence": c.confidence,
                "recorded_at": c.recorded_at.isoformat(),
                "evidence": _evidence(store, [e.evidence_id for e in c.evidence]),
            }
            for c in claims
        ],
        "rejected": [
            {"text": h.text, "reason": h.reason}
            for h in store.rejected_hypotheses(entity_id=entity_id)
        ],
    }


def _thesis_details(store: Backend, thesis_id: str, names: dict[str, str]) -> dict[str, Any]:
    thesis = store.get_thesis(thesis_id)
    links = []
    for link in thesis.claims:
        claim = store.current_claim(link.claim_lineage_id)
        links.append(
            {
                "role": link.role.value,
                "text": claim.text if claim else "(retracted)",
                "confidence": claim.confidence if claim else None,
                "evidence": _evidence(store, [e.evidence_id for e in claim.evidence])
                if claim
                else [],
            }
        )
    alerts = []
    for alert in store.alerts(statuses=list(AlertStatus), thesis_ids=[thesis_id]):
        event = store.get_event(alert.event_id)
        alerts.append(
            {
                "id": alert.id,
                "status": alert.status.value,
                "verdict": alert.verdict.value if alert.verdict else None,
                "score": alert.score,
                "event": event.title,
                "uri": event.uri,
                "occurred_at": event.occurred_at.isoformat(),
                "paths": [f"{p.route} via {p.via}" for p in alert.paths],
            }
        )
    return {
        "title": thesis.title,
        "statement": thesis.statement,
        "stance": thesis.stance.value,
        "status": thesis.status.value,
        "confidence": thesis.confidence,
        "horizon": thesis.horizon,
        "subject": names.get(thesis.subject_entity_id or ""),
        "claims": links,
        "triggers": [
            {"description": t.description, "entity": names.get(t.entity_id or "")}
            for t in thesis.triggers
        ],
        "history": [
            {
                "at": h.at.isoformat(),
                "status": h.status.value,
                "confidence": h.confidence,
                "reason": h.reason,
            }
            for h in store.thesis_history(thesis_id)
        ],
        "alerts": alerts,
    }


def _event_details(store: Backend, event_id: str, names: dict[str, str]) -> dict[str, Any]:
    event = store.get_event(event_id)
    return {
        "title": event.title,
        "kind": event.kind.value,
        "occurred_at": event.occurred_at.isoformat(),
        "uri": event.uri,
        "summary": event.summary,
        "source": event.source,
        "entities": [names.get(e, e) for e in event.entity_ids],
    }
