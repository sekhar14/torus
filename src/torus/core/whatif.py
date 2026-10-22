"""What if...? Run impact analysis on a hypothetical event without recording it.

The same graph walk, materiality and trigger matching as a real event, so the
answer is what Torus *would* alert on, plus theses just under the threshold.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime

from pydantic import BaseModel

from torus.core.impact import ImpactHit, global_threshold, impact
from torus.core.materiality import assess
from torus.core.models import Stance, ThesisStatus
from torus.core.store.base import Backend

NEAR_MISS_FACTOR = 0.5  # also show theses scoring at least half the threshold


class WhatIfHit(BaseModel):
    hit: ImpactHit
    stance: Stance
    status: ThesisStatus
    confidence: float
    would_alert: bool


class WhatIfResult(BaseModel):
    description: str
    materiality: float
    materiality_reason: str
    threshold: float
    hits: list[WhatIfHit]


def what_if(
    store: Backend,
    entity_ids: Sequence[str],
    description: str,
    at: datetime,
    materiality: float | None = None,
) -> WhatIfResult:
    if materiality is None:
        assessed = assess("whatif", description)
        weight, why = assessed.value, assessed.reason
    else:
        weight, why = materiality, "given"
    threshold = global_threshold(store)
    hits = impact(
        store,
        entity_ids,
        at,
        threshold=threshold * NEAR_MISS_FACTOR,
        text=description,
        materiality=weight,
    )
    out = []
    for hit in hits:
        thesis = store.get_thesis(hit.thesis_id)
        limit = thesis.alert_threshold if thesis.alert_threshold is not None else threshold
        out.append(
            WhatIfHit(
                hit=hit,
                stance=thesis.stance,
                status=thesis.status,
                confidence=thesis.confidence,
                would_alert=hit.score >= limit,
            )
        )
    return WhatIfResult(
        description=description,
        materiality=weight,
        materiality_reason=why,
        threshold=threshold,
        hits=out,
    )
