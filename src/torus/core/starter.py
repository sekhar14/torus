"""Starter graph packs: curated supplier / customer / competitor links that give a
new graph enough structure for impact analysis to work from day one.

Packs ship in ``torus/data/starter/<name>.json``. Seeding is idempotent and never
overrides what research recorded: a link that already exists (from any source)
is left alone. Starter links carry the source ``starter:<name>@<version>`` so
they can be told apart, filtered out in the explorer, or replaced as research
confirms or corrects them.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from importlib import resources
from typing import Any

from torus.core.models import Alias, AliasType, EntityKind, RelationType
from torus.core.reference import ReferenceData
from torus.core.resolve import ResolutionStatus, Resolver
from torus.core.store.base import Backend

PACKAGE = "torus.data.starter"


@dataclass
class SeedReport:
    pack: str
    entities_created: int = 0
    entities_existing: int = 0
    relations_added: int = 0
    relations_kept: int = 0  # already present (from research or an earlier seed)
    notes: list[str] = field(default_factory=list)


def available_packs() -> dict[str, dict[str, Any]]:
    """name -> {title, description, version, entities, relations} for each pack."""
    packs = {}
    for entry in sorted(resources.files(PACKAGE).iterdir(), key=lambda e: e.name):
        if entry.name.endswith(".json"):
            data = json.loads(entry.read_text("utf-8"))
            packs[data["name"]] = {
                "title": data["title"],
                "description": data["description"],
                "version": data["version"],
                "entities": len(data["entities"]),
                "relations": len(data["relations"]),
            }
    return packs


def load_pack(name: str) -> dict[str, Any]:
    entry = resources.files(PACKAGE).joinpath(f"{name}.json")
    if not entry.is_file():
        raise ValueError(f"no starter pack {name!r}; available: {', '.join(available_packs())}")
    data: dict[str, Any] = json.loads(entry.read_text("utf-8"))
    return data


def seed(store: Backend, name: str, reference: ReferenceData | None = None) -> SeedReport:
    """Add a starter pack's entities and links. Safe to run repeatedly."""
    pack = load_pack(name)
    source = f"starter:{pack['name']}@{pack['version']}"
    report = SeedReport(pack=source)
    resolver = Resolver(store, reference)
    ids: dict[str, str] = {}

    with store.transaction():
        for item in pack["entities"]:
            aliases = [Alias(alias=a, alias_type=AliasType.NAME) for a in item.get("aliases", [])]
            entity = None
            if "ticker" in item:
                result = resolver.resolve(item["ticker"], create=True)
                if result.status is ResolutionStatus.RESOLVED and result.entity is not None:
                    entity = result.entity
                    created = result.method.endswith("+created")
                elif result.status is ResolutionStatus.AMBIGUOUS:
                    report.notes.append(f"{item['ticker']} is ambiguous; using pack identity")
                if entity is None:
                    key = f"ticker:{item['ticker']}"
                    created = store.get_entity_by_key(key) is None
                    aliases.append(Alias(alias=item["ticker"], alias_type=AliasType.TICKER))
                    entity = store.upsert_entity(
                        EntityKind.COMPANY, item["name"], canonical_key=key, aliases=aliases
                    )
                else:
                    for alias in aliases:
                        store.add_alias(entity.id, alias.alias, alias.alias_type)
            else:
                created = store.get_entity_by_key(item["key"]) is None
                entity = store.upsert_entity(
                    EntityKind.COMPANY, item["name"], canonical_key=item["key"], aliases=aliases
                )
            ids[item["ref"]] = entity.id
            if created:
                report.entities_created += 1
            else:
                report.entities_existing += 1

        for src, rel, dst, confidence, _note in pack["relations"]:
            src_id, dst_id = ids[src], ids[dst]
            rel_type = RelationType(rel)
            existing = [
                r
                for r in store.relations(src_id, direction="out", rel=rel_type)
                if r.dst_id == dst_id
            ]
            if existing:
                report.relations_kept += 1
                continue
            store.record_relation(src_id, rel_type, dst_id, float(confidence), source=source)
            report.relations_added += 1
    return report
