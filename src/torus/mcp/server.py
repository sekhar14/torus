"""MCP server exposing the Torus graph to agents (stdio transport).

Entities can be passed as tickers, CIKs, names, canonical keys or ids; companies
known only from the SEC reference list are added on first use. Domain errors come
back as tool errors with a hint, never as crashes.

Watchers (``torus watch run``) ingest filings and feed items as events.
"""

from __future__ import annotations

import functools
import os
import threading
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Annotated, Literal, ParamSpec, TypeVar

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations
from pydantic import BaseModel, Field

from torus import __version__
from torus.config import CaptureMode, capture_mode
from torus.core import graph, transcript
from torus.core.changes import ChangeLog, timeline, what_changed
from torus.core.clock import Clock, SystemClock
from torus.core.detect import EntityDetector
from torus.core.errors import NotFoundError, TorusError
from torus.core.graph import PathResult, Subgraph
from torus.core.impact import ImpactHit, ingest_event
from torus.core.impact import impact as impact_of
from torus.core.models import (
    Alert,
    AlertStatus,
    Alias,
    AliasType,
    Claim,
    ClaimEntity,
    ClaimEntityRole,
    ClaimEvidence,
    ClaimType,
    Entity,
    EntityKind,
    Event,
    EventKind,
    Evidence,
    EvidenceKind,
    QueueStatus,
    RejectedHypothesis,
    Relation,
    RelationType,
    SearchHit,
    Stance,
    Support,
    Thesis,
    ThesisClaimRole,
    ThesisHistoryEntry,
    ThesisStatus,
    Verdict,
)
from torus.core.recall import RecallResult, recall, render_context
from torus.core.reference import ReferenceData
from torus.core.resolve import ResolutionStatus, Resolver
from torus.core.store.base import Backend
from torus.protocol import instructions

ENV_HARNESS = "TORUS_HARNESS"
ENV_SESSION = "TORUS_SESSION_ID"
DISTILL_KIND = "distill_session"  # same kind the hooks enqueue

READ_ONLY = ToolAnnotations(readOnlyHint=True, openWorldHint=False)
WRITE = ToolAnnotations(readOnlyHint=False, destructiveHint=False, openWorldHint=False)

P = ParamSpec("P")
R = TypeVar("R")

EntityRef = Annotated[str, Field(description="Ticker, CIK, company name, canonical key or id")]
Confidence = Annotated[float, Field(ge=0.0, le=1.0, description="0 = guess, 1 = certain")]


class RelationView(BaseModel):
    id: str
    rel: RelationType
    direction: Literal["out", "in"]
    other_id: str
    other_name: str
    confidence: float
    valid_from: datetime | None
    valid_to: datetime | None


class EntityView(BaseModel):
    entity: Entity
    resolved_by: str
    relations: list[RelationView]


class TriggerIn(BaseModel):
    description: str = Field(description="What would invalidate the thesis")
    entity: str | None = Field(default=None, description="Entity the trigger watches")


class ThesisClaimIn(BaseModel):
    claim_id: str
    role: ThesisClaimRole = ThesisClaimRole.ASSUMPTION
    weight: float = Field(default=1.0, ge=0.0, le=1.0)


class AlertView(BaseModel):
    alert: Alert
    event: Event
    thesis_title: str
    thesis_status: ThesisStatus
    thesis_confidence: float


class EventResult(BaseModel):
    event: Event
    created: bool = Field(description="False if this event was already recorded")
    alerts: list[AlertView]


class ImpactResult(BaseModel):
    hits: list[ImpactHit]


class AlertList(BaseModel):
    alerts: list[AlertView]


def _alert_view(store: Backend, alert: Alert) -> AlertView:
    thesis = store.get_thesis(alert.thesis_id)
    return AlertView(
        alert=alert,
        event=store.get_event(alert.event_id),
        thesis_title=thesis.title,
        thesis_status=thesis.status,
        thesis_confidence=thesis.confidence,
    )


