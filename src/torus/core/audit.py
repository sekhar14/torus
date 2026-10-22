"""Graph health checks: what in the research memory has gone stale, thin or
inconsistent. Deterministic; the agent decides what to do about each finding.

A wrong or outdated claim doesn't just sit in the graph: recall injects it into
future prompts and impact analysis builds alerts on it. The audit surfaces:

==========================  ====================================================
expired_assumption          an open thesis rests on a claim past its ``valid_to``
past_horizon                an open thesis is past its horizon date
single_source_assumption    an assumption backed by one non-filing source
opinion_assumption          an assumption that is an opinion or forecast with no
                            evidence
retracted_basis             an inference with no evidence whose source claims
                            were all retracted
possible_conflict           two current claims about the same company that look
                            alike but differ in numbers or direction (candidates)
stale_thesis                an open thesis unchanged for ``stale_days``
unconfirmed_starter_link    a starter-pack link touching an open thesis's
                            companies that research never confirmed
orphan_entity               an entity with no claims, links, theses or watch
==========================  ====================================================
"""

from __future__ import annotations

import re
from collections import Counter
from collections.abc import Callable
from datetime import datetime, timedelta
from enum import StrEnum

from pydantic import BaseModel

from torus.core import triggers
from torus.core.models import (
    Claim,
    ClaimType,
    EvidenceKind,
    Support,
    Thesis,
    ThesisClaimRole,
    ThesisStatus,
)
from torus.core.store.base import Backend

DEFAULT_STALE_DAYS = 90
MAX_CONFLICTS = 20
CONFLICT_SIMILARITY = 0.5
OPEN = (ThesisStatus.ACTIVE, ThesisStatus.AT_RISK)

_NUMBER = re.compile(r"(?<![\w.])\$?(\d+(?:[.,]\d+)*)\s*(%|bn|b|billion|m|million|k|x)?", re.I)
_DIRECTION_UP = {"rise", "up", "growth", "grow", "gain", "increase", "higher", "beat"}
_DIRECTION_DOWN = {"fall", "down", "cut", "decline", "lower", "loss", "miss", "shrink"}


class Severity(StrEnum):
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"


class Finding(BaseModel):
    kind: str
    severity: Severity
    title: str
    detail: str
    object_type: str  # claim | thesis | relation | entity
    object_ids: list[str]
    thesis_id: str | None = None
    suggestion: str


class AuditReport(BaseModel):
    generated_at: datetime
    findings: list[Finding]
    counts: dict[str, int]

    @property
    def by_severity(self) -> dict[str, int]:
        return dict(Counter(f.severity.value for f in self.findings))


def _numbers(text: str) -> set[str]:
    return {(n.replace(",", "") + (unit or "").lower()) for n, unit in _NUMBER.findall(text)}


def _direction(words: set[str]) -> int:
    up, down = bool(words & _DIRECTION_UP), bool(words & _DIRECTION_DOWN)
    return 0 if up == down else (1 if up else -1)


def _similarity(a: set[str], b: set[str]) -> float:
    return len(a & b) / len(a | b) if a and b else 0.0


def audit(
    store: Backend,
    now: datetime,
    stale_days: int = DEFAULT_STALE_DAYS,
) -> AuditReport:
    findings: list[Finding] = []
    names = {eid: name for eid, (_, name) in store.entity_index().items()}
    theses = store.theses(statuses=list(OPEN))
    evidence_kinds: dict[str, EvidenceKind] = {}

    def kind_of(evidence_id: str) -> EvidenceKind:
        if evidence_id not in evidence_kinds:
            evidence_kinds[evidence_id] = store.get_evidence(evidence_id).kind
        return evidence_kinds[evidence_id]

    for thesis in theses:
        findings += _thesis_findings(store, thesis, now, stale_days, kind_of)

    claims = store.claims()
    for claim in claims:
        if claim.claim_type is not ClaimType.INFERENCE or claim.evidence or not claim.derived_from:
            continue
        sources = [store.current_claim(store.get_claim(c).lineage_id) for c in claim.derived_from]
        if all(source is None for source in sources):
            findings.append(
                Finding(
                    kind="retracted_basis",
                    severity=Severity.MEDIUM,
                    title="Inference built only on retracted claims",
                    detail=claim.text,
                    object_type="claim",
                    object_ids=[claim.id],
                    suggestion="Re-derive it from current claims (supersede_claim) or retract it.",
                )
            )
    findings += _conflicts(claims, names)
    findings += _starter_links(store, theses, names)
    findings += _orphans(store, names)

    order = {Severity.HIGH: 0, Severity.MEDIUM: 1, Severity.LOW: 2}
    findings.sort(key=lambda f: (order[f.severity], f.kind, f.object_ids))
    return AuditReport(
        generated_at=now, findings=findings, counts=dict(Counter(f.kind for f in findings))
    )


