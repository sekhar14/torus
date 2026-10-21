"""Resolve a free-form reference ("NVDA", "$nvda", "0001045810", "Nvidia Corp",
an entity id) to one canonical entity.

Lookup order, stopping at the first step that finds anything:

1. entity id (merge redirects followed)
2. canonical key ("cik:0001045810", "ticker:NVDA") or bare CIK digits
3. ticker alias in the graph, then the SEC reference list
4. exact alias in the graph (any alias type)
5. company-name key in the graph (legal suffixes ignored), then the reference list

More than one graph match is reported as ambiguous with candidates: the resolver
never guesses. Reference-only matches are returned without touching the graph
unless ``create=True``, which adds them as company entities keyed by CIK.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import StrEnum

from torus.core.errors import NotFoundError
from torus.core.models import Alias, AliasType, Entity, EntityKind
from torus.core.reference import ReferenceCompany, ReferenceData, cik_key, format_cik
from torus.core.store.base import Backend
from torus.core.text import normalize_ticker

_TICKER = re.compile(r"^\$?[A-Za-z]{1,5}([.\-/][A-Za-z]{1,2})?$")
_CIK = re.compile(r"^(?:cik:?\s*)?(\d{1,10})$", re.IGNORECASE)
_ULID = re.compile(r"^[0-9A-HJKMNP-TV-Z]{26}$")


class ResolutionStatus(StrEnum):
    RESOLVED = "resolved"  # one entity in the graph
    REFERENCE = "reference"  # one company known only from reference data
    AMBIGUOUS = "ambiguous"  # several candidates; caller must choose
    NOT_FOUND = "not_found"


@dataclass(frozen=True)
class Resolution:
    status: ResolutionStatus
    query: str
    method: str = ""
    entity: Entity | None = None
    reference: ReferenceCompany | None = None
    candidates: tuple[Entity, ...] = field(default=())
    reference_candidates: tuple[ReferenceCompany, ...] = field(default=())

    @property
    def canonical_key(self) -> str | None:
        if self.entity is not None:
            return self.entity.canonical_key
        if self.reference is not None:
            return self.reference.canonical_key
        return None


class Resolver:
    def __init__(self, store: Backend, reference: ReferenceData | None = None) -> None:
        self.store = store
        self.reference = reference or ReferenceData()

    def resolve(self, ref: str, create: bool = False) -> Resolution:
        result = self._resolve(ref.strip())
        if create and result.status is ResolutionStatus.REFERENCE and result.reference:
            entity = self.ensure_company(result.reference)
            return Resolution(
                ResolutionStatus.RESOLVED, result.query, result.method + "+created", entity
            )
        return result

    def ensure_company(self, company: ReferenceCompany) -> Entity:
        """Add (or merge into) the graph entity for a reference company."""
        aliases = [Alias(alias=t, alias_type=AliasType.TICKER) for t in company.tickers]
        aliases.append(Alias(alias=company.cik, alias_type=AliasType.IDENTIFIER))
        return self.store.upsert_entity(
            EntityKind.COMPANY,
            company.title,
            canonical_key=company.canonical_key,
            aliases=aliases,
            attrs={"cik": company.cik, "tickers": list(company.tickers)},
        )

    def _resolve(self, ref: str) -> Resolution:
        if not ref:
            return Resolution(ResolutionStatus.NOT_FOUND, ref)

        if _ULID.match(ref):
            try:
                return self._one(ref, "id", [self.store.get_entity(ref)])
            except NotFoundError:
                pass

        if ":" in ref and not ref.lower().startswith("cik"):
            entity = self.store.get_entity_by_key(ref)
            if entity is not None:
                return self._one(ref, "canonical_key", [entity])

        cik_match = _CIK.match(ref)
        if cik_match:
            cik = format_cik(cik_match.group(1))
            entity = self.store.get_entity_by_key(cik_key(cik))
            if entity is not None:
                return self._one(ref, "cik", [entity])
            found = self.store.find_entities_by_alias(cik, [AliasType.IDENTIFIER])
            if found:
                return self._one(ref, "cik", found)
            company = self.reference.by_cik(cik)
            if company is not None:
                return Resolution(ResolutionStatus.REFERENCE, ref, "cik", reference=company)

        if _TICKER.match(ref):
            ticker = normalize_ticker(ref)
            found = self.store.find_entities_by_alias(ticker, [AliasType.TICKER])
            if found:
                return self._one(ref, "ticker", found)
            company = self.reference.by_ticker(ticker)
            if company is not None:
                return self._graph_or_reference(ref, "ticker", company)

        found = self.store.find_entities_by_alias(ref)
        if found:
            return self._one(ref, "alias", found)

        found = self.store.find_entities_by_name_key(
            ref, [AliasType.NAME, AliasType.FORMER_NAME, AliasType.ABBREVIATION]
        )
        if found:
            return self._one(ref, "name", found)

        companies = self.reference.by_name(ref)
        if len(companies) == 1:
            return self._graph_or_reference(ref, "name", companies[0])
        if len(companies) > 1:
            return Resolution(
                ResolutionStatus.AMBIGUOUS,
                ref,
                "name",
                reference_candidates=tuple(companies),
            )
        return Resolution(ResolutionStatus.NOT_FOUND, ref)

    def _graph_or_reference(self, ref: str, method: str, company: ReferenceCompany) -> Resolution:
        """A reference hit may already exist in the graph under its CIK key."""
        entity = self.store.get_entity_by_key(company.canonical_key)
        if entity is not None:
            return self._one(ref, method, [entity])
        return Resolution(ResolutionStatus.REFERENCE, ref, method, reference=company)

    @staticmethod
    def _one(ref: str, method: str, found: list[Entity]) -> Resolution:
        if len(found) == 1:
            return Resolution(ResolutionStatus.RESOLVED, ref, method, entity=found[0])
        return Resolution(ResolutionStatus.AMBIGUOUS, ref, method, candidates=tuple(found))