class WatchedEntity(BaseModel):
    id: str
    name: str
    canonical_key: str | None
    reason: Literal["watchlist", "open thesis"]


class WatchList(BaseModel):
    entities: list[WatchedEntity]
    feeds: list[str]


def _watch_list(store: Backend) -> WatchList:
    explicit = {e.id for e in store.watchlist()}
    return WatchList(
        entities=[
            WatchedEntity(
                id=e.id,
                name=e.name,
                canonical_key=e.canonical_key,
                reason="watchlist" if e.id in explicit else "open thesis",
            )
            for e in store.watch_targets()
        ],
        feeds=[str(f["url"]) for f in store.feeds()],
    )


class RecordedRef(BaseModel):
    type: str
    id: str
    text: str


class ThesisRef(BaseModel):
    id: str
    title: str
    status: ThesisStatus
    confidence: float


class SourceRef(BaseModel):
    tool: str
    uri: str | None
    query: str | None
    evidence_id: str | None = Field(description="Set when already recorded as evidence")


class DistillationJob(BaseModel):
    queue_id: str
    session_id: str
    attempt: int
    capture_mode: str
    entities: list[str]
    already_recorded: list[RecordedRef]
    theses: list[ThesisRef]
    sources: list[SourceRef]
    transcript: str


class NextDistillation(BaseModel):
    job: DistillationJob | None
    remaining: int = Field(description="Jobs still waiting after this one")


class DistillationOutcome(BaseModel):
    queue_id: str
    status: QueueStatus
    remaining: int


def _recorded_ref(
    store: Backend, object_type: str, object_id: str, names: dict[str, str]
) -> RecordedRef | None:
    """Short description of something the agent recorded, for de-duplication."""
    try:
        if object_type == "claim":
            claim = store.get_claim(object_id)
            current = store.current_claim(claim.lineage_id)
            text = current.text if current else f"(retracted) {claim.text}"
            return RecordedRef(type="claim", id=current.id if current else claim.id, text=text)
        if object_type == "thesis":
            return RecordedRef(type="thesis", id=object_id, text=store.get_thesis(object_id).title)
        if object_type == "evidence":
            ev = store.get_evidence(object_id)
            return RecordedRef(type="evidence", id=object_id, text=ev.title or ev.uri)
        if object_type == "relation":
            r = store.get_relation(object_id)
            text = f"{names.get(r.src_id, r.src_id)} {r.rel.value} {names.get(r.dst_id, r.dst_id)}"
            return RecordedRef(type="relation", id=object_id, text=text)
        if object_type == "hypothesis":
            hyps = store.rejected_hypotheses(hypothesis_id=object_id)
            return RecordedRef(type="hypothesis", id=object_id, text=hyps[0].text) if hyps else None
    except NotFoundError:
        return None
    return None


class SearchResults(BaseModel):
    hits: list[SearchHit]


class ThesisView(BaseModel):
    thesis: Thesis
    history: list[ThesisHistoryEntry]


def _aware(value: datetime | None) -> datetime | None:
    """Treat naive datetimes from agents as UTC."""
    if value is None or value.tzinfo is not None:
        return value
    return value.replace(tzinfo=UTC)