def _thesis_findings(
    store: Backend,
    thesis: Thesis,
    now: datetime,
    stale_days: int,
    kind_of: Callable[[str], EvidenceKind],
) -> list[Finding]:
    found = []
    if thesis.horizon_date is not None and thesis.horizon_date < now:
        found.append(
            Finding(
                kind="past_horizon",
                severity=Severity.HIGH,
                title=f"Past its horizon ({thesis.horizon}): {thesis.title}",
                detail=f"Due {thesis.horizon_date:%Y-%m-%d}; still {thesis.status.value}.",
                object_type="thesis",
                object_ids=[thesis.id],
                thesis_id=thesis.id,
                suggestion="Decide the outcome: mark it confirmed or broken, or extend the"
                " horizon with update_thesis.",
            )
        )
    if thesis.updated_at < now - timedelta(days=stale_days):
        found.append(
            Finding(
                kind="stale_thesis",
                severity=Severity.LOW,
                title=f"Unchanged for {(now - thesis.updated_at).days} days: {thesis.title}",
                detail=f"Last updated {thesis.updated_at:%Y-%m-%d}.",
                object_type="thesis",
                object_ids=[thesis.id],
                thesis_id=thesis.id,
                suggestion="Re-check its assumptions against recent filings and news.",
            )
        )
    for link in thesis.claims:
        if link.role is not ThesisClaimRole.ASSUMPTION:
            continue
        claim = store.current_claim(link.claim_lineage_id)
        if claim is None:
            continue
        found += _assumption_findings(thesis, claim, now, kind_of)
    return found


def _assumption_findings(
    thesis: Thesis, claim: Claim, now: datetime, kind_of: Callable[[str], EvidenceKind]
) -> list[Finding]:
    found = []
    if claim.valid_to is not None and claim.valid_to <= now:
        found.append(
            Finding(
                kind="expired_assumption",
                severity=Severity.HIGH,
                title=f"Assumption expired {claim.valid_to:%Y-%m-%d}: {thesis.title}",
                detail=claim.text,
                object_type="claim",
                object_ids=[claim.id],
                thesis_id=thesis.id,
                suggestion="Find current data and supersede_claim, or re-judge the thesis.",
            )
        )
    supporting = [e.evidence_id for e in claim.evidence if e.support is Support.SUPPORTS]
    if claim.claim_type in (ClaimType.OPINION, ClaimType.FORECAST) and not supporting:
        found.append(
            Finding(
                kind="opinion_assumption",
                severity=Severity.MEDIUM,
                title=f"Assumption is an unsourced {claim.claim_type.value}: {thesis.title}",
                detail=claim.text,
                object_type="claim",
                object_ids=[claim.id],
                thesis_id=thesis.id,
                suggestion="Back it with evidence (whose view is it?) or lower its weight.",
            )
        )
    elif len(supporting) == 1 and kind_of(supporting[0]) is not EvidenceKind.FILING:
        found.append(
            Finding(
                kind="single_source_assumption",
                severity=Severity.MEDIUM,
                title=f"Assumption rests on one source: {thesis.title}",
                detail=claim.text,
                object_type="claim",
                object_ids=[claim.id],
                thesis_id=thesis.id,
                suggestion="Corroborate it with a primary source (filing, transcript).",
            )
        )
    return found


