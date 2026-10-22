"""The storage contract every backend implements.

SQLite is the default ("lite"); a server backend (Postgres / Neo4j via Graphiti)
can be added later for teams without changing the MCP server, hooks or watchers,
which depend only on this protocol.
"""

from __future__ import annotations

from collections.abc import Sequence
from contextlib import AbstractContextManager
from datetime import date, datetime
from typing import Any, Protocol

from torus.core.models import (
    Alert,
    AlertStatus,
    Alias,
    AliasType,
    Claim,
    ClaimEntity,
    ClaimEvidence,
    ClaimType,
    Entity,
    EntityKind,
    Event,
    EventKind,
    Evidence,
    EvidenceKind,
    ImpactPath,
    QueueItem,
    QueueStatus,
    RejectedHypothesis,
    Relation,
    RelationType,
    SearchHit,
    Session,
    SessionActivity,
    SourceCandidate,
    Stance,
    Thesis,
    ThesisClaim,
    ThesisClaimRole,
    ThesisHistoryEntry,
    ThesisMute,
    ThesisOutcome,
    ThesisStatus,
    ThesisTrigger,
    Verdict,
)


class Backend(Protocol):
    def transaction(self) -> AbstractContextManager[None]: ...
    def schema_version(self) -> int: ...
    def counts(self) -> dict[str, int]: ...
    def close(self) -> None: ...

    # sessions
    def start_session(
        self,
        harness: str,
        external_session_id: str | None = ...,
        transcript_path: str | None = ...,
    ) -> Session: ...
    def end_session(self, session_id: str) -> Session: ...
    def get_session(self, session_id: str) -> Session: ...

    # entities
    def upsert_entity(
        self,
        kind: EntityKind,
        name: str,
        canonical_key: str | None = ...,
        aliases: Sequence[Alias] = ...,
        attrs: dict[str, Any] | None = ...,
    ) -> Entity: ...
    def add_alias(self, entity_id: str, alias: str, alias_type: AliasType) -> Entity: ...
    def get_entity(self, entity_id: str) -> Entity: ...
    def get_entity_by_key(self, canonical_key: str) -> Entity | None: ...
    def live_entity_id(self, entity_id: str) -> str: ...
    def find_entities_by_alias(
        self, alias: str, alias_types: Sequence[AliasType] = ...
    ) -> list[Entity]: ...
    def find_entities_by_name_key(
        self, name: str, alias_types: Sequence[AliasType] = ...
    ) -> list[Entity]: ...
    def all_aliases(self) -> list[tuple[str, str, AliasType]]: ...
    def entity_index(self) -> dict[str, tuple[str | None, str]]: ...
    def merge_entities(self, keep_id: str, merge_id: str, reason: str) -> Entity: ...

    # evidence
    def record_evidence(
        self,
        uri: str,
        kind: EvidenceKind = ...,
        title: str | None = ...,
        publisher: str | None = ...,
        published_at: datetime | None = ...,
        retrieved_at: datetime | None = ...,
        quote: str | None = ...,
        content_hash: str | None = ...,
        source_path: str | None = ...,
    ) -> Evidence: ...
    def get_evidence(self, evidence_id: str) -> Evidence: ...

    # claims
    def record_claim(
        self,
        text: str,
        claim_type: ClaimType,
        confidence: float,
        entities: Sequence[str | ClaimEntity] = ...,
        evidence: Sequence[str | ClaimEvidence] = ...,
        derived_from: Sequence[str] = ...,
        valid_from: datetime | None = ...,
        valid_to: datetime | None = ...,
        session_id: str | None = ...,
    ) -> Claim: ...
    def supersede_claim(
        self,
        claim_id: str,
        reason: str,
        text: str | None = ...,
        claim_type: ClaimType | None = ...,
        confidence: float | None = ...,
        entities: Sequence[str | ClaimEntity] | None = ...,
        evidence: Sequence[str | ClaimEvidence] | None = ...,
        derived_from: Sequence[str] | None = ...,
        *,
        session_id: str | None = ...,
    ) -> Claim: ...
    def retract_claim(self, claim_id: str, reason: str) -> Claim: ...
    def get_claim(self, claim_id: str) -> Claim: ...
    def current_claim(self, lineage_id: str, as_of: datetime | None = ...) -> Claim | None: ...
    def claim_history(self, lineage_id: str) -> list[Claim]: ...
    def get_claims(self, claim_ids: Sequence[str]) -> list[Claim]: ...
    def claim_versions(
        self,
        entity_id: str | None = ...,
        since: datetime | None = ...,
        until: datetime | None = ...,
    ) -> list[Claim]: ...
    def claims(
        self,
        entity_id: str | None = ...,
        as_of: datetime | None = ...,
        valid_at: datetime | None = ...,
        claim_types: Sequence[ClaimType] = ...,
        limit: int | None = ...,
    ) -> list[Claim]: ...

    # relations
    def record_relation(
        self,
        src_id: str,
        rel: RelationType,
        dst_id: str,
        confidence: float,
        claim_id: str | None = ...,
        valid_from: datetime | None = ...,
        valid_to: datetime | None = ...,
        source: str = ...,
    ) -> Relation: ...
    def retire_relation(self, relation_id: str, reason: str) -> Relation | None: ...
    def get_relation(self, relation_id: str) -> Relation: ...
    def relations(
        self,
        entity_id: str | None = ...,
        direction: str = ...,
        rel: RelationType | None = ...,
        as_of: datetime | None = ...,
        valid_at: datetime | None = ...,
    ) -> list[Relation]: ...

    # theses
    def record_thesis(
        self,
        title: str,
        statement: str,
        stance: Stance,
        confidence: float,
        horizon: str | None = ...,
        subject_entity_id: str | None = ...,
        assumptions: Sequence[str] = ...,
        supporting: Sequence[str] = ...,
        risks: Sequence[str] = ...,
        triggers: Sequence[tuple[str, str | None]] = ...,
        session_id: str | None = ...,
        horizon_date: datetime | None = ...,
    ) -> Thesis: ...
    def link_thesis_claim(
        self, thesis_id: str, claim_id: str, role: ThesisClaimRole, weight: float = ...
    ) -> Thesis: ...
    def update_thesis(
        self,
        thesis_id: str,
        reason: str,
        status: ThesisStatus | None = ...,
        confidence: float | None = ...,
        alert_id: str | None = ...,
    ) -> Thesis: ...
    def revise_thesis(
        self,
        thesis_id: str,
        reason: str,
        status: ThesisStatus | None = ...,
        confidence: float | None = ...,
        add_claims: Sequence[tuple[str, ThesisClaimRole, float]] = ...,
        remove_claims: Sequence[str] = ...,
        add_triggers: Sequence[tuple[str, str | None]] = ...,
        remove_triggers: Sequence[str] = ...,
        alert_id: str | None = ...,
    ) -> Thesis: ...
    def thesis_links_as_of(self, thesis_id: str, as_of: datetime) -> list[ThesisClaim]: ...
    def get_thesis(self, thesis_id: str) -> Thesis: ...
    def theses(
        self, entity_id: str | None = ..., statuses: Sequence[ThesisStatus] = ...
    ) -> list[Thesis]: ...
    def thesis_history(self, thesis_id: str) -> list[ThesisHistoryEntry]: ...
    def thesis_changes(
        self,
        since: datetime | None = ...,
        until: datetime | None = ...,
        thesis_ids: Sequence[str] | None = ...,
    ) -> list[ThesisHistoryEntry]: ...
    def entities_created(
        self, since: datetime | None = ..., until: datetime | None = ...
    ) -> list[Entity]: ...
    def thesis_state_as_of(self, thesis_id: str, as_of: datetime) -> ThesisHistoryEntry | None: ...

    # rejected hypotheses
    def record_rejected_hypothesis(
        self,
        text: str,
        reason: str,
        entity_ids: Sequence[str] = ...,
        session_id: str | None = ...,
    ) -> RejectedHypothesis: ...
    def withdraw_hypothesis(self, hypothesis_id: str, reason: str) -> bool: ...
    def rejected_hypotheses(
        self, entity_id: str | None = ..., hypothesis_id: str | None = ...
    ) -> list[RejectedHypothesis]: ...

    # search
    def search(
        self,
        query: str,
        object_types: Sequence[str] = ...,
        limit: int = ...,
        match_any: bool = ...,
    ) -> list[SearchHit]: ...

    # events & alerts
    def add_event(
        self,
        kind: EventKind,
        title: str,
        occurred_at: datetime,
        entity_ids: Sequence[str] = ...,
        source: str = ...,
        uri: str | None = ...,
        summary: str | None = ...,
        payload: dict[str, Any] | None = ...,
        materiality: float = ...,
        materiality_reason: str | None = ...,
    ) -> tuple[Event, bool]: ...
    def get_event(self, event_id: str) -> Event: ...
    def events(
        self,
        entity_id: str | None = ...,
        since: datetime | None = ...,
        limit: int | None = ...,
    ) -> list[Event]: ...
    def thesis_triggers_for_entity(self, entity_id: str) -> list[tuple[str, ThesisTrigger]]: ...
    def upsert_alert(
        self,
        event_id: str,
        thesis_id: str,
        score: float,
        paths: Sequence[ImpactPath],
        *,
        graph_score: float | None = ...,
        materiality: float | None = ...,
        trigger_match: float | None = ...,
        matched_trigger: str | None = ...,
    ) -> tuple[Alert, bool]: ...
    def get_alert(self, alert_id: str) -> Alert: ...
    def alerts(
        self,
        statuses: Sequence[AlertStatus] = ...,
        thesis_ids: Sequence[str] | None = ...,
        event_id: str | None = ...,
    ) -> list[Alert]: ...
    def resolve_alert(
        self,
        alert_id: str,
        verdict: Verdict,
        rationale: str,
        confidence: float | None = ...,
        status: ThesisStatus | None = ...,
    ) -> Alert: ...

    def set_thesis_horizon(
        self, thesis_id: str, horizon: str | None, horizon_date: datetime | None = ...
    ) -> Thesis: ...
    def set_thesis_tracking(
        self,
        thesis_id: str,
        instrument: str | None,
        entry_price: float | None = ...,
        entry_at: datetime | None = ...,
        benchmark: str | None = ...,
    ) -> Thesis: ...
    def set_thesis_outcome(
        self,
        thesis_id: str,
        outcome: str,
        method: str,
        stance_return: float | None = ...,
        note: str | None = ...,
    ) -> ThesisOutcome: ...
    def clear_thesis_outcome(self, thesis_id: str) -> bool: ...
    def thesis_outcomes(
        self, thesis_ids: Sequence[str] | None = ...
    ) -> dict[str, ThesisOutcome]: ...
    def upsert_prices(
        self, symbol: str, rows: Sequence[tuple[date, float]], source: str
    ) -> int: ...
    def prices(
        self, symbol: str, start: date | None = ..., end: date | None = ...
    ) -> list[tuple[date, float]]: ...
    def set_alert_threshold(self, thesis_id: str, threshold: float | None) -> Thesis: ...
    def add_thesis_mute(
        self,
        thesis_id: str,
        entity_id: str | None = ...,
        event_kind: EventKind | None = ...,
        form: str | None = ...,
        until: datetime | None = ...,
        note: str | None = ...,
    ) -> ThesisMute: ...
    def remove_thesis_mute(self, mute_id: str) -> bool: ...
    def thesis_mutes(self, thesis_id: str | None = ...) -> list[ThesisMute]: ...

    # watchers
    def watch_entity(self, entity_id: str, note: str | None = ...) -> Entity: ...
    def unwatch_entity(self, entity_id: str) -> bool: ...
    def watchlist(self) -> list[Entity]: ...
    def watch_targets(self) -> list[Entity]: ...
    def add_feed(self, url: str, name: str | None = ...) -> dict[str, str | None]: ...
    def remove_feed(self, url: str) -> bool: ...
    def feeds(self) -> list[dict[str, str | None]]: ...
    def get_state(self, key: str) -> dict[str, Any] | None: ...
    def set_state(self, key: str, value: dict[str, Any]) -> None: ...
    def get_setting(self, key: str) -> str | None: ...
    def set_setting(self, key: str, value: str | None) -> None: ...

    # session capture & queue
    def add_source_candidate(
        self,
        session_id: str,
        tool: str,
        uri: str | None = ...,
        query: str | None = ...,
    ) -> SourceCandidate: ...
    def source_candidates(self, session_id: str) -> list[SourceCandidate]: ...
    def note_session_entities(self, session_id: str, entity_ids: Sequence[str]) -> None: ...
    def note_session_record(self, session_id: str, object_type: str, object_id: str) -> None: ...
    def adopt_into_session(self, session_id: str, object_type: str, object_id: str) -> str: ...
    def recent_record_sessions(self, limit: int = ...) -> list[tuple[str, datetime]]: ...
    def session_records(self, session_id: str) -> list[tuple[str, str]]: ...
    def session_activity(self, session_id: str) -> SessionActivity: ...
    def enqueue(self, kind: str, dedupe_key: str, payload: dict[str, Any]) -> QueueItem: ...
    def get_queue_item(self, item_id: str) -> QueueItem: ...
    def lease_work(
        self, kind: str, limit: int = ..., lease_seconds: int = ...
    ) -> list[QueueItem]: ...
    def complete_work(self, item_id: str, result: dict[str, Any]) -> QueueItem: ...
    def release_work(self, item_id: str, error: str) -> QueueItem: ...
    def queue_counts(self) -> dict[str, dict[str, int]]: ...
    def queue_items(
        self, kind: str | None = ..., statuses: Sequence[QueueStatus] = ...
    ) -> list[QueueItem]: ...