class TorusTools:
    """State shared by the tool functions: store, resolver, detector, session."""

    def __init__(
        self,
        store: Backend,
        reference: ReferenceData | None = None,
        clock: Clock | None = None,
        harness: str | None = None,
        external_session_id: str | None = None,
        mode: CaptureMode | None = None,
    ) -> None:
        self.store = store
        self.mode = mode or capture_mode()
        self.clock = clock or SystemClock()
        self.resolver = Resolver(store, reference)
        self.detector = EntityDetector(store, reference)
        self.harness = harness or os.environ.get(ENV_HARNESS, "mcp")
        self.external_session_id = external_session_id or os.environ.get(ENV_SESSION) or None
        self.lock = threading.RLock()
        self._session_id: str | None = None
        self.job_session_id: str | None = None

    def session_id(self) -> str:
        """The session new records are attributed to.

        While a distillation job is leased, that is the session being distilled
        (the records come from its transcript). Otherwise it is this server
        process's own session, created on first write.
        """
        if self.job_session_id is not None:
            return self.job_session_id
        if self._session_id is None:
            session = self.store.start_session(self.harness, self.external_session_id)
            self._session_id = session.id
        return self._session_id

    def entity(self, ref: str) -> Entity:
        result = self.resolver.resolve(ref, create=True)
        if result.status is ResolutionStatus.RESOLVED and result.entity is not None:
            if result.method.endswith("+created"):
                self.detector.refresh()
            return result.entity
        if result.status is ResolutionStatus.AMBIGUOUS:
            options = [f"{c.name} ({c.id})" for c in result.candidates] + [
                f"{c.title} (cik:{c.cik})" for c in result.reference_candidates
            ]
            raise ToolError(f"'{ref}' is ambiguous: {'; '.join(options)}. Pass an id instead.")
        raise ToolError(
            f"Unknown entity '{ref}'. Create it with upsert_entity(name=..., kind=...) first."
        )

    def guard(self, fn: Callable[P, R]) -> Callable[P, R]:
        """Serialise store access and turn domain errors into tool errors."""

        @functools.wraps(fn)
        def wrapper(*args: P.args, **kwargs: P.kwargs) -> R:
            with self.lock:
                try:
                    return fn(*args, **kwargs)
                except ToolError:
                    raise
                except NotFoundError as exc:
                    raise ToolError(f"{exc}. Use recall or search to find valid ids.") from exc
                except (TorusError, ValueError) as exc:
                    raise ToolError(str(exc)) from exc

        return wrapper