def _conflicts(claims: list[Claim], names: dict[str, str]) -> list[Finding]:
    by_subject: dict[str, list[Claim]] = {}
    for claim in claims:
        for ce in claim.entities:
            if ce.role.value == "subject":
                by_subject.setdefault(ce.entity_id, []).append(claim)
    found: list[Finding] = []
    seen: set[tuple[str, str]] = set()
    for entity_id, group in by_subject.items():
        exclude = [names.get(entity_id, "")]
        prepared = [(c, triggers.terms(c.text, exclude), _numbers(c.text)) for c in group]
        for i, (a, terms_a, nums_a) in enumerate(prepared):
            for b, terms_b, nums_b in prepared[i + 1 :]:
                key = (min(a.lineage_id, b.lineage_id), max(a.lineage_id, b.lineage_id))
                if key in seen or a.lineage_id == b.lineage_id:
                    continue
                words_a = terms_a - {t for t in terms_a if t[:1].isdigit()}
                words_b = terms_b - {t for t in terms_b if t[:1].isdigit()}
                if _similarity(words_a, words_b) < CONFLICT_SIMILARITY:
                    continue
                numbers_differ = bool(nums_a and nums_b and nums_a != nums_b)
                opposite = _direction(terms_a) * _direction(terms_b) < 0
                if not (numbers_differ or opposite):
                    continue
                if _windows_apart(a, b):
                    continue  # e.g. FY2025 vs FY2026 figures: both can be true
                seen.add(key)
                found.append(
                    Finding(
                        kind="possible_conflict",
                        severity=Severity.MEDIUM,
                        title=f"Claims about {names.get(entity_id, entity_id)} may conflict",
                        detail=f"{a.text}  <->  {b.text}",
                        object_type="claim",
                        object_ids=[a.id, b.id],
                        suggestion="Check both sources; supersede or retract the wrong one, or"
                        " set validity dates if both were true at different times.",
                    )
                )
                if len(found) >= MAX_CONFLICTS:
                    return found
    return found


def _windows_apart(a: Claim, b: Claim) -> bool:
    """True when the claims' validity windows don't overlap."""
    if a.valid_to is not None and b.valid_from is not None and a.valid_to <= b.valid_from:
        return True
    return b.valid_to is not None and a.valid_from is not None and b.valid_to <= a.valid_from


def _starter_links(store: Backend, theses: list[Thesis], names: dict[str, str]) -> list[Finding]:
    entities: dict[str, Thesis] = {}
    for thesis in theses:
        if thesis.subject_entity_id:
            entities.setdefault(thesis.subject_entity_id, thesis)
        for trigger in thesis.triggers:
            if trigger.entity_id:
                entities.setdefault(trigger.entity_id, thesis)
    found = []
    seen = set()
    for entity_id, thesis in entities.items():
        for rel in store.relations(entity_id):
            if not rel.source.startswith("starter:") or rel.lineage_id in seen:
                continue
            seen.add(rel.lineage_id)
            src, dst = names.get(rel.src_id, rel.src_id), names.get(rel.dst_id, rel.dst_id)
            text = f"{src} {rel.rel.value} {dst}"
            found.append(
                Finding(
                    kind="unconfirmed_starter_link",
                    severity=Severity.LOW,
                    title=f"Unconfirmed starter link: {text}",
                    detail=f"From {rel.source}; alerts on '{thesis.title}' can travel along it.",
                    object_type="relation",
                    object_ids=[rel.id],
                    thesis_id=thesis.id,
                    suggestion="Confirm it from a filing (record_relation again) or correct it.",
                )
            )
    return found


def _orphans(store: Backend, names: dict[str, str]) -> list[Finding]:
    linked = {r.src_id for r in store.relations()} | {r.dst_id for r in store.relations()}
    linked |= {ce.entity_id for c in store.claims() for ce in c.entities}
    linked |= {e.id for e in store.watch_targets()}
    for thesis in store.theses():
        if thesis.subject_entity_id:
            linked.add(thesis.subject_entity_id)
    return [
        Finding(
            kind="orphan_entity",
            severity=Severity.LOW,
            title=f"Nothing recorded about {name}",
            detail="No claims, links, theses or watch.",
            object_type="entity",
            object_ids=[entity_id],
            suggestion="Research it, link it, or leave it: orphans are harmless but noisy.",
        )
        for entity_id, name in sorted(names.items(), key=lambda kv: kv[1])
        if entity_id not in linked and store.live_entity_id(entity_id) == entity_id
    ]
