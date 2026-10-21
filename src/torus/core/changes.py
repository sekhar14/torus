"""Chronological views of the graph: ``timeline`` for one entity, ``what_changed``
for everything since a point in time."""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum

from pydantic import BaseModel

from torus.core.models import ClaimStatus, ThesisHistoryEntry
from torus.core.store.base import Backend


class ChangeKind(StrEnum):
    # Declaration order breaks timestamp ties: an old version closes before
    # its replacement is recorded.
    ENTITY_ADDED = "entity_added"
    CLAIM_SUPERSEDED = "claim_superseded"
    CLAIM_RETRACTED = "claim_retracted"
    CLAIM_RECORDED = "claim_recorded"
    THESIS_CREATED = "thesis_created"
    THESIS_CHANGED = "thesis_changed"
    EVENT_ADDED = "event_added"


class Change(BaseModel):
    at: datetime
    kind: ChangeKind
    object_id: str
    summary: str
    reason: str | None = None


class ChangeLog(BaseModel):
    since: datetime | None
    until: datetime | None
    entity_id: str | None
    changes: list[Change]


def _in_window(at: datetime, since: datetime | None, until: datetime | None) -> bool:
    return (since is None or at >= since) and (until is None or at < until)


def _thesis_change(store: Backend, entry: ThesisHistoryEntry) -> Change:
    title = store.get_thesis(entry.thesis_id).title
    created = entry.reason == "created"
    return Change(
        at=entry.at,
        kind=ChangeKind.THESIS_CREATED if created else ChangeKind.THESIS_CHANGED,
        object_id=entry.thesis_id,
        summary=f"{title}: {entry.status.value}, conf {entry.confidence:.2f}",
        reason=None if created else entry.reason,
    )


def _changes(
    store: Backend,
    entity_id: str | None,
    since: datetime | None,
    until: datetime | None,
) -> list[Change]:
    changes: list[Change] = []
    for claim in store.claim_versions(entity_id=entity_id, since=since, until=until):
        if _in_window(claim.recorded_at, since, until):
            changes.append(
                Change(
                    at=claim.recorded_at,
                    kind=ChangeKind.CLAIM_RECORDED,
                    object_id=claim.id,
                    summary=f"{claim.claim_type.value} ({claim.confidence:.2f}): {claim.text}",
                )
            )
        if claim.superseded_at and _in_window(claim.superseded_at, since, until):
            retracted = claim.status is ClaimStatus.RETRACTED
            changes.append(
                Change(
                    at=claim.superseded_at,
                    kind=ChangeKind.CLAIM_RETRACTED if retracted else ChangeKind.CLAIM_SUPERSEDED,
                    object_id=claim.id,
                    summary=claim.text,
                    reason=claim.change_reason,
                )
            )

    thesis_ids = None
    if entity_id is not None:
        thesis_ids = [t.id for t in store.theses(entity_id=entity_id)]
    for entry in store.thesis_changes(since=since, until=until, thesis_ids=thesis_ids):
        changes.append(_thesis_change(store, entry))

    for event in store.events(entity_id=entity_id):
        if _in_window(event.ingested_at, since, until):
            changes.append(
                Change(
                    at=event.ingested_at,
                    kind=ChangeKind.EVENT_ADDED,
                    object_id=event.id,
                    summary=f"{event.kind.value} {event.occurred_at:%Y-%m-%d}: {event.title}",
                )
            )

    if entity_id is None:
        for entity in store.entities_created(since=since, until=until):
            changes.append(
                Change(
                    at=entity.created_at,
                    kind=ChangeKind.ENTITY_ADDED,
                    object_id=entity.id,
                    summary=f"{entity.kind.value}: {entity.name}",
                )
            )

    order = list(ChangeKind)
    changes.sort(key=lambda c: (c.at, order.index(c.kind), c.object_id))
    return changes


def timeline(
    store: Backend,
    entity_id: str,
    since: datetime | None = None,
    until: datetime | None = None,
) -> ChangeLog:
    """Claims and thesis changes touching one entity, oldest first."""
    live = store.live_entity_id(entity_id)
    return ChangeLog(
        since=since, until=until, entity_id=live, changes=_changes(store, live, since, until)
    )


def what_changed(store: Backend, since: datetime, until: datetime | None = None) -> ChangeLog:
    """Every change across the graph within [since, until), oldest first."""
    return ChangeLog(
        since=since, until=until, entity_id=None, changes=_changes(store, None, since, until)
    )
