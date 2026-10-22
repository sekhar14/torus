"""Domain models returned by the store.

Records are immutable snapshots. Bi-temporal fields:

* ``valid_from`` / ``valid_to``      - when the statement is true in the world
* ``recorded_at`` / ``superseded_at`` - when Torus believed it (transaction time)
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class EntityKind(StrEnum):
    COMPANY = "company"
    SECURITY = "security"
    PERSON = "person"
    PRODUCT = "product"
    SECTOR = "sector"
    GEOGRAPHY = "geography"
    MACRO_INDICATOR = "macro_indicator"
    OTHER = "other"


class AliasType(StrEnum):
    TICKER = "ticker"
    NAME = "name"
    FORMER_NAME = "former_name"
    ABBREVIATION = "abbreviation"
    IDENTIFIER = "identifier"


class EvidenceKind(StrEnum):
    WEB = "web"
    FILING = "filing"
    TRANSCRIPT = "transcript"
    DATASET = "dataset"
    USER_NOTE = "user_note"


class ClaimType(StrEnum):
    FACT = "fact"
    METRIC = "metric"
    INFERENCE = "inference"
    FORECAST = "forecast"
    OPINION = "opinion"


# Claim types that must cite evidence (inference may cite derived claims instead).
EVIDENCED_CLAIM_TYPES = frozenset({ClaimType.FACT, ClaimType.METRIC})


class ClaimStatus(StrEnum):
    ACTIVE = "active"
    SUPERSEDED = "superseded"
    RETRACTED = "retracted"


class ClaimEntityRole(StrEnum):
    SUBJECT = "subject"
    OBJECT = "object"
    MENTIONED = "mentioned"


class Support(StrEnum):
    SUPPORTS = "supports"
    CONTRADICTS = "contradicts"


class RelationType(StrEnum):
    SUPPLIES = "SUPPLIES"
    CUSTOMER_OF = "CUSTOMER_OF"
    COMPETES_WITH = "COMPETES_WITH"
    SUBSIDIARY_OF = "SUBSIDIARY_OF"
    EXPOSED_TO = "EXPOSED_TO"
    EXECUTIVE_OF = "EXECUTIVE_OF"
    PRODUCES = "PRODUCES"
    IN_SECTOR = "IN_SECTOR"
    CORRELATED_WITH = "CORRELATED_WITH"


class Stance(StrEnum):
    BULLISH = "bullish"
    BEARISH = "bearish"
    NEUTRAL = "neutral"
    NA = "n/a"


class ThesisStatus(StrEnum):
    ACTIVE = "active"
    AT_RISK = "at_risk"
    BROKEN = "broken"
    CONFIRMED = "confirmed"
    ARCHIVED = "archived"


THESIS_TRANSITIONS: dict[ThesisStatus, frozenset[ThesisStatus]] = {
    ThesisStatus.ACTIVE: frozenset(
        {ThesisStatus.AT_RISK, ThesisStatus.BROKEN, ThesisStatus.CONFIRMED, ThesisStatus.ARCHIVED}
    ),
    ThesisStatus.AT_RISK: frozenset(
        {ThesisStatus.ACTIVE, ThesisStatus.BROKEN, ThesisStatus.CONFIRMED, ThesisStatus.ARCHIVED}
    ),
    ThesisStatus.BROKEN: frozenset({ThesisStatus.ARCHIVED}),
    ThesisStatus.CONFIRMED: frozenset({ThesisStatus.ARCHIVED}),
    ThesisStatus.ARCHIVED: frozenset(),
}


class ThesisClaimRole(StrEnum):
    ASSUMPTION = "assumption"
    SUPPORTING = "supporting"
    RISK = "risk"


Confidence = Field(ge=0.0, le=1.0)


class _Record(BaseModel):
    model_config = ConfigDict(frozen=True)


class Alias(_Record):
    alias: str
    alias_type: AliasType


class Entity(_Record):
    id: str
    kind: EntityKind
    name: str
    canonical_key: str | None
    attrs: dict[str, Any]
    aliases: tuple[Alias, ...]
    created_at: datetime
    updated_at: datetime


class Evidence(_Record):
    id: str
    kind: EvidenceKind
    uri: str
    title: str | None
    publisher: str | None
    published_at: datetime | None
    retrieved_at: datetime
    quote: str | None
    content_hash: str | None
    source_path: str | None
    recorded_at: datetime


class ClaimEntity(_Record):
    entity_id: str
    role: ClaimEntityRole


class ClaimEvidence(_Record):
    evidence_id: str
    support: Support


class Claim(_Record):
    id: str
    lineage_id: str
    supersedes_id: str | None
    text: str
    claim_type: ClaimType
    confidence: float = Confidence
    status: ClaimStatus
    valid_from: datetime | None
    valid_to: datetime | None
    recorded_at: datetime
    superseded_at: datetime | None
    change_reason: str | None
    session_id: str | None
    entities: tuple[ClaimEntity, ...]
    evidence: tuple[ClaimEvidence, ...]
    derived_from: tuple[str, ...]

    @property
    def is_current(self) -> bool:
        return self.superseded_at is None


class Relation(_Record):
    id: str
    lineage_id: str
    supersedes_id: str | None
    src_id: str
    rel: RelationType
    dst_id: str
    confidence: float = Confidence
    claim_id: str | None
    valid_from: datetime | None
    valid_to: datetime | None
    recorded_at: datetime
    superseded_at: datetime | None
    source: str = "agent"  # agent | curator | user | starter:<pack>@<version>


class ThesisClaim(_Record):
    """Link from a thesis to a claim lineage (so it follows claim corrections)."""

    claim_lineage_id: str
    role: ThesisClaimRole
    weight: float


class ThesisTrigger(_Record):
    id: str
    description: str
    entity_id: str | None


class Thesis(_Record):
    id: str
    title: str
    statement: str
    stance: Stance
    horizon: str | None
    confidence: float = Confidence
    status: ThesisStatus
    subject_entity_id: str | None
    session_id: str | None
    created_at: datetime
    updated_at: datetime
    claims: tuple[ThesisClaim, ...]
    triggers: tuple[ThesisTrigger, ...]
    alert_threshold: float | None = None  # None: the global setting
    horizon_date: datetime | None = None  # when the horizon ends, if understood
    instrument: str | None = None  # ticker the scorecard tracks
    entry_price: float | None = None  # None: the close on entry_at
    entry_at: datetime | None = None  # None: created_at
    benchmark: str | None = None  # None: the global benchmark (SPY)


class ThesisHistoryEntry(_Record):
    id: str
    thesis_id: str
    status: ThesisStatus
    confidence: float
    reason: str | None
    alert_id: str | None
    at: datetime


class RejectedHypothesis(_Record):
    id: str
    text: str
    reason: str
    entity_ids: tuple[str, ...]
    session_id: str | None
    recorded_at: datetime
    withdrawn_at: datetime | None = None


class Session(_Record):
    id: str
    harness: str
    external_session_id: str | None
    transcript_path: str | None
    started_at: datetime
    ended_at: datetime | None


class SearchHit(_Record):
    object_type: str
    object_id: str
    snippet: str
    rank: float


class SourceCandidate(_Record):
    id: str
    session_id: str
    tool: str
    uri: str | None
    query: str | None
    seen_at: datetime
    linked_evidence_id: str | None


class SessionActivity(_Record):
    """What a session touched: drives whether it needs distillation."""

    session_id: str
    entity_ids: tuple[str, ...]
    records: dict[str, int]  # object_type -> count recorded via Torus tools
    source_candidates: int

    @property
    def is_research(self) -> bool:
        return bool(self.entity_ids or self.records)

    @property
    def recorded_total(self) -> int:
        return sum(self.records.values())


class QueueStatus(StrEnum):
    PENDING = "pending"
    LEASED = "leased"
    DONE = "done"
    PARKED = "parked"


class QueueItem(_Record):
    id: str
    kind: str
    dedupe_key: str
    payload: dict[str, Any]
    status: QueueStatus
    attempts: int
    created_at: datetime
    updated_at: datetime
    lease_until: datetime | None


class EventKind(StrEnum):
    FILING = "filing"
    NEWS = "news"
    EARNINGS = "earnings"
    PRICE_MOVE = "price_move"
    MACRO = "macro"
    MANUAL = "manual"


class Event(_Record):
    id: str
    kind: EventKind
    title: str
    summary: str | None
    uri: str | None
    source: str
    occurred_at: datetime
    ingested_at: datetime
    content_hash: str
    payload: dict[str, Any]
    entity_ids: tuple[str, ...]
    materiality: float = 1.0
    materiality_reason: str | None = None


class AlertStatus(StrEnum):
    OPEN = "open"
    RESOLVED = "resolved"
    DISMISSED = "dismissed"


class Verdict(StrEnum):
    SUPPORTS = "supports"
    WEAKENS = "weakens"
    BREAKS = "breaks"
    UNRELATED = "unrelated"


class ImpactPath(_Record):
    """One reason an event reaches a thesis, e.g. TSMC -SUPPLIES-> NVIDIA, assumption claim."""

    route: str  # "TSMC" or "TSMC -SUPPLIES-> NVIDIA CORP"
    via: str  # subject | assumption claim | supporting claim | risk claim | trigger
    detail: str  # claim text, trigger description or thesis subject
    claim_id: str | None = None
    trigger_id: str | None = None
    contribution: float


class Alert(_Record):
    id: str
    event_id: str
    thesis_id: str
    score: float
    paths: tuple[ImpactPath, ...]
    status: AlertStatus
    verdict: Verdict | None
    rationale: str | None
    created_at: datetime
    resolved_at: datetime | None
    # score = graph_score * materiality * (1 + trigger_match); None before schema v8
    graph_score: float | None = None
    materiality: float | None = None
    trigger_match: float | None = None
    matched_trigger: str | None = None


class Outcome(StrEnum):
    RIGHT = "right"
    WRONG = "wrong"
    UNCLEAR = "unclear"


class ThesisOutcome(_Record):
    thesis_id: str
    outcome: Outcome
    method: str  # status | horizon | user
    decided_at: datetime
    stance_return: float | None
    note: str | None


class ThesisMute(_Record):
    """Suppresses alerts on a thesis for matching events (all set fields must match)."""

    id: str
    thesis_id: str
    entity_id: str | None
    event_kind: EventKind | None
    form: str | None
    until: datetime | None
    note: str | None
    created_at: datetime
