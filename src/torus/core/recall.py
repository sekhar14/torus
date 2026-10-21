"""Assemble what Torus already knows about some entities and/or a question.

Ranking:

* theses: at-risk first, then active, broken, confirmed; newest update first
* claims: ``score = 4·entity_match + 2·query_match + 2·recency·confidence`` where
  entity_match is 1 / 0.8 / 0.6 for subject / object / mentioned, query_match is
  ``1 / (1 + rank)`` in the full-text results, and recency halves every 90 days

``render_context`` turns a result into a compact markdown block under a token
budget, for injection into a prompt (hooks) or a digest.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from datetime import datetime

from pydantic import BaseModel

from torus.core.clock import Clock
from torus.core.models import (
    AlertStatus,
    AliasType,
    Claim,
    ClaimEntityRole,
    ClaimType,
    Entity,
    EntityKind,
    Stance,
    ThesisClaimRole,
    ThesisStatus,
)
from torus.core.store.base import Backend

HALF_LIFE_DAYS = 90.0
ROLE_WEIGHT = {
    ClaimEntityRole.SUBJECT: 1.0,
    ClaimEntityRole.OBJECT: 0.8,
    ClaimEntityRole.MENTIONED: 0.6,
}
STATUS_ORDER = {
    ThesisStatus.AT_RISK: 0,
    ThesisStatus.ACTIVE: 1,
    ThesisStatus.BROKEN: 2,
    ThesisStatus.CONFIRMED: 3,
    ThesisStatus.ARCHIVED: 4,
}
CANDIDATES_PER_ENTITY = 200
DEFAULT_BUDGET_TOKENS = 1500


class EvidenceRef(BaseModel):
    id: str
    uri: str
    title: str | None
    published_at: datetime | None


class RecalledEntity(BaseModel):
    id: str
    name: str
    kind: EntityKind
    canonical_key: str | None
    tickers: list[str]
    last_researched: datetime | None


class RecalledClaim(BaseModel):
    id: str
    lineage_id: str
    text: str
    claim_type: ClaimType
    confidence: float
    recorded_at: datetime
    valid_from: datetime | None
    valid_to: datetime | None
    entities: list[str]  # entity names
    evidence: list[EvidenceRef]
    score: float


class ThesisClaimRef(BaseModel):
    lineage_id: str
    role: ThesisClaimRole
    text: str | None  # None when the claim was retracted
    confidence: float | None


class RecalledThesis(BaseModel):
    id: str
    title: str
    statement: str
    stance: Stance
    status: ThesisStatus
    confidence: float
    horizon: str | None
    subject: str | None  # entity name
    updated_at: datetime
    claims: list[ThesisClaimRef]


class RecalledHypothesis(BaseModel):
    id: str
    text: str
    reason: str
    recorded_at: datetime


class RecalledAlert(BaseModel):
    id: str
    thesis_id: str
    thesis_title: str
    event_title: str
    event_uri: str | None
    occurred_at: datetime
    score: float
    why: str  # strongest path, e.g. "TSMC -SUPPLIES-> NVIDIA CORP via assumption claim"


class RecallResult(BaseModel):
    as_of: datetime | None
    query: str | None
    entities: list[RecalledEntity]
    alerts: list[RecalledAlert] = []
    theses: list[RecalledThesis]
    claims: list[RecalledClaim]
    rejected_hypotheses: list[RecalledHypothesis]
    total_claims: int  # candidates before the limit was applied

    @property
    def is_empty(self) -> bool:
        return not (self.alerts or self.theses or self.claims or self.rejected_hypotheses)


def recall(
    store: Backend,
    clock: Clock,
    entity_ids: Sequence[str] = (),
    query: str | None = None,
    as_of: datetime | None = None,
    limit: int = 20,
    thesis_limit: int = 10,
    include_archived: bool = False,
) -> RecallResult:
    now = as_of or clock.now()
    names = {eid: name for eid, (_, name) in store.entity_index().items()}
    entity_ids = list(dict.fromkeys(store.live_entity_id(e) for e in entity_ids))

    # -- claims -------------------------------------------------------------
    entity_match: dict[str, float] = {}
    claims: dict[str, Claim] = {}
    for eid in entity_ids:
        for claim in store.claims(entity_id=eid, as_of=as_of, limit=CANDIDATES_PER_ENTITY):
            claims[claim.id] = claim
            weight = max(
                (ROLE_WEIGHT[ce.role] for ce in claim.entities if ce.entity_id == eid),
                default=0.0,
            )
            entity_match[claim.id] = max(entity_match.get(claim.id, 0.0), weight)

    query_match: dict[str, float] = {}
    if query:
        hits = store.search(query, object_types=["claim"], limit=50, match_any=True)
        for pos, claim in enumerate(store.get_claims([h.object_id for h in hits])):
            if as_of is not None and claim.recorded_at > as_of:
                continue
            claims.setdefault(claim.id, claim)
            query_match[claim.id] = 1.0 / (1 + pos)

    def score(claim: Claim) -> float:
        age_days = max((now - claim.recorded_at).total_seconds() / 86400, 0.0)
        recency = math.pow(0.5, age_days / HALF_LIFE_DAYS)
        return (
            4 * entity_match.get(claim.id, 0.0)
            + 2 * query_match.get(claim.id, 0.0)
            + 2 * recency * claim.confidence
        )

    ranked = sorted(claims.values(), key=lambda c: (-score(c), -c.recorded_at.timestamp()))
    recalled_claims = [_recalled_claim(store, c, score(c), names) for c in ranked[:limit]]

    # -- theses -------------------------------------------------------------
    thesis_ids: dict[str, None] = {}
    for eid in entity_ids:
        for t in store.theses(entity_id=eid):
            thesis_ids[t.id] = None
    if query:
        for hit in store.search(query, object_types=["thesis"], limit=20, match_any=True):
            thesis_ids[hit.object_id] = None
    theses = []
    for thesis_id in thesis_ids:
        thesis = store.get_thesis(thesis_id)
        status, confidence = thesis.status, thesis.confidence
        if as_of is not None:
            state = store.thesis_state_as_of(thesis_id, as_of)
            if state is None:
                continue  # did not exist yet
            status, confidence = state.status, state.confidence
        if status is ThesisStatus.ARCHIVED and not include_archived:
            continue
        refs = []
        links = thesis.claims if as_of is None else store.thesis_links_as_of(thesis_id, as_of)
        for link in links:
            current = store.current_claim(link.claim_lineage_id, as_of=as_of)
            refs.append(
                ThesisClaimRef(
                    lineage_id=link.claim_lineage_id,
                    role=link.role,
                    text=current.text if current else None,
                    confidence=current.confidence if current else None,
                )
            )
        theses.append(
            RecalledThesis(
                id=thesis.id,
                title=thesis.title,
                statement=thesis.statement,
                stance=thesis.stance,
                status=status,
                confidence=confidence,
                horizon=thesis.horizon,
                subject=names.get(thesis.subject_entity_id or ""),
                updated_at=thesis.updated_at,
                claims=refs,
            )
        )
    theses.sort(key=lambda t: (STATUS_ORDER[t.status], -t.updated_at.timestamp()))

    # -- rejected hypotheses & entities -------------------------------------
    hypotheses: dict[str, RecalledHypothesis] = {}
    for eid in entity_ids:
        for h in store.rejected_hypotheses(entity_id=eid):
            if as_of is None or h.recorded_at <= as_of:
                hypotheses[h.id] = RecalledHypothesis(
                    id=h.id, text=h.text, reason=h.reason, recorded_at=h.recorded_at
                )
    entities = [_recalled_entity(store, store.get_entity(eid), as_of) for eid in entity_ids]

    # Open alerts only make sense for "now"; a past view shows the past.
    alerts = []
    if as_of is None:
        titles = {t.id: t.title for t in theses}
        for alert in store.alerts(statuses=[AlertStatus.OPEN], thesis_ids=list(titles)):
            event = store.get_event(alert.event_id)
            top = alert.paths[0] if alert.paths else None
            alerts.append(
                RecalledAlert(
                    id=alert.id,
                    thesis_id=alert.thesis_id,
                    thesis_title=titles[alert.thesis_id],
                    event_title=event.title,
                    event_uri=event.uri,
                    occurred_at=event.occurred_at,
                    score=alert.score,
                    why=f"{top.route} via {top.via}" if top else "",
                )
            )

    return RecallResult(
        as_of=as_of,
        query=query,
        entities=entities,
        alerts=alerts,
        theses=theses[:thesis_limit],
        claims=recalled_claims,
        rejected_hypotheses=sorted(hypotheses.values(), key=lambda h: h.recorded_at, reverse=True)[
            :5
        ],
        total_claims=len(claims),
    )


def _recalled_claim(
    store: Backend, claim: Claim, score: float, names: dict[str, str]
) -> RecalledClaim:
    evidence = []
    for link in claim.evidence:
        ev = store.get_evidence(link.evidence_id)
        evidence.append(
            EvidenceRef(id=ev.id, uri=ev.uri, title=ev.title, published_at=ev.published_at)
        )
    return RecalledClaim(
        id=claim.id,
        lineage_id=claim.lineage_id,
        text=claim.text,
        claim_type=claim.claim_type,
        confidence=claim.confidence,
        recorded_at=claim.recorded_at,
        valid_from=claim.valid_from,
        valid_to=claim.valid_to,
        entities=[names.get(ce.entity_id, ce.entity_id) for ce in claim.entities],
        evidence=evidence,
        score=round(score, 4),
    )


def _recalled_entity(store: Backend, entity: Entity, as_of: datetime | None) -> RecalledEntity:
    newest = store.claims(entity_id=entity.id, as_of=as_of, limit=1)
    return RecalledEntity(
        id=entity.id,
        name=entity.name,
        kind=entity.kind,
        canonical_key=entity.canonical_key,
        tickers=[a.alias for a in entity.aliases if a.alias_type is AliasType.TICKER],
        last_researched=newest[0].recorded_at if newest else None,
    )


# -- rendering ------------------------------------------------------------------


def _tokens(text: str) -> int:
    return (len(text) + 3) // 4


def _day(value: datetime) -> str:
    return value.strftime("%Y-%m-%d")


def _host(uri: str) -> str:
    rest = uri.split("://", 1)[-1]
    return rest.split("/", 1)[0] or uri


def render_context(
    result: RecallResult,
    now: datetime,
    budget_tokens: int = DEFAULT_BUDGET_TOKENS,
) -> str:
    """Markdown summary that fits ``budget_tokens`` (approx. 4 chars/token).

    Order of priority: entities, theses, claims, rejected hypotheses. Whatever
    doesn't fit is counted in a closing line that points at the recall tool.
    """
    if result.is_empty and not result.entities:
        return ""
    header = "## Torus: prior research"
    if result.as_of:
        header += f" (as of {_day(result.as_of)})"

    sections: list[tuple[str, list[str]]] = []
    entity_lines = []
    for e in result.entities:
        label = e.name + (f" ({', '.join(e.tickers)})" if e.tickers else "")
        if e.last_researched:
            days = (now - e.last_researched).days
            label += f" - last researched {_day(e.last_researched)} ({days}d ago)"
        else:
            label += " - no research recorded yet"
        entity_lines.append(f"- {label}")
    sections.append(("", entity_lines))

    alert_lines = [
        f'- {_day(a.occurred_at)} {a.event_title} may affect "{a.thesis_title}"'
        f" (score {a.score:.2f}; {a.why}; alert {a.id})"
        for a in result.alerts
    ]
    sections.append(("### Open alerts - review with /torus:review", alert_lines))

    thesis_lines = []
    for t in result.theses:
        line = (
            f"- [{t.status.value}] {t.title} - {t.stance.value}, conf {t.confidence:.2f}"
            + (f", {t.horizon}" if t.horizon else "")
            + f" (thesis {t.id})"
        )
        assumptions = [c for c in t.claims if c.role is ThesisClaimRole.ASSUMPTION]
        for ref in assumptions[:3]:
            text = ref.text if ref.text is not None else "(retracted)"
            line += f"\n  - assumes: {text}"
        thesis_lines.append(line)
    sections.append(("### Theses", thesis_lines))

    claim_lines = []
    for c in result.claims:
        src = f" src: {_host(c.evidence[0].uri)}" if c.evidence else ""
        claim_lines.append(
            f"- {_day(c.recorded_at)} {c.claim_type.value} ({c.confidence:.2f}): {c.text}"
            f" [claim {c.id}]{src}"
        )
    sections.append(("### Claims", claim_lines))

    hyp_lines = [f"- {h.text} - rejected: {h.reason}" for h in result.rejected_hypotheses]
    sections.append(("### Rejected hypotheses", hyp_lines))

    footer_reserve = 30
    used = _tokens(header) + footer_reserve
    out = [header]
    skipped = 0
    for title, lines in sections:
        if not lines:
            continue
        title_cost = _tokens(title) if title else 0
        wrote_title = False
        for line in lines:
            cost = _tokens(line) + (0 if wrote_title or not title else title_cost)
            if used + cost > budget_tokens:
                skipped += 1
                continue
            if title and not wrote_title:
                out.append(title)
                wrote_title = True
            out.append(line)
            used += cost
    skipped += max(result.total_claims - len(result.claims), 0)
    if skipped:
        out.append(f"({skipped} more items not shown; use the torus `recall` tool for details)")
    return "\n".join(out)
