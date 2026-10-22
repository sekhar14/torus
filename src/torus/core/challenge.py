"""Where is a thesis weakest? The deterministic half of ``/torus:challenge``.

The challenger agent argues against a thesis; this module tells it where to push:

* assumptions that are thin: one source or none, low confidence, opinion or
  forecast, expired or expiring soon, contradicted by recorded evidence, or
  derived from claims since retracted
* dependencies nothing watches: strong supplier/customer links of the subject
  that no claim or trigger of the thesis mentions, so news about them alerts
  only through the graph and the thesis never says what it assumes about them
* triggers that can only fire on wording (no entity attached)
* opposing views already in the graph: theses on the same subject with the
  opposite stance, ruled-out ideas, and alerts judged as weakening
"""

from __future__ import annotations

from datetime import datetime, timedelta

from pydantic import BaseModel

from torus.core.models import (
    AlertStatus,
    ClaimType,
    EvidenceKind,
    RelationType,
    Stance,
    Support,
    ThesisClaimRole,
    Verdict,
)
from torus.core.store.base import Backend

LOW_CONFIDENCE = 0.5
EXPIRING_DAYS = 60
STRONG_LINK = 0.7
OPPOSITE = {Stance.BULLISH: Stance.BEARISH, Stance.BEARISH: Stance.BULLISH}


class AssumptionCheck(BaseModel):
    claim_id: str
    text: str
    role: ThesisClaimRole
    claim_type: ClaimType
    confidence: float
    sources: int
    primary_sources: int
    issues: list[str]


class Dependency(BaseModel):
    entity_id: str
    name: str
    link: str  # e.g. "TSMC SUPPLIES NVIDIA"
    confidence: float
    source: str


class OpposingView(BaseModel):
    kind: str  # thesis | ruled_out | alert
    id: str
    text: str
    detail: str


class WeakPoints(BaseModel):
    thesis_id: str
    title: str
    statement: str
    stance: Stance
    confidence: float
    subject: str | None
    assumptions: list[AssumptionCheck]
    unwatched_dependencies: list[Dependency]
    text_only_triggers: list[str]
    opposing_views: list[OpposingView]
    retracted_links: int

    @property
    def issue_count(self) -> int:
        return (
            sum(len(a.issues) for a in self.assumptions)
            + len(self.unwatched_dependencies)
            + len(self.text_only_triggers)
        )


def weak_points(store: Backend, thesis_id: str, now: datetime) -> WeakPoints:
    thesis = store.get_thesis(thesis_id)
    names = {eid: name for eid, (_, name) in store.entity_index().items()}
    mentioned: set[str] = set()
    checks = []
    retracted = 0
    for link in thesis.claims:
        claim = store.current_claim(link.claim_lineage_id)
        if claim is None:
            retracted += 1
            continue
        mentioned |= {ce.entity_id for ce in claim.entities}
        supporting = [e.evidence_id for e in claim.evidence if e.support is Support.SUPPORTS]
        kinds = [store.get_evidence(e).kind for e in supporting]
        primary = sum(1 for k in kinds if k in (EvidenceKind.FILING, EvidenceKind.TRANSCRIPT))
        issues = []
        if not supporting and not claim.derived_from:
            issues.append("no source recorded")
        elif len(supporting) == 1 and not primary:
            issues.append("a single non-primary source")
        if claim.confidence < LOW_CONFIDENCE:
            issues.append(f"low confidence ({claim.confidence:.2f})")
        if claim.claim_type in (ClaimType.OPINION, ClaimType.FORECAST):
            issues.append(f"a {claim.claim_type.value}, not a fact")
        if claim.valid_to is not None:
            if claim.valid_to <= now:
                issues.append(f"expired {claim.valid_to:%Y-%m-%d}")
            elif claim.valid_to <= now + timedelta(days=EXPIRING_DAYS):
                issues.append(f"expires {claim.valid_to:%Y-%m-%d}")
        if any(e.support is Support.CONTRADICTS for e in claim.evidence):
            issues.append("recorded evidence contradicts it")
        for source_id in claim.derived_from:
            if store.current_claim(store.get_claim(source_id).lineage_id) is None:
                issues.append("derived from a retracted claim")
                break
        checks.append(
            AssumptionCheck(
                claim_id=claim.id,
                text=claim.text,
                role=link.role,
                claim_type=claim.claim_type,
                confidence=claim.confidence,
                sources=len(supporting),
                primary_sources=primary,
                issues=issues,
            )
        )
    checks.sort(key=lambda c: (c.role is not ThesisClaimRole.ASSUMPTION, -len(c.issues)))

    watched = mentioned | {t.entity_id for t in thesis.triggers if t.entity_id}
    dependencies = []
    if thesis.subject_entity_id:
        subject = thesis.subject_entity_id
        for rel in store.relations(subject):
            if rel.rel not in (RelationType.SUPPLIES, RelationType.CUSTOMER_OF):
                continue
            other = rel.dst_id if rel.src_id == subject else rel.src_id
            if other in watched or rel.confidence < STRONG_LINK:
                continue
            src, dst = names.get(rel.src_id, rel.src_id), names.get(rel.dst_id, rel.dst_id)
            dependencies.append(
                Dependency(
                    entity_id=other,
                    name=names.get(other, other),
                    link=f"{src} {rel.rel.value} {dst}",
                    confidence=rel.confidence,
                    source=rel.source,
                )
            )
    dependencies.sort(key=lambda d: -d.confidence)

    opposing = []
    if thesis.subject_entity_id:
        for rival in store.theses(entity_id=thesis.subject_entity_id):
            if rival.id != thesis.id and OPPOSITE.get(thesis.stance) is rival.stance:
                opposing.append(
                    OpposingView(
                        kind="thesis",
                        id=rival.id,
                        text=rival.title,
                        detail=f"{rival.stance.value}, {rival.status.value},"
                        f" confidence {rival.confidence:.2f}",
                    )
                )
        opposing += [
            OpposingView(kind="ruled_out", id=h.id, text=h.text, detail=h.reason)
            for h in store.rejected_hypotheses(thesis.subject_entity_id)
        ]
    for alert in store.alerts(statuses=[AlertStatus.RESOLVED], thesis_ids=[thesis.id]):
        if alert.verdict in (Verdict.WEAKENS, Verdict.BREAKS):
            event = store.get_event(alert.event_id)
            opposing.append(
                OpposingView(
                    kind="alert",
                    id=alert.id,
                    text=event.title,
                    detail=f"judged {alert.verdict.value}: {alert.rationale or ''}",
                )
            )

    return WeakPoints(
        thesis_id=thesis.id,
        title=thesis.title,
        statement=thesis.statement,
        stance=thesis.stance,
        confidence=thesis.confidence,
        subject=names.get(thesis.subject_entity_id) if thesis.subject_entity_id else None,
        assumptions=checks,
        unwatched_dependencies=dependencies,
        text_only_triggers=[t.description for t in thesis.triggers if t.entity_id is None],
        opposing_views=opposing,
        retracted_links=retracted,
    )