def build_server(tools: TorusTools) -> MCPServer:
    server = MCPServer(
        "torus",
        title="Torus research memory",
        instructions=instructions(tools.mode),
        version=__version__,
    )
    store = tools.store
    guard = tools.guard

    # -- read ---------------------------------------------------------------

    @server.tool(name="recall", annotations=READ_ONLY)
    @guard
    def recall_tool(
        query: Annotated[
            str | None, Field(description="Free-text question; entities in it are detected")
        ] = None,
        entities: Annotated[list[str], Field(description="Entity refs to focus on")] = [],  # noqa: B006
        as_of: Annotated[
            datetime | None, Field(description="Show what was believed at this time")
        ] = None,
        limit: Annotated[int, Field(ge=1, le=100)] = 20,
    ) -> RecallResult:
        """Prior theses, claims and rejected hypotheses about entities and/or a question.

        Call this before starting research so you build on earlier work."""
        ids = [tools.entity(ref).id for ref in entities]
        if query:
            ids += tools.detector.detect(query).entity_ids
        return recall(
            store, tools.clock, entity_ids=ids, query=query, as_of=_aware(as_of), limit=limit
        )

    @server.tool(annotations=READ_ONLY)
    @guard
    def get_entity(entity: EntityRef) -> EntityView:
        """Resolve an entity and list its current relations."""
        result = tools.resolver.resolve(entity, create=False)
        found = tools.entity(entity)
        names = {eid: name for eid, (_, name) in store.entity_index().items()}
        relations = []
        for r in store.relations(found.id):
            outgoing = r.src_id == found.id
            other = r.dst_id if outgoing else r.src_id
            relations.append(
                RelationView(
                    id=r.id,
                    rel=r.rel,
                    direction="out" if outgoing else "in",
                    other_id=other,
                    other_name=names.get(other, other),
                    confidence=r.confidence,
                    valid_from=r.valid_from,
                    valid_to=r.valid_to,
                )
            )
        return EntityView(entity=found, resolved_by=result.method or "id", relations=relations)

    @server.tool(annotations=READ_ONLY)
    @guard
    def search(
        query: str,
        types: Annotated[
            list[Literal["claim", "evidence", "thesis"]], Field(description="Object types")
        ] = ["claim", "evidence", "thesis"],  # noqa: B006
        limit: Annotated[int, Field(ge=1, le=100)] = 20,
    ) -> SearchResults:
        """Keyword search over current claims, evidence and theses."""
        return SearchResults(hits=store.search(query, object_types=types, limit=limit))

    @server.tool(name="timeline", annotations=READ_ONLY)
    @guard
    def timeline_tool(
        entity: EntityRef,
        since: datetime | None = None,
        until: datetime | None = None,
    ) -> ChangeLog:
        """Chronological claims and thesis changes for one entity."""
        return timeline(store, tools.entity(entity).id, _aware(since), _aware(until))

    @server.tool(name="what_changed", annotations=READ_ONLY)
    @guard
    def what_changed_tool(
        since: Annotated[datetime, Field(description="Start of the window (UTC if no zone)")],
        until: datetime | None = None,
    ) -> ChangeLog:
        """Everything recorded or revised across the graph since a point in time."""
        start = _aware(since)
        assert start is not None
        return what_changed(store, start, _aware(until))

    # -- write --------------------------------------------------------------

    @server.tool(annotations=WRITE)
    @guard
    def upsert_entity(
        name: str,
        kind: EntityKind = EntityKind.COMPANY,
        canonical_key: Annotated[
            str | None, Field(description="Stable id such as cik:0001045810; merges on match")
        ] = None,
        tickers: list[str] = [],  # noqa: B006
        aliases: Annotated[list[str], Field(description="Other names, abbreviations")] = [],  # noqa: B006
    ) -> Entity:
        """Create an entity (or merge into the one with the same canonical_key)."""
        entity = store.upsert_entity(
            kind,
            name,
            canonical_key=canonical_key,
            aliases=[Alias(alias=t, alias_type=AliasType.TICKER) for t in tickers]
            + [Alias(alias=a, alias_type=AliasType.ABBREVIATION) for a in aliases],
        )
        tools.detector.refresh()
        return entity

    @server.tool(annotations=WRITE)
    @guard
    def merge_entities(keep: EntityRef, duplicate: EntityRef, reason: str) -> Entity:
        """Fold a duplicate entity into the one to keep; its links move over."""
        merged = store.merge_entities(tools.entity(keep).id, tools.entity(duplicate).id, reason)
        tools.detector.refresh()
        return merged

    @server.tool(annotations=WRITE)
    @guard
    def record_evidence(
        uri: Annotated[str, Field(description="URL or document path")],
        title: str | None = None,
        quote: Annotated[
            str | None, Field(description="Short verbatim excerpt supporting claims")
        ] = None,
        kind: EvidenceKind = EvidenceKind.WEB,
        publisher: str | None = None,
        published_at: datetime | None = None,
    ) -> Evidence:
        """Record a source. Re-recording the same URI returns the existing record."""
        return store.record_evidence(
            uri,
            kind=kind,
            title=title,
            publisher=publisher,
            published_at=_aware(published_at),
            quote=quote,
        )

    @server.tool(annotations=WRITE)
    @guard
    def record_claim(
        text: Annotated[str, Field(description="One atomic, self-contained statement")],
        claim_type: ClaimType,
        confidence: Confidence,
        entities: Annotated[list[str], Field(description="Entities the claim is about")] = [],  # noqa: B006
        mentioned: Annotated[
            list[str], Field(description="Entities only mentioned in passing")
        ] = [],  # noqa: B006
        evidence_ids: list[str] = [],  # noqa: B006
        contradicting_evidence_ids: list[str] = [],  # noqa: B006
        derived_from: Annotated[
            list[str], Field(description="Claim ids an inference builds on")
        ] = [],  # noqa: B006
        valid_from: datetime | None = None,
        valid_to: datetime | None = None,
    ) -> Claim:
        """Record a finding. Facts and metrics need evidence_ids."""
        links = [
            ClaimEntity(entity_id=tools.entity(e).id, role=ClaimEntityRole.SUBJECT)
            for e in entities
        ]
        links += [
            ClaimEntity(entity_id=tools.entity(e).id, role=ClaimEntityRole.MENTIONED)
            for e in mentioned
        ]
        support = [ClaimEvidence(evidence_id=e, support=Support.SUPPORTS) for e in evidence_ids]
        support += [
            ClaimEvidence(evidence_id=e, support=Support.CONTRADICTS)
            for e in contradicting_evidence_ids
        ]
        return store.record_claim(
            text,
            claim_type,
            confidence,
            entities=links,
            evidence=support,
            derived_from=derived_from,
            valid_from=_aware(valid_from),
            valid_to=_aware(valid_to),
            session_id=tools.session_id(),
        )

    @server.tool(annotations=WRITE)
    @guard
    def supersede_claim(
        claim_id: str,
        reason: str,
        text: str | None = None,
        confidence: Annotated[float | None, Field(ge=0.0, le=1.0)] = None,
        evidence_ids: Annotated[
            list[str] | None, Field(description="Replaces the evidence list if given")
        ] = None,
    ) -> Claim:
        """Correct a claim: the old version stays in history, the new one is current."""
        return store.supersede_claim(
            claim_id,
            reason,
            text=text,
            confidence=confidence,
            evidence=evidence_ids,
            session_id=tools.session_id(),
        )

    @server.tool(annotations=WRITE)
    @guard
    def retract_claim(claim_id: str, reason: str) -> Claim:
        """Withdraw a claim that turned out to be wrong (kept in history)."""
        return store.retract_claim(claim_id, reason)

    @server.tool(annotations=WRITE)
    @guard
    def record_relation(
        source: EntityRef,
        rel: RelationType,
        target: EntityRef,
        confidence: Confidence,
        claim_id: Annotated[str | None, Field(description="Claim that backs this link")] = None,
        valid_from: datetime | None = None,
        valid_to: datetime | None = None,
    ) -> Relation:
        """Link two entities, e.g. TSMC SUPPLIES NVIDIA. Re-recording an existing
        link updates it (confidence, backing claim, dates) instead of duplicating it."""
        return store.record_relation(
            tools.entity(source).id,
            rel,
            tools.entity(target).id,
            confidence,
            claim_id=claim_id,
            valid_from=_aware(valid_from),
            valid_to=_aware(valid_to),
            # While a distillation job is leased, the caller is the curator.
            source="curator" if tools.job_session_id else "agent",
        )

    @server.tool(annotations=WRITE)
    @guard
    def record_thesis(
        title: str,
        statement: str,
        stance: Stance,
        confidence: Confidence,
        subject: Annotated[str | None, Field(description="Main entity of the thesis")] = None,
        horizon: Annotated[str | None, Field(description="e.g. 12m, FY27")] = None,
        assumption_claim_ids: Annotated[
            list[str], Field(description="Claims the thesis depends on")
        ] = [],  # noqa: B006
        supporting_claim_ids: list[str] = [],  # noqa: B006
        risk_claim_ids: list[str] = [],  # noqa: B006
        triggers: Annotated[
            list[TriggerIn], Field(description="Conditions that would invalidate it")
        ] = [],  # noqa: B006
    ) -> Thesis:
        """Record a conclusion and the claims it rests on."""
        return store.record_thesis(
            title=title,
            statement=statement,
            stance=stance,
            confidence=confidence,
            horizon=horizon,
            subject_entity_id=tools.entity(subject).id if subject else None,
            assumptions=assumption_claim_ids,
            supporting=supporting_claim_ids,
            risks=risk_claim_ids,
            triggers=[
                (t.description, tools.entity(t.entity).id if t.entity else None) for t in triggers
            ],
            session_id=tools.session_id(),
        )

    @server.tool(annotations=WRITE)
    @guard
    def update_thesis(
        thesis_id: str,
        reason: Annotated[str, Field(description="Why; kept in the thesis history")],
        status: ThesisStatus | None = None,
        confidence: Annotated[float | None, Field(ge=0.0, le=1.0)] = None,
        add_claims: Annotated[
            list[ThesisClaimIn],
            Field(description="Claims to link (e.g. newer, better-sourced assumptions)"),
        ] = [],  # noqa: B006
        remove_claim_ids: Annotated[
            list[str], Field(description="Claims the thesis no longer rests on")
        ] = [],  # noqa: B006
        add_triggers: Annotated[
            list[TriggerIn], Field(description="New conditions that would invalidate it")
        ] = [],  # noqa: B006
        remove_trigger_ids: list[str] = [],  # noqa: B006
    ) -> Thesis:
        """Revise a thesis: status, confidence, the claims it rests on, and its
        triggers, in one atomic step. Everything is kept in its history."""
        return store.revise_thesis(
            thesis_id,
            reason,
            status=status,
            confidence=confidence,
            add_claims=[(c.claim_id, c.role, c.weight) for c in add_claims],
            remove_claims=remove_claim_ids,
            add_triggers=[
                (t.description, tools.entity(t.entity).id if t.entity else None)
                for t in add_triggers
            ],
            remove_triggers=remove_trigger_ids,
        )

    @server.tool(annotations=WRITE)
    @guard
    def record_rejected_hypothesis(
        text: str,
        reason: Annotated[str, Field(description="Why it was ruled out")],
        entities: list[str] = [],  # noqa: B006
    ) -> RejectedHypothesis:
        """Record an idea that was checked and ruled out, so it isn't re-explored."""
        return store.record_rejected_hypothesis(
            text,
            reason,
            entity_ids=[tools.entity(e).id for e in entities],
            session_id=tools.session_id(),
        )

    # -- events & alerts --------------------------------------------------------

    @server.tool(annotations=WRITE)
    @guard
    def add_event(
        title: Annotated[
            str, Field(description="What happened, e.g. 'TSMC 8-K: CoWoS ramp delayed'")
        ],
        occurred_at: Annotated[datetime, Field(description="When it happened (UTC if no zone)")],
        entities: Annotated[list[str], Field(description="Entities the event is about")],
        kind: EventKind = EventKind.MANUAL,
        uri: str | None = None,
        summary: str | None = None,
    ) -> EventResult:
        """Record an event and raise alerts on theses it may affect (through the
        companies involved and their suppliers, customers and competitors)."""
        when = _aware(occurred_at)
        assert when is not None
        result = ingest_event(
            store,
            kind,
            title,
            when,
            entity_ids=[tools.entity(e).id for e in entities],
            source="agent",
            uri=uri,
            summary=summary,
        )
        return EventResult(
            event=result.event,
            created=result.created,
            alerts=[_alert_view(store, a) for a in result.alerts],
        )

    @server.tool(annotations=READ_ONLY)
    @guard
    def impact(
        entities: Annotated[list[str], Field(description="Entities a hypothetical event touches")],
        occurred_at: datetime | None = None,
    ) -> ImpactResult:
        """Dry run: which theses would an event on these entities affect, and why?
        Records nothing."""
        when = _aware(occurred_at) or tools.clock.now()
        ids = [tools.entity(e).id for e in entities]
        return ImpactResult(hits=impact_of(store, ids, when))

    @server.tool(annotations=READ_ONLY)
    @guard
    def list_alerts(
        status: AlertStatus = AlertStatus.OPEN,
        thesis_id: str | None = None,
    ) -> AlertList:
        """Alerts raised by events on theses, highest score first."""
        found = store.alerts(
            statuses=[status], thesis_ids=[thesis_id] if thesis_id is not None else None
        )
        return AlertList(alerts=[_alert_view(store, a) for a in found])

    @server.tool(annotations=WRITE)
    @guard
    def resolve_alert(
        alert_id: str,
        verdict: Annotated[
            Verdict,
            Field(description="supports | weakens (active -> at_risk) | breaks | unrelated"),
        ],
        rationale: Annotated[str, Field(description="Why, citing the event and evidence")],
        confidence: Annotated[
            float | None, Field(ge=0.0, le=1.0, description="New thesis confidence")
        ] = None,
        status: Annotated[
            ThesisStatus | None, Field(description="Override the default status change")
        ] = None,
    ) -> AlertView:
        """Judge an alert after reading the event and the thesis's claims; the
        thesis is updated accordingly and the change logged with the alert."""
        return _alert_view(
            store,
            store.resolve_alert(alert_id, verdict, rationale, confidence=confidence, status=status),
        )

    # -- graph queries ------------------------------------------------------------

    @server.tool(annotations=READ_ONLY)
    @guard
    def neighbors(
        entity: EntityRef,
        hops: Annotated[int, Field(ge=1, le=4)] = 1,
        relations: Annotated[
            list[RelationType], Field(description="Only these relation types (default all)")
        ] = [],  # noqa: B006
        valid_at: Annotated[
            datetime | None, Field(description="Only links true at this date")
        ] = None,
    ) -> Subgraph:
        """The entity's neighbourhood: companies within `hops` links (suppliers,
        customers, competitors...), the links among them, and theses about them."""
        return graph.neighbors(
            store,
            [tools.entity(entity).id],
            hops=hops,
            rel_types=relations,
            valid_at=_aware(valid_at),
        )

    @server.tool(annotations=READ_ONLY)
    @guard
    def find_paths(
        source: EntityRef,
        target: EntityRef,
        max_hops: Annotated[int, Field(ge=1, le=4)] = 3,
        relations: list[RelationType] = [],  # noqa: B006
    ) -> PathResult:
        """How two entities are connected, e.g. ASML -SUPPLIES-> TSMC -SUPPLIES-> AMD.
        Shortest and most confident paths first."""
        return graph.find_paths(
            store,
            tools.entity(source).id,
            tools.entity(target).id,
            max_hops=max_hops,
            rel_types=relations,
        )

    # -- watchers ---------------------------------------------------------------

    @server.tool(annotations=WRITE)
    @guard
    def watch(entity: EntityRef, note: str | None = None) -> WatchList:
        """Watch a company: its new SEC filings become events (on the next
        `torus watch run`). Subjects of open theses are watched automatically."""
        store.watch_entity(tools.entity(entity).id, note=note)
        return _watch_list(store)

    @server.tool(annotations=WRITE)
    @guard
    def unwatch(entity: EntityRef) -> WatchList:
        """Remove a company from the explicit watchlist."""
        store.unwatch_entity(tools.entity(entity).id)
        return _watch_list(store)

    @server.tool(annotations=READ_ONLY)
    @guard
    def list_watches() -> WatchList:
        """What the watchers follow: the watchlist, open-thesis entities, feeds."""
        return _watch_list(store)

    # -- distillation (curator) -----------------------------------------------

    @server.tool(annotations=WRITE)
    @guard
    def next_distillation(
        transcript_chars: Annotated[
            int, Field(ge=5_000, le=200_000)
        ] = transcript.DEFAULT_MAX_CHARS,
    ) -> NextDistillation:
        """Lease the oldest research session waiting to be distilled into the graph.

        Returns its transcript digest, what was already recorded (don't duplicate
        it), the sources the agent looked at, and existing theses on its entities.
        Until `complete_distillation`, new records are attributed to that session."""
        leased = store.lease_work(DISTILL_KIND, limit=1)
        remaining = len(store.queue_items(kind=DISTILL_KIND, statuses=[QueueStatus.PENDING]))
        if not leased:
            return NextDistillation(job=None, remaining=remaining)
        item = leased[0]
        session_id = str(item.payload.get("session_id", ""))
        session = store.get_session(session_id)
        tools.job_session_id = session.id
        activity = store.session_activity(session.id)
        names = {eid: name for eid, (_, name) in store.entity_index().items()}
        recorded = [_recorded_ref(store, t, i, names) for t, i in store.session_records(session.id)]
        theses: dict[str, ThesisRef] = {}
        for entity_id in activity.entity_ids:
            for t in store.theses(
                entity_id=entity_id, statuses=[ThesisStatus.ACTIVE, ThesisStatus.AT_RISK]
            ):
                theses[t.id] = ThesisRef(
                    id=t.id, title=t.title, status=t.status, confidence=t.confidence
                )
        job = DistillationJob(
            queue_id=item.id,
            session_id=session.id,
            attempt=item.attempts,
            capture_mode=str(item.payload.get("capture_mode", "balanced")),
            entities=[f"{names.get(e, e)} ({e})" for e in activity.entity_ids],
            already_recorded=[r for r in recorded if r is not None],
            theses=list(theses.values()),
            sources=[
                SourceRef(tool=c.tool, uri=c.uri, query=c.query, evidence_id=c.linked_evidence_id)
                for c in store.source_candidates(session.id)
            ],
            transcript=transcript.digest(session.transcript_path, max_chars=transcript_chars),
        )
        return NextDistillation(job=job, remaining=remaining)

    @server.tool(annotations=WRITE)
    @guard
    def complete_distillation(
        queue_id: str,
        summary: Annotated[str, Field(description="What was added, in a sentence or two")],
        failed: Annotated[
            bool, Field(description="True if the job couldn't be done; it will be retried")
        ] = False,
    ) -> DistillationOutcome:
        """Finish a leased distillation job (or hand it back if it failed)."""
        if failed:
            item = store.release_work(queue_id, error=summary)
        else:
            item = store.complete_work(queue_id, result={"summary": summary})
        tools.job_session_id = None
        remaining = len(store.queue_items(kind=DISTILL_KIND, statuses=[QueueStatus.PENDING]))
        return DistillationOutcome(queue_id=item.id, status=item.status, remaining=remaining)

    # -- resources ----------------------------------------------------------

    @server.resource("torus://entity/{ref}", mime_type="text/markdown")
    @guard
    def entity_resource(ref: str) -> str:
        """Prior research on one entity, as a compact markdown summary."""
        entity = tools.entity(ref)
        result = recall(store, tools.clock, entity_ids=[entity.id])
        return render_context(result, now=tools.clock.now())

    @server.resource("torus://thesis/{thesis_id}", mime_type="application/json")
    @guard
    def thesis_resource(thesis_id: str) -> str:
        """A thesis with its full status/confidence history."""
        view = ThesisView(
            thesis=store.get_thesis(thesis_id), history=store.thesis_history(thesis_id)
        )
        return view.model_dump_json(indent=2)

    @server.resource("torus://digest", mime_type="text/markdown")
    @guard
    def digest_resource() -> str:
        """Theses at risk and what changed in the last 7 days."""
        now = tools.clock.now()
        lines = ["## Torus digest"]
        at_risk = store.theses(statuses=[ThesisStatus.AT_RISK])
        lines.append(f"Theses at risk: {len(at_risk)}")
        lines += [f"- {t.title} (thesis {t.id}, conf {t.confidence:.2f})" for t in at_risk]
        changes = what_changed(store, now - timedelta(days=7)).changes
        lines.append(f"Changes in the last 7 days: {len(changes)}")
        lines += [f"- {c.at:%Y-%m-%d} {c.kind.value}: {c.summary}" for c in changes[-20:]]
        return "\n".join(lines)

    return server


def serve(store: Backend, reference: ReferenceData | None = None) -> None:
    """Run the server over stdio until the client disconnects."""
    tools = TorusTools(store, reference)
    build_server(tools).run("stdio")
