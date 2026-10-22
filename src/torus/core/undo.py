"""See and revert what a research session recorded. Nothing is deleted: undo
retracts, archives, retires or withdraws, so history still shows what happened.

==============  ==========================================================
claim           retracted; a correction (supersede) goes back to the
                version it replaced
thesis          archived, if the session created it (a thesis the session
                only revised is left for ``update_thesis``)
relation        retired; a link that replaced an earlier version (such as a
                confirmed starter link) goes back to that version
hypothesis      withdrawn (no longer recalled)
evidence        kept: a source on its own changes nothing
==============  ==========================================================
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime

from pydantic import BaseModel

from torus.core.errors import NotFoundError, TorusError
from torus.core.models import ThesisStatus
from torus.core.store.base import Backend

UNDO_REASON = "undone by the user"


class RecordState(BaseModel):
    type: str
    id: str
    text: str
    state: str
    undoable: bool


class SessionRecords(BaseModel):
    session_id: str
    last_recorded_at: datetime
    records: list[RecordState]


class UndoOutcome(BaseModel):
    type: str
    id: str
    done: bool
    result: str


def describe(store: Backend, object_type: str, object_id: str, session_id: str) -> RecordState:
    names = {eid: name for eid, (_, name) in store.entity_index().items()}

    def state(text: str, now: str, undoable: bool) -> RecordState:
        return RecordState(type=object_type, id=object_id, text=text, state=now, undoable=undoable)

    try:
        if object_type == "claim":
            claim = store.get_claim(object_id)
            current = store.current_claim(claim.lineage_id)
            if current is None:
                return state(claim.text, "retracted", False)
            if current.id != claim.id:
                return state(claim.text, "changed since", False)
            kind = "correction" if claim.supersedes_id else "current"
            return state(claim.text, kind, True)
        if object_type == "thesis":
            thesis = store.get_thesis(object_id)
            created_here = thesis.session_id == session_id
            if thesis.status is ThesisStatus.ARCHIVED:
                return state(thesis.title, "archived", False)
            return state(thesis.title, "created" if created_here else "revised", created_here)
        if object_type == "relation":
            rel = store.get_relation(object_id)
            src, dst = names.get(rel.src_id, rel.src_id), names.get(rel.dst_id, rel.dst_id)
            text = f"{src} {rel.rel.value} {dst}"
            if rel.superseded_at is not None:
                return state(text, "changed since", False)
            return state(text, "current", True)
        if object_type == "hypothesis":
            [hyp] = store.rejected_hypotheses(hypothesis_id=object_id)
            withdrawn = hyp.withdrawn_at is not None
            return state(hyp.text, "withdrawn" if withdrawn else "current", not withdrawn)
        if object_type == "evidence":
            ev = store.get_evidence(object_id)
            return state(ev.title or ev.uri, "kept", False)
    except (NotFoundError, ValueError):
        pass
    return state("(not found)", "unknown", False)


def recent(store: Backend, sessions: int = 1) -> list[SessionRecords]:
    """What the most recently active sessions recorded, newest session first."""
    result = []
    for session_id, last_at in store.recent_record_sessions(sessions):
        records = [describe(store, t, i, session_id) for t, i in store.session_records(session_id)]
        result.append(
            SessionRecords(session_id=session_id, last_recorded_at=last_at, records=records)
        )
    return result


def undo(
    store: Backend,
    items: Sequence[tuple[str, str]],
    reason: str = UNDO_REASON,
    session_id: str | None = None,
) -> list[UndoOutcome]:
    """Revert each (type, id); one failing item doesn't stop the others.

    A thesis is archived only if ``session_id`` created it."""
    outcomes = []
    for object_type, object_id in items:
        try:
            with store.transaction():
                outcomes.append(_undo_one(store, object_type, object_id, reason, session_id))
        except (TorusError, ValueError) as exc:
            outcomes.append(
                UndoOutcome(type=object_type, id=object_id, done=False, result=str(exc))
            )
    return outcomes


def _undo_one(
    store: Backend, object_type: str, object_id: str, reason: str, session_id: str | None
) -> UndoOutcome:
    def out(done: bool, result: str) -> UndoOutcome:
        return UndoOutcome(type=object_type, id=object_id, done=done, result=result)

    if object_type == "claim":
        claim = store.get_claim(object_id)
        current = store.current_claim(claim.lineage_id)
        if current is None or current.id != claim.id:
            return out(False, "the claim has changed since; nothing undone")
        if claim.supersedes_id is None:
            store.retract_claim(claim.id, reason)
            return out(True, "retracted")
        old = store.get_claim(claim.supersedes_id)
        restored = store.supersede_claim(
            claim.id,
            reason,
            text=old.text,
            claim_type=old.claim_type,
            confidence=old.confidence,
            entities=list(old.entities),
            evidence=list(old.evidence),
            derived_from=list(old.derived_from),
        )
        return out(True, f"restored the earlier version ({restored.id})")
    if object_type == "thesis":
        thesis = store.get_thesis(object_id)
        if thesis.status is ThesisStatus.ARCHIVED:
            return out(False, "already archived")
        if session_id is None or thesis.session_id != session_id:
            return out(False, "not created in this session; revise it with update_thesis")
        store.update_thesis(thesis.id, reason, status=ThesisStatus.ARCHIVED)
        return out(True, "archived")
    if object_type == "relation":
        restored_rel = store.retire_relation(object_id, reason)
        if restored_rel is None:
            return out(True, "link removed")
        return out(True, f"restored the earlier link ({restored_rel.source})")
    if object_type == "hypothesis":
        if store.withdraw_hypothesis(object_id, reason):
            return out(True, "withdrawn")
        return out(False, "already withdrawn")
    if object_type == "evidence":
        return out(False, "sources are kept; on their own they change nothing")
    return out(False, f"unknown record type {object_type!r}")
