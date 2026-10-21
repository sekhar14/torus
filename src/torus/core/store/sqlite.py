"""SQLite implementation of the Torus store (the default "lite" backend).

Temporal rules:

* Claims and relations are never edited in place. A correction closes the old row
  (``superseded_at``) and inserts a new row in the same lineage.
* ``as_of=t`` selects rows with ``recorded_at <= t < superseded_at`` (open-ended
  when ``superseded_at`` is NULL): what Torus believed at ``t``.
* ``valid_at=t`` selects rows whose world-validity window contains ``t``.
* Thesis status/confidence changes are appended to ``thesis_history``.
"""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from datetime import datetime, timedelta
from enum import Enum
from pathlib import Path
from types import TracebackType
from typing import Any, Final

from torus.core.clock import Clock, SystemClock, from_ts, from_ts_opt, to_ts, to_ts_opt
from torus.core.errors import (
    EvidenceRequiredError,
    InvalidTransitionError,
    NotCurrentError,
    NotFoundError,
)
from torus.core.ids import IdGenerator
from torus.core.models import (
    EVIDENCED_CLAIM_TYPES,
    THESIS_TRANSITIONS,
    Alert,
    AlertStatus,
    Alias,
    AliasType,
    Claim,
    ClaimEntity,
    ClaimEntityRole,
    ClaimEvidence,
    ClaimStatus,
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
    Support,
    Thesis,
    ThesisClaim,
    ThesisClaimRole,
    ThesisHistoryEntry,
    ThesisStatus,
    ThesisTrigger,
    Verdict,
)
from torus.core.store import migrate as migrations
from torus.core.text import name_key, normalize_alias

__all__ = ["UNSET", "WORKSPACE", "SQLiteBackend", "normalize_alias"]

WORKSPACE: Final = "default"
MAX_ATTEMPTS: Final = 3


class _Unset(Enum):
    UNSET = "UNSET"


UNSET: Final = _Unset.UNSET
"""Sentinel for "leave unchanged" where ``None`` is a meaningful value."""

COUNTED_TABLES: Final = (
    "entities",
    "evidence",
    "claims",
    "relations",
    "theses",
    "thesis_history",
    "rejected_hypotheses",
    "sessions",
    "source_candidates",
    "queue",
    "events",
    "alerts",
)

_NON_WORD = re.compile(r"[^\w]+", re.UNICODE)


# Question words dropped from any-term queries ("what do we know about HBM supply").
_QUERY_STOPWORDS = frozenset(
    [
        "a",
        "an",
        "and",
        "are",
        "as",
        "at",
        "be",
        "by",
        "can",
        "did",
        "do",
        "does",
        "for",
        "from",
        "has",
        "have",
        "how",
        "i",
        "in",
        "is",
        "it",
        "its",
        "me",
        "my",
        "of",
        "on",
        "or",
        "our",
        "should",
        "so",
        "than",
        "that",
        "the",
        "their",
        "them",
        "there",
        "these",
        "this",
        "to",
        "was",
        "we",
        "what",
        "when",
        "where",
        "which",
        "who",
        "why",
        "will",
        "with",
        "would",
        "you",
        "your",
        "about",
        "know",
        "tell",
        "think",
        "any",
    ]
)


def _fts_query(text: str, match_any: bool = False) -> str:
    """Quote every token so user text can't inject FTS5 syntax.

    Tokens are ANDed by default. With ``match_any`` they are ORed (BM25 still
    ranks documents matching more terms higher) and question words are dropped.
    """
    tokens = [t for t in _NON_WORD.split(text) if t]
    if match_any:
        tokens = [t for t in tokens if t.casefold() not in _QUERY_STOPWORDS]
        return " OR ".join(f'"{t}"' for t in tokens)
    return " ".join(f'"{t}"' for t in tokens)


def _temporal_clause(
    alias: str, as_of: datetime | None, valid_at: datetime | None, params: list[Any]
) -> str:
    clauses = []
    if as_of is None:
        clauses.append(f"{alias}.superseded_at IS NULL")
    else:
        ts = to_ts(as_of)
        clauses.append(
            f"{alias}.recorded_at <= ? AND ({alias}.superseded_at IS NULL "
            f"OR {alias}.superseded_at > ?)"
        )
        params += [ts, ts]
    if valid_at is not None:
        ts = to_ts(valid_at)
        clauses.append(
            f"({alias}.valid_from IS NULL OR {alias}.valid_from <= ?) "
            f"AND ({alias}.valid_to IS NULL OR {alias}.valid_to > ?)"
        )
        params += [ts, ts]
    return " AND ".join(clauses)


def _placeholders(n: int) -> str:
    return ",".join("?" * n)


class SQLiteBackend:
    def __init__(
        self,
        conn: sqlite3.Connection,
        clock: Clock | None = None,
        ids: IdGenerator | None = None,
    ) -> None:
        self._conn = conn
        self._clock = clock or SystemClock()
        self._ids = ids or IdGenerator(self._clock)
        self._tx_depth = 0
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("PRAGMA busy_timeout = 5000")
        migrations.migrate(conn)

    @classmethod
    def open(
        cls,
        path: str | Path,
        clock: Clock | None = None,
        ids: IdGenerator | None = None,
    ) -> SQLiteBackend:
        """Open (and migrate) a database file, or ``":memory:"``."""
        conn = sqlite3.connect(str(path), isolation_level=None, check_same_thread=False)
        if str(path) != ":memory:":
            conn.execute("PRAGMA journal_mode = WAL")
            conn.execute("PRAGMA synchronous = NORMAL")
        return cls(conn, clock=clock, ids=ids)

    def close(self) -> None:
        self._conn.close()

    def __enter__(self) -> SQLiteBackend:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.close()

    # -- infrastructure -----------------------------------------------------

    @contextmanager
    def transaction(self) -> Iterator[None]:
        """Atomic unit of work. Nested calls join the outer transaction."""
        if self._tx_depth == 0:
            self._conn.execute("BEGIN IMMEDIATE")
        self._tx_depth += 1
        try:
            yield
        except BaseException:
            self._tx_depth -= 1
            if self._tx_depth == 0:
                self._conn.execute("ROLLBACK")
            raise
        self._tx_depth -= 1
        if self._tx_depth == 0:
            self._conn.execute("COMMIT")

    def _now(self) -> str:
        return to_ts(self._clock.now())

    def _one(self, sql: str, params: Sequence[Any] = ()) -> sqlite3.Row | None:
        row: sqlite3.Row | None = self._conn.execute(sql, params).fetchone()
        return row

    def _all(self, sql: str, params: Sequence[Any] = ()) -> list[sqlite3.Row]:
        return list(self._conn.execute(sql, params).fetchall())

    def schema_version(self) -> int:
        return migrations.current_version(self._conn)

    def counts(self) -> dict[str, int]:
        return {
            table: int(self._conn.execute(f"SELECT count(*) FROM {table}").fetchone()[0])
            for table in COUNTED_TABLES
        }

    # -- sessions -----------------------------------------------------------

    def start_session(
        self,
        harness: str,
        external_session_id: str | None = None,
        transcript_path: str | None = None,
    ) -> Session:
        """Create a session, or return the existing one for (harness, external id)."""
        with self.transaction():
            if external_session_id is not None:
                row = self._one(
                    "SELECT * FROM sessions WHERE harness = ? AND external_session_id = ?",
                    (harness, external_session_id),
                )
                if row:
                    if transcript_path and row["transcript_path"] != transcript_path:
                        self._conn.execute(
                            "UPDATE sessions SET transcript_path = ? WHERE id = ?",
                            (transcript_path, row["id"]),
                        )
                    return self.get_session(row["id"])
            session_id = self._ids.new()
            self._conn.execute(
                "INSERT INTO sessions (id, harness, external_session_id, transcript_path,"
                " started_at) VALUES (?, ?, ?, ?, ?)",
                (session_id, harness, external_session_id, transcript_path, self._now()),
            )
            return self.get_session(session_id)

    def end_session(self, session_id: str) -> Session:
        self.get_session(session_id)
        self._conn.execute(
            "UPDATE sessions SET ended_at = ? WHERE id = ?", (self._now(), session_id)
        )
        return self.get_session(session_id)

    def get_session(self, session_id: str) -> Session:
        row = self._one("SELECT * FROM sessions WHERE id = ?", (session_id,))
        if row is None:
            raise NotFoundError("session", session_id)
        return Session(
            id=row["id"],
            harness=row["harness"],
            external_session_id=row["external_session_id"],
            transcript_path=row["transcript_path"],
            started_at=from_ts(row["started_at"]),
            ended_at=from_ts_opt(row["ended_at"]),
        )

    # -- entities -----------------------------------------------------------

    def upsert_entity(
        self,
        kind: EntityKind,
        name: str,
        canonical_key: str | None = None,
        aliases: Sequence[Alias] = (),
        attrs: dict[str, Any] | None = None,
    ) -> Entity:
        """Create an entity, or merge into the one with the same ``canonical_key``.

        Merging unions aliases and overlays ``attrs``; the existing name and kind stay.
        The name is always registered as a NAME alias.
        """
        now = self._now()
        with self.transaction():
            row = None
            if canonical_key is not None:
                row = self._one(
                    "SELECT * FROM entities WHERE workspace = ? AND canonical_key = ?",
                    (WORKSPACE, canonical_key),
                )
            if row is None:
                entity_id = self._ids.new()
                self._conn.execute(
                    "INSERT INTO entities (id, kind, name, canonical_key, attrs_json,"
                    " created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (
                        entity_id,
                        EntityKind(kind).value,
                        name,
                        canonical_key,
                        json.dumps(attrs or {}, sort_keys=True),
                        now,
                        now,
                    ),
                )
            else:
                entity_id = row["id"]
                merged = {**json.loads(row["attrs_json"]), **(attrs or {})}
                self._conn.execute(
                    "UPDATE entities SET attrs_json = ?, updated_at = ? WHERE id = ?",
                    (json.dumps(merged, sort_keys=True), now, entity_id),
                )
            for alias in (Alias(alias=name, alias_type=AliasType.NAME), *aliases):
                self._insert_alias(entity_id, alias)
            return self.get_entity(entity_id)

    def add_alias(self, entity_id: str, alias: str, alias_type: AliasType) -> Entity:
        with self.transaction():
            self.get_entity(entity_id)
            self._insert_alias(entity_id, Alias(alias=alias, alias_type=alias_type))
            return self.get_entity(entity_id)

    def _insert_alias(self, entity_id: str, alias: Alias) -> None:
        norm = normalize_alias(alias.alias)
        if not norm:
            return
        self._conn.execute(
            "INSERT OR IGNORE INTO entity_aliases"
            " (entity_id, alias, alias_norm, alias_key, alias_type) VALUES (?, ?, ?, ?, ?)",
            (
                entity_id,
                alias.alias,
                norm,
                name_key(alias.alias),
                AliasType(alias.alias_type).value,
            ),
        )

    def get_entity(self, entity_id: str) -> Entity:
        """Fetch an entity. Ids of merged-away entities resolve to the survivor."""
        live_id = self.live_entity_id(entity_id)
        return self._hydrate_entities(self._all("SELECT * FROM entities WHERE id = ?", (live_id,)))[
            0
        ]

    def live_entity_id(self, entity_id: str) -> str:
        """Follow merge redirects to the surviving entity id; NotFoundError if unknown."""
        current = entity_id
        for _ in range(64):  # merge chains are short; guard against cycles
            if self._one("SELECT 1 FROM entities WHERE id = ?", (current,)):
                return current
            row = self._one("SELECT into_id FROM entity_merges WHERE merged_id = ?", (current,))
            if row is None:
                break
            current = row["into_id"]
        raise NotFoundError("entity", entity_id)

    def _live_or_same(self, entity_id: str) -> str:
        try:
            return self.live_entity_id(entity_id)
        except NotFoundError:
            return entity_id

    def get_entity_by_key(self, canonical_key: str) -> Entity | None:
        rows = self._all(
            "SELECT * FROM entities WHERE workspace = ? AND canonical_key = ?",
            (WORKSPACE, canonical_key),
        )
        found = self._hydrate_entities(rows)
        return found[0] if found else None

    def find_entities_by_alias(
        self, alias: str, alias_types: Sequence[AliasType] = ()
    ) -> list[Entity]:
        """Entities with an alias equal to ``alias`` after ``normalize_alias``."""
        return self._find_by_alias_column("alias_norm", normalize_alias(alias), alias_types)

    def find_entities_by_name_key(
        self, name: str, alias_types: Sequence[AliasType] = ()
    ) -> list[Entity]:
        """Entities whose alias matches ``name`` ignoring legal suffixes (``name_key``)."""
        return self._find_by_alias_column("alias_key", name_key(name), alias_types)

    def _find_by_alias_column(
        self, column: str, value: str, alias_types: Sequence[AliasType]
    ) -> list[Entity]:
        if not value:
            return []
        params: list[Any] = [WORKSPACE, value]
        sql = (
            "SELECT DISTINCT e.* FROM entities e JOIN entity_aliases a ON a.entity_id = e.id"
            f" WHERE e.workspace = ? AND a.{column} = ?"
        )
        if alias_types:
            sql += f" AND a.alias_type IN ({_placeholders(len(alias_types))})"
            params += [AliasType(t).value for t in alias_types]
        return self._hydrate_entities(self._all(sql + " ORDER BY e.id", params))

    def all_aliases(self) -> list[tuple[str, str, AliasType]]:
        """Every (entity_id, alias, alias_type): input for the detector's index."""
        rows = self._all(
            "SELECT a.entity_id, a.alias, a.alias_type FROM entity_aliases a"
            " JOIN entities e ON e.id = a.entity_id WHERE e.workspace = ?"
            " ORDER BY a.entity_id, a.alias",
            (WORKSPACE,),
        )
        return [(r["entity_id"], r["alias"], AliasType(r["alias_type"])) for r in rows]

    def entity_index(self) -> dict[str, tuple[str | None, str]]:
        """Every entity id -> (canonical_key, name), in one query."""
        rows = self._all(
            "SELECT id, canonical_key, name FROM entities WHERE workspace = ?", (WORKSPACE,)
        )
        return {r["id"]: (r["canonical_key"], r["name"]) for r in rows}

    def merge_entities(self, keep_id: str, merge_id: str, reason: str) -> Entity:
        """Fold ``merge_id`` into ``keep_id`` (two records of the same thing).

        Aliases, claim links, relations, thesis subjects/triggers and rejected
        hypotheses move to the survivor; the old id keeps resolving via a redirect.
        Relations that become self-loops are closed. A merge corrects identity, so
        links are repointed in place rather than versioned.
        """
        with self.transaction():
            keep = self.get_entity(keep_id)
            merge = self.get_entity(merge_id)
            if keep.id == merge.id:
                raise ValueError("cannot merge an entity into itself")
            k, m = keep.id, merge.id
            now = self._now()
            self._conn.execute(
                "INSERT OR IGNORE INTO entity_aliases"
                " (entity_id, alias, alias_norm, alias_key, alias_type)"
                " SELECT ?, alias, alias_norm, alias_key, alias_type"
                " FROM entity_aliases WHERE entity_id = ?",
                (k, m),
            )
            for table in ("claim_entities", "rejected_hypothesis_entities"):
                self._conn.execute(
                    f"UPDATE OR IGNORE {table} SET entity_id = ? WHERE entity_id = ?", (k, m)
                )
                self._conn.execute(f"DELETE FROM {table} WHERE entity_id = ?", (m,))
            self._conn.execute("UPDATE relations SET src_id = ? WHERE src_id = ?", (k, m))
            self._conn.execute("UPDATE relations SET dst_id = ? WHERE dst_id = ?", (k, m))
            self._conn.execute(
                "UPDATE relations SET superseded_at = ?"
                " WHERE src_id = dst_id AND src_id = ? AND superseded_at IS NULL",
                (now, k),
            )
            self._conn.execute(
                "UPDATE theses SET subject_entity_id = ? WHERE subject_entity_id = ?", (k, m)
            )
            self._conn.execute(
                "UPDATE thesis_triggers SET entity_id = ? WHERE entity_id = ?", (k, m)
            )
            self._conn.execute("UPDATE entity_merges SET into_id = ? WHERE into_id = ?", (k, m))
            self._conn.execute("DELETE FROM entity_aliases WHERE entity_id = ?", (m,))
            self._conn.execute("DELETE FROM entities WHERE id = ?", (m,))
            self._conn.execute(
                "UPDATE entities SET attrs_json = ?, updated_at = ?,"
                " canonical_key = coalesce(canonical_key, ?) WHERE id = ?",
                (
                    json.dumps({**merge.attrs, **keep.attrs}, sort_keys=True),
                    now,
                    merge.canonical_key,
                    k,
                ),
            )
            self._conn.execute(
                "INSERT INTO entity_merges (merged_id, into_id, reason, at) VALUES (?, ?, ?, ?)",
                (m, k, reason, now),
            )
            return self.get_entity(k)

    def _hydrate_entities(self, rows: list[sqlite3.Row]) -> list[Entity]:
        if not rows:
            return []
        ids = [r["id"] for r in rows]
        aliases: dict[str, list[Alias]] = {i: [] for i in ids}
        for a in self._all(
            f"SELECT * FROM entity_aliases WHERE entity_id IN ({_placeholders(len(ids))})"
            " ORDER BY alias_type, alias",
            ids,
        ):
            aliases[a["entity_id"]].append(
                Alias(alias=a["alias"], alias_type=AliasType(a["alias_type"]))
            )
        return [
            Entity(
                id=r["id"],
                kind=EntityKind(r["kind"]),
                name=r["name"],
                canonical_key=r["canonical_key"],
                attrs=json.loads(r["attrs_json"]),
                aliases=tuple(aliases[r["id"]]),
                created_at=from_ts(r["created_at"]),
                updated_at=from_ts(r["updated_at"]),
            )
            for r in rows
        ]

    # -- evidence -----------------------------------------------------------

    def record_evidence(
        self,
        uri: str,
        kind: EvidenceKind = EvidenceKind.WEB,
        title: str | None = None,
        publisher: str | None = None,
        published_at: datetime | None = None,
        retrieved_at: datetime | None = None,
        quote: str | None = None,
        content_hash: str | None = None,
        source_path: str | None = None,
    ) -> Evidence:
        """Record a source. The same (uri, content_hash) returns the existing record."""
        with self.transaction():
            row = self._one(
                "SELECT id FROM evidence WHERE workspace = ? AND uri = ?"
                " AND coalesce(content_hash, '') = ?",
                (WORKSPACE, uri, content_hash or ""),
            )
            if row:
                self._link_candidates(uri, row["id"])
                return self.get_evidence(row["id"])
            evidence_id = self._ids.new()
            now = self._now()
            self._conn.execute(
                "INSERT INTO evidence (id, kind, uri, title, publisher, published_at,"
                " retrieved_at, quote, content_hash, source_path, recorded_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    evidence_id,
                    EvidenceKind(kind).value,
                    uri,
                    title,
                    publisher,
                    to_ts_opt(published_at),
                    to_ts(retrieved_at) if retrieved_at else now,
                    quote,
                    content_hash,
                    source_path,
                    now,
                ),
            )
            self._link_candidates(uri, evidence_id)
            return self.get_evidence(evidence_id)

    def _link_candidates(self, uri: str, evidence_id: str) -> None:
        """Source candidates for this URI are now backed by recorded evidence."""
        self._conn.execute(
            "UPDATE source_candidates SET linked_evidence_id = ?"
            " WHERE uri = ? AND linked_evidence_id IS NULL",
            (evidence_id, uri),
        )

    def get_evidence(self, evidence_id: str) -> Evidence:
        r = self._one("SELECT * FROM evidence WHERE id = ?", (evidence_id,))
        if r is None:
            raise NotFoundError("evidence", evidence_id)
        return Evidence(
            id=r["id"],
            kind=EvidenceKind(r["kind"]),
            uri=r["uri"],
            title=r["title"],
            publisher=r["publisher"],
            published_at=from_ts_opt(r["published_at"]),
            retrieved_at=from_ts(r["retrieved_at"]),
            quote=r["quote"],
            content_hash=r["content_hash"],
            source_path=r["source_path"],
            recorded_at=from_ts(r["recorded_at"]),
        )

    # -- claims -------------------------------------------------------------

    def record_claim(
        self,
        text: str,
        claim_type: ClaimType,
        confidence: float,
        entities: Sequence[str | ClaimEntity] = (),
        evidence: Sequence[str | ClaimEvidence] = (),
        derived_from: Sequence[str] = (),
        valid_from: datetime | None = None,
        valid_to: datetime | None = None,
        session_id: str | None = None,
    ) -> Claim:
        """Record a new claim lineage.

        ``entities`` given as bare ids are subjects; ``evidence`` given as bare ids
        support the claim. Facts and metrics need supporting evidence; inferences
        need supporting evidence or ``derived_from`` claims.
        """
        with self.transaction():
            claim_id = self._ids.new()
            self._insert_claim(
                claim_id=claim_id,
                lineage_id=claim_id,
                supersedes_id=None,
                text=text,
                claim_type=ClaimType(claim_type),
                confidence=confidence,
                entities=[_as_claim_entity(e) for e in entities],
                evidence=[_as_claim_evidence(e) for e in evidence],
                derived_from=list(derived_from),
                valid_from=to_ts_opt(valid_from),
                valid_to=to_ts_opt(valid_to),
                session_id=session_id,
            )
            return self.get_claim(claim_id)

    def supersede_claim(
        self,
        claim_id: str,
        reason: str,
        text: str | None = None,
        claim_type: ClaimType | None = None,
        confidence: float | None = None,
        entities: Sequence[str | ClaimEntity] | None = None,
        evidence: Sequence[str | ClaimEvidence] | None = None,
        derived_from: Sequence[str] | None = None,
        valid_from: datetime | _Unset | None = UNSET,
        valid_to: datetime | _Unset | None = UNSET,
        session_id: str | None = None,
    ) -> Claim:
        """Replace the current version of a claim. Unspecified fields carry over."""
        with self.transaction():
            old = self._require_current_claim(claim_id)
            new_id = self._ids.new()
            self._close_claim(old.id, ClaimStatus.SUPERSEDED, reason)
            self._insert_claim(
                claim_id=new_id,
                lineage_id=old.lineage_id,
                supersedes_id=old.id,
                text=text if text is not None else old.text,
                claim_type=ClaimType(claim_type) if claim_type else old.claim_type,
                confidence=confidence if confidence is not None else old.confidence,
                entities=(
                    [_as_claim_entity(e) for e in entities]
                    if entities is not None
                    else list(old.entities)
                ),
                evidence=(
                    [_as_claim_evidence(e) for e in evidence]
                    if evidence is not None
                    else list(old.evidence)
                ),
                derived_from=(
                    list(derived_from) if derived_from is not None else list(old.derived_from)
                ),
                valid_from=(
                    to_ts_opt(old.valid_from) if valid_from is UNSET else to_ts_opt(valid_from)
                ),
                valid_to=to_ts_opt(old.valid_to) if valid_to is UNSET else to_ts_opt(valid_to),
                session_id=session_id if session_id is not None else old.session_id,
            )
            return self.get_claim(new_id)

    def retract_claim(self, claim_id: str, reason: str) -> Claim:
        """Withdraw a claim with no replacement."""
        with self.transaction():
            old = self._require_current_claim(claim_id)
            self._close_claim(old.id, ClaimStatus.RETRACTED, reason)
            return self.get_claim(old.id)

    def _require_current_claim(self, claim_id: str) -> Claim:
        claim = self.get_claim(claim_id)
        if not claim.is_current:
            raise NotCurrentError(
                f"claim {claim_id} is {claim.status.value}; use the current version"
            )
        return claim

    def _close_claim(self, claim_id: str, status: ClaimStatus, reason: str) -> None:
        self._conn.execute(
            "UPDATE claims SET superseded_at = ?, status = ?, change_reason = ? WHERE id = ?",
            (self._now(), status.value, reason, claim_id),
        )

    def _insert_claim(
        self,
        *,
        claim_id: str,
        lineage_id: str,
        supersedes_id: str | None,
        text: str,
        claim_type: ClaimType,
        confidence: float,
        entities: list[ClaimEntity],
        evidence: list[ClaimEvidence],
        derived_from: list[str],
        valid_from: str | None,
        valid_to: str | None,
        session_id: str | None,
    ) -> None:
        if not text.strip():
            raise ValueError("claim text must not be empty")
        if not 0.0 <= confidence <= 1.0:
            raise ValueError(f"confidence must be in [0, 1], got {confidence}")
        if valid_from and valid_to and valid_to <= valid_from:
            raise ValueError("valid_to must be after valid_from")
        supporting = [e for e in evidence if e.support is Support.SUPPORTS]
        if claim_type in EVIDENCED_CLAIM_TYPES and not supporting:
            raise EvidenceRequiredError(
                f"a {claim_type.value} claim needs at least one supporting evidence id"
            )
        if claim_type is ClaimType.INFERENCE and not supporting and not derived_from:
            raise EvidenceRequiredError(
                "an inference needs supporting evidence or derived_from claim ids"
            )
        entities = [
            ClaimEntity(entity_id=self.live_entity_id(ce.entity_id), role=ce.role)
            for ce in entities
        ]
        for ev in evidence:
            self.get_evidence(ev.evidence_id)
        for parent in derived_from:
            self.get_claim(parent)
        if session_id is not None:
            self.get_session(session_id)

        self._conn.execute(
            "INSERT INTO claims (id, lineage_id, supersedes_id, text, claim_type, confidence,"
            " status, valid_from, valid_to, recorded_at, session_id)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                claim_id,
                lineage_id,
                supersedes_id,
                text,
                claim_type.value,
                confidence,
                ClaimStatus.ACTIVE.value,
                valid_from,
                valid_to,
                self._now(),
                session_id,
            ),
        )
        self._conn.executemany(
            "INSERT OR IGNORE INTO claim_entities (claim_id, entity_id, role) VALUES (?, ?, ?)",
            [(claim_id, ce.entity_id, ce.role.value) for ce in entities],
        )
        self._conn.executemany(
            "INSERT OR REPLACE INTO claim_evidence (claim_id, evidence_id, support)"
            " VALUES (?, ?, ?)",
            [(claim_id, ev.evidence_id, ev.support.value) for ev in evidence],
        )
        self._conn.executemany(
            "INSERT OR IGNORE INTO claim_derivations (claim_id, from_claim_id) VALUES (?, ?)",
            [(claim_id, parent) for parent in derived_from],
        )

    def get_claim(self, claim_id: str) -> Claim:
        claims = self._hydrate_claims(self._all("SELECT * FROM claims WHERE id = ?", (claim_id,)))
        if not claims:
            raise NotFoundError("claim", claim_id)
        return claims[0]

    def current_claim(self, lineage_id: str, as_of: datetime | None = None) -> Claim | None:
        """The version of a lineage believed now (or at ``as_of``)."""
        params: list[Any] = [lineage_id]
        where = _temporal_clause("c", as_of, None, params)
        rows = self._all(
            f"SELECT c.* FROM claims c WHERE c.lineage_id = ? AND {where}"
            " ORDER BY c.recorded_at DESC, c.id DESC LIMIT 1",
            params,
        )
        found = self._hydrate_claims(rows)
        return found[0] if found else None

    def claim_history(self, lineage_id: str) -> list[Claim]:
        """Every version of a claim lineage, oldest first."""
        return self._hydrate_claims(
            self._all(
                "SELECT * FROM claims WHERE lineage_id = ? ORDER BY recorded_at, id",
                (lineage_id,),
            )
        )

    def claims(
        self,
        entity_id: str | None = None,
        as_of: datetime | None = None,
        valid_at: datetime | None = None,
        claim_types: Sequence[ClaimType] = (),
        limit: int | None = None,
    ) -> list[Claim]:
        """Claims believed now (or at ``as_of``), newest first."""
        sql, params = self._claims_from(entity_id)
        if claim_types:
            sql += f" AND c.claim_type IN ({_placeholders(len(claim_types))})"
            params += [ClaimType(t).value for t in claim_types]
        sql += " AND " + _temporal_clause("c", as_of, valid_at, params)
        sql += " ORDER BY c.recorded_at DESC, c.id DESC"
        if limit is not None:
            sql += " LIMIT ?"
            params.append(limit)
        return self._hydrate_claims(self._all(sql, params))

    def _claims_from(self, entity_id: str | None) -> tuple[str, list[Any]]:
        """``SELECT c.* ... WHERE`` prefix, optionally limited to one entity's claims.

        For an entity, the scan starts from its links (CROSS JOIN fixes the join
        order); otherwise SQLite would walk every current claim and probe the links.
        """
        if entity_id is None:
            return "SELECT c.* FROM claims c WHERE c.workspace = ?", [WORKSPACE]
        return (
            "SELECT c.* FROM (SELECT DISTINCT claim_id FROM claim_entities WHERE entity_id = ?)"
            " ce CROSS JOIN claims c ON c.id = ce.claim_id WHERE c.workspace = ?",
            [self._live_or_same(entity_id), WORKSPACE],
        )

    def get_claims(self, claim_ids: Sequence[str]) -> list[Claim]:
        """Claims by id (any version), in the order given; unknown ids are skipped."""
        if not claim_ids:
            return []
        rows = self._all(
            f"SELECT * FROM claims WHERE id IN ({_placeholders(len(claim_ids))})", list(claim_ids)
        )
        by_id = {c.id: c for c in self._hydrate_claims(rows)}
        return [by_id[i] for i in claim_ids if i in by_id]

    def claim_versions(
        self,
        entity_id: str | None = None,
        since: datetime | None = None,
        until: datetime | None = None,
    ) -> list[Claim]:
        """Claim rows recorded or closed within [since, until), oldest first.

        Includes superseded and retracted versions: this is the raw change log
        behind ``timeline`` and ``what_changed``.
        """
        sql, params = self._claims_from(entity_id)
        lo, hi = to_ts_opt(since), to_ts_opt(until)
        windows = []
        for column in ("c.recorded_at", "c.superseded_at"):
            parts = [f"{column} IS NOT NULL"]
            if lo:
                parts.append(f"{column} >= ?")
                params.append(lo)
            if hi:
                parts.append(f"{column} < ?")
                params.append(hi)
            windows.append("(" + " AND ".join(parts) + ")")
        sql += " AND (" + " OR ".join(windows) + ") ORDER BY c.recorded_at, c.id"
        return self._hydrate_claims(self._all(sql, params))

    def _hydrate_claims(self, rows: list[sqlite3.Row]) -> list[Claim]:
        if not rows:
            return []
        ids = [r["id"] for r in rows]
        marks = _placeholders(len(ids))
        ents: dict[str, list[ClaimEntity]] = {i: [] for i in ids}
        for r in self._all(
            f"SELECT * FROM claim_entities WHERE claim_id IN ({marks}) ORDER BY role, entity_id",
            ids,
        ):
            ents[r["claim_id"]].append(
                ClaimEntity(entity_id=r["entity_id"], role=ClaimEntityRole(r["role"]))
            )
        evs: dict[str, list[ClaimEvidence]] = {i: [] for i in ids}
        for r in self._all(
            f"SELECT * FROM claim_evidence WHERE claim_id IN ({marks}) ORDER BY evidence_id", ids
        ):
            evs[r["claim_id"]].append(
                ClaimEvidence(evidence_id=r["evidence_id"], support=Support(r["support"]))
            )
        ders: dict[str, list[str]] = {i: [] for i in ids}
        for r in self._all(
            f"SELECT * FROM claim_derivations WHERE claim_id IN ({marks}) ORDER BY from_claim_id",
            ids,
        ):
            ders[r["claim_id"]].append(r["from_claim_id"])
        return [
            Claim(
                id=r["id"],
                lineage_id=r["lineage_id"],
                supersedes_id=r["supersedes_id"],
                text=r["text"],
                claim_type=ClaimType(r["claim_type"]),
                confidence=r["confidence"],
                status=ClaimStatus(r["status"]),
                valid_from=from_ts_opt(r["valid_from"]),
                valid_to=from_ts_opt(r["valid_to"]),
                recorded_at=from_ts(r["recorded_at"]),
                superseded_at=from_ts_opt(r["superseded_at"]),
                change_reason=r["change_reason"],
                session_id=r["session_id"],
                entities=tuple(ents[r["id"]]),
                evidence=tuple(evs[r["id"]]),
                derived_from=tuple(ders[r["id"]]),
            )
            for r in rows
        ]

    # -- relations ----------------------------------------------------------

    def record_relation(
        self,
        src_id: str,
        rel: RelationType,
        dst_id: str,
        confidence: float,
        claim_id: str | None = None,
        valid_from: datetime | None = None,
        valid_to: datetime | None = None,
        source: str = "agent",
    ) -> Relation:
        """Record a link between entities.

        If the same (source, relation, target) link is already current, it is
        revised instead of duplicated: unchanged values return it as is, changed
        values supersede it. ``None`` arguments mean "keep what is there".
        """
        with self.transaction():
            src_id = self.live_entity_id(src_id)
            dst_id = self.live_entity_id(dst_id)
            existing = self._one(
                "SELECT id FROM relations WHERE workspace = ? AND src_id = ? AND rel = ?"
                " AND dst_id = ? AND superseded_at IS NULL ORDER BY recorded_at DESC LIMIT 1",
                (WORKSPACE, src_id, RelationType(rel).value, dst_id),
            )
            if existing:
                old = self.get_relation(existing["id"])
                changed = (
                    confidence != old.confidence
                    or (claim_id is not None and claim_id != old.claim_id)
                    or (valid_from is not None and valid_from != old.valid_from)
                    or (valid_to is not None and valid_to != old.valid_to)
                    or source != old.source
                )
                if not changed:
                    return old
                return self.supersede_relation(
                    old.id,
                    confidence=confidence,
                    claim_id=UNSET if claim_id is None else claim_id,
                    valid_from=UNSET if valid_from is None else valid_from,
                    valid_to=UNSET if valid_to is None else valid_to,
                    source=source,
                )
            relation_id = self._ids.new()
            self._insert_relation(
                relation_id=relation_id,
                lineage_id=relation_id,
                supersedes_id=None,
                src_id=src_id,
                rel=RelationType(rel),
                dst_id=dst_id,
                confidence=confidence,
                claim_id=claim_id,
                valid_from=to_ts_opt(valid_from),
                valid_to=to_ts_opt(valid_to),
                source=source,
            )
            return self.get_relation(relation_id)

    def supersede_relation(
        self,
        relation_id: str,
        confidence: float | None = None,
        claim_id: str | _Unset | None = UNSET,
        valid_from: datetime | _Unset | None = UNSET,
        valid_to: datetime | _Unset | None = UNSET,
        source: str | None = None,
    ) -> Relation:
        """Revise a relation, e.g. set ``valid_to`` when a supply contract ends."""
        with self.transaction():
            old = self.get_relation(relation_id)
            if old.superseded_at is not None:
                raise NotCurrentError(f"relation {relation_id} has been superseded")
            self._conn.execute(
                "UPDATE relations SET superseded_at = ? WHERE id = ?", (self._now(), old.id)
            )
            new_id = self._ids.new()
            self._insert_relation(
                relation_id=new_id,
                lineage_id=old.lineage_id,
                supersedes_id=old.id,
                src_id=old.src_id,
                rel=old.rel,
                dst_id=old.dst_id,
                confidence=confidence if confidence is not None else old.confidence,
                claim_id=old.claim_id if claim_id is UNSET else claim_id,
                valid_from=(
                    to_ts_opt(old.valid_from) if valid_from is UNSET else to_ts_opt(valid_from)
                ),
                valid_to=to_ts_opt(old.valid_to) if valid_to is UNSET else to_ts_opt(valid_to),
                source=source or old.source,
            )
            return self.get_relation(new_id)

    def _insert_relation(
        self,
        *,
        relation_id: str,
        lineage_id: str,
        supersedes_id: str | None,
        src_id: str,
        rel: RelationType,
        dst_id: str,
        confidence: float,
        claim_id: str | None,
        valid_from: str | None,
        valid_to: str | None,
        source: str = "agent",
    ) -> None:
        if not 0.0 <= confidence <= 1.0:
            raise ValueError(f"confidence must be in [0, 1], got {confidence}")
        if valid_from and valid_to and valid_to <= valid_from:
            raise ValueError("valid_to must be after valid_from")
        if not source.strip():
            raise ValueError("relation source must not be empty")
        src_id = self.live_entity_id(src_id)
        dst_id = self.live_entity_id(dst_id)
        if claim_id is not None:
            self.get_claim(claim_id)
        self._conn.execute(
            "INSERT INTO relations (id, lineage_id, supersedes_id, src_id, rel, dst_id,"
            " confidence, claim_id, valid_from, valid_to, recorded_at, source)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                relation_id,
                lineage_id,
                supersedes_id,
                src_id,
                rel.value,
                dst_id,
                confidence,
                claim_id,
                valid_from,
                valid_to,
                self._now(),
                source,
            ),
        )

    def get_relation(self, relation_id: str) -> Relation:
        row = self._one("SELECT * FROM relations WHERE id = ?", (relation_id,))
        if row is None:
            raise NotFoundError("relation", relation_id)
        return _relation(row)

    def relations(
        self,
        entity_id: str | None = None,
        direction: str = "both",
        rel: RelationType | None = None,
        as_of: datetime | None = None,
        valid_at: datetime | None = None,
    ) -> list[Relation]:
        """Relations touching ``entity_id`` (``out``, ``in`` or ``both``)."""
        params: list[Any] = [WORKSPACE]
        sql = "SELECT r.* FROM relations r WHERE r.workspace = ?"
        if entity_id is not None:
            entity_id = self._live_or_same(entity_id)
            if direction == "out":
                sql += " AND r.src_id = ?"
                params.append(entity_id)
            elif direction == "in":
                sql += " AND r.dst_id = ?"
                params.append(entity_id)
            elif direction == "both":
                sql += " AND (r.src_id = ? OR r.dst_id = ?)"
                params += [entity_id, entity_id]
            else:
                raise ValueError(f"direction must be out, in or both, got {direction!r}")
        if rel is not None:
            sql += " AND r.rel = ?"
            params.append(RelationType(rel).value)
        sql += " AND " + _temporal_clause("r", as_of, valid_at, params)
        sql += " ORDER BY r.recorded_at, r.id"
        return [_relation(row) for row in self._all(sql, params)]

    # -- theses -------------------------------------------------------------

    def record_thesis(
        self,
        title: str,
        statement: str,
        stance: Stance,
        confidence: float,
        horizon: str | None = None,
        subject_entity_id: str | None = None,
        assumptions: Sequence[str] = (),
        supporting: Sequence[str] = (),
        risks: Sequence[str] = (),
        triggers: Sequence[tuple[str, str | None]] = (),
        session_id: str | None = None,
    ) -> Thesis:
        """Record a thesis linked to claims by role.

        Claim ids may be any version; the thesis links to the claim *lineage*, so it
        follows later corrections. ``triggers`` are ``(description, entity_id)`` pairs.
        """
        if not 0.0 <= confidence <= 1.0:
            raise ValueError(f"confidence must be in [0, 1], got {confidence}")
        now = self._now()
        with self.transaction():
            if subject_entity_id is not None:
                subject_entity_id = self.live_entity_id(subject_entity_id)
            if session_id is not None:
                self.get_session(session_id)
            thesis_id = self._ids.new()
            self._conn.execute(
                "INSERT INTO theses (id, title, statement, stance, horizon, confidence, status,"
                " subject_entity_id, session_id, created_at, updated_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    thesis_id,
                    title,
                    statement,
                    Stance(stance).value,
                    horizon,
                    confidence,
                    ThesisStatus.ACTIVE.value,
                    subject_entity_id,
                    session_id,
                    now,
                    now,
                ),
            )
            for role, claim_ids in (
                (ThesisClaimRole.ASSUMPTION, assumptions),
                (ThesisClaimRole.SUPPORTING, supporting),
                (ThesisClaimRole.RISK, risks),
            ):
                for claim_id in claim_ids:
                    self._link_thesis_claim(thesis_id, claim_id, role)
            for description, trigger_entity in triggers:
                self._add_trigger(thesis_id, description, trigger_entity)
            self._append_history(
                thesis_id, ThesisStatus.ACTIVE, confidence, "created", alert_id=None
            )
            return self.get_thesis(thesis_id)

    def link_thesis_claim(
        self, thesis_id: str, claim_id: str, role: ThesisClaimRole, weight: float = 1.0
    ) -> Thesis:
        return self.revise_thesis(
            thesis_id,
            reason=f"linked claim {claim_id} as {ThesisClaimRole(role).value}",
            add_claims=[(claim_id, role, weight)],
        )

    def _link_thesis_claim(
        self, thesis_id: str, claim_id: str, role: ThesisClaimRole, weight: float = 1.0
    ) -> bool:
        """Link a claim lineage; returns False if an identical link is already current."""
        claim = self.get_claim(claim_id)
        if self.current_claim(claim.lineage_id) is None:
            raise NotCurrentError(f"claim {claim_id} has been retracted")
        role = ThesisClaimRole(role)
        current = self._one(
            "SELECT id, role, weight FROM thesis_claims WHERE thesis_id = ? AND claim_id = ?"
            " AND unlinked_at IS NULL",
            (thesis_id, claim.lineage_id),
        )
        if current and current["role"] == role.value and current["weight"] == weight:
            return False
        now = self._now()
        if current:
            self._conn.execute(
                "UPDATE thesis_claims SET unlinked_at = ?, reason = ? WHERE id = ?",
                (now, f"re-linked as {role.value}", current["id"]),
            )
        self._conn.execute(
            "INSERT INTO thesis_claims (id, thesis_id, claim_id, role, weight, linked_at)"
            " VALUES (?, ?, ?, ?, ?, ?)",
            (self._ids.new(), thesis_id, claim.lineage_id, role.value, weight, now),
        )
        return True

    def _unlink_thesis_claim(self, thesis_id: str, claim_id: str, reason: str) -> None:
        lineage = self.get_claim(claim_id).lineage_id
        cursor = self._conn.execute(
            "UPDATE thesis_claims SET unlinked_at = ?, reason = ?"
            " WHERE thesis_id = ? AND claim_id = ? AND unlinked_at IS NULL",
            (self._now(), reason, thesis_id, lineage),
        )
        if cursor.rowcount == 0:
            raise NotFoundError("thesis link", f"{thesis_id} -> {claim_id}")

    def _add_trigger(self, thesis_id: str, description: str, entity: str | None) -> None:
        if not description.strip():
            raise ValueError("trigger description must not be empty")
        entity_id = None if entity is None else self.live_entity_id(entity)
        self._conn.execute(
            "INSERT INTO thesis_triggers (id, thesis_id, description, entity_id, created_at)"
            " VALUES (?, ?, ?, ?, ?)",
            (self._ids.new(), thesis_id, description, entity_id, self._now()),
        )

    def _remove_trigger(self, thesis_id: str, trigger_id: str) -> None:
        cursor = self._conn.execute(
            "UPDATE thesis_triggers SET removed_at = ?"
            " WHERE id = ? AND thesis_id = ? AND removed_at IS NULL",
            (self._now(), trigger_id, thesis_id),
        )
        if cursor.rowcount == 0:
            raise NotFoundError("thesis trigger", trigger_id)

    def update_thesis(
        self,
        thesis_id: str,
        reason: str,
        status: ThesisStatus | None = None,
        confidence: float | None = None,
        alert_id: str | None = None,
    ) -> Thesis:
        """Change status and/or confidence, validating the lifecycle and logging history."""
        return self.revise_thesis(
            thesis_id, reason, status=status, confidence=confidence, alert_id=alert_id
        )

    def revise_thesis(
        self,
        thesis_id: str,
        reason: str,
        status: ThesisStatus | None = None,
        confidence: float | None = None,
        add_claims: Sequence[tuple[str, ThesisClaimRole, float]] = (),
        remove_claims: Sequence[str] = (),
        add_triggers: Sequence[tuple[str, str | None]] = (),
        remove_triggers: Sequence[str] = (),
        alert_id: str | None = None,
    ) -> Thesis:
        """Revise a thesis in one step: status, confidence, the claims it rests on
        and its triggers. All changes are atomic and logged with ``reason``.

        Removed claims stay in history (``thesis_links_as_of``); a claim already
        linked with the same role and weight is left alone.
        """
        with self.transaction():
            thesis = self.get_thesis(thesis_id)
            new_status = ThesisStatus(status) if status is not None else thesis.status
            new_conf = confidence if confidence is not None else thesis.confidence
            if not 0.0 <= new_conf <= 1.0:
                raise ValueError(f"confidence must be in [0, 1], got {new_conf}")
            if new_status != thesis.status and new_status not in THESIS_TRANSITIONS[thesis.status]:
                raise InvalidTransitionError(
                    f"thesis cannot move from {thesis.status.value} to {new_status.value}"
                )
            changed = new_status != thesis.status or new_conf != thesis.confidence
            for claim_id in remove_claims:
                self._unlink_thesis_claim(thesis_id, claim_id, reason)
                changed = True
            for claim_id, role, weight in add_claims:
                changed = self._link_thesis_claim(thesis_id, claim_id, role, weight) or changed
            for trigger_id in remove_triggers:
                self._remove_trigger(thesis_id, trigger_id)
                changed = True
            for description, entity in add_triggers:
                self._add_trigger(thesis_id, description, entity)
                changed = True
            if not changed:
                return thesis
            self._conn.execute(
                "UPDATE theses SET status = ?, confidence = ?, updated_at = ? WHERE id = ?",
                (new_status.value, new_conf, self._now(), thesis_id),
            )
            self._append_history(thesis_id, new_status, new_conf, reason, alert_id)
            return self.get_thesis(thesis_id)

    def thesis_links_as_of(self, thesis_id: str, as_of: datetime) -> list[ThesisClaim]:
        """The claims a thesis rested on at ``as_of``."""
        ts = to_ts(as_of)
        rows = self._all(
            "SELECT * FROM thesis_claims WHERE thesis_id = ? AND linked_at <= ?"
            " AND (unlinked_at IS NULL OR unlinked_at > ?) ORDER BY role, claim_id",
            (thesis_id, ts, ts),
        )
        return [_thesis_claim(r) for r in rows]

    def _append_history(
        self,
        thesis_id: str,
        status: ThesisStatus,
        confidence: float,
        reason: str | None,
        alert_id: str | None,
    ) -> None:
        self._conn.execute(
            "INSERT INTO thesis_history (id, thesis_id, status, confidence, reason, alert_id, at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?)",
            (self._ids.new(), thesis_id, status.value, confidence, reason, alert_id, self._now()),
        )

    def get_thesis(self, thesis_id: str) -> Thesis:
        theses = self._hydrate_theses(self._all("SELECT * FROM theses WHERE id = ?", (thesis_id,)))
        if not theses:
            raise NotFoundError("thesis", thesis_id)
        return theses[0]

    def theses(
        self,
        entity_id: str | None = None,
        statuses: Sequence[ThesisStatus] = (),
    ) -> list[Thesis]:
        """Theses about an entity: as subject, or through any linked current claim."""
        params: list[Any] = [WORKSPACE]
        sql = "SELECT t.* FROM theses t WHERE t.workspace = ?"
        if entity_id is not None:
            entity_id = self._live_or_same(entity_id)
            sql += (
                " AND (t.subject_entity_id = ? OR t.id IN ("
                "  SELECT tc.thesis_id FROM thesis_claims tc"
                "  JOIN claims c ON c.lineage_id = tc.claim_id AND c.superseded_at IS NULL"
                "   AND tc.unlinked_at IS NULL"
                "  JOIN claim_entities ce ON ce.claim_id = c.id"
                "  WHERE ce.entity_id = ?))"
            )
            params += [entity_id, entity_id]
        if statuses:
            sql += f" AND t.status IN ({_placeholders(len(statuses))})"
            params += [ThesisStatus(s).value for s in statuses]
        sql += " ORDER BY t.updated_at DESC, t.id DESC"
        return self._hydrate_theses(self._all(sql, params))

    def thesis_history(self, thesis_id: str) -> list[ThesisHistoryEntry]:
        rows = self._all(
            "SELECT * FROM thesis_history WHERE thesis_id = ? ORDER BY at, id", (thesis_id,)
        )
        return [_history(r) for r in rows]

    def thesis_changes(
        self,
        since: datetime | None = None,
        until: datetime | None = None,
        thesis_ids: Sequence[str] | None = None,
    ) -> list[ThesisHistoryEntry]:
        """Thesis history entries within [since, until), oldest first."""
        params: list[Any] = []
        clauses = ["1 = 1"]
        if since is not None:
            clauses.append("at >= ?")
            params.append(to_ts(since))
        if until is not None:
            clauses.append("at < ?")
            params.append(to_ts(until))
        if thesis_ids is not None:
            if not thesis_ids:
                return []
            clauses.append(f"thesis_id IN ({_placeholders(len(thesis_ids))})")
            params += list(thesis_ids)
        rows = self._all(
            f"SELECT * FROM thesis_history WHERE {' AND '.join(clauses)} ORDER BY at, id", params
        )
        return [_history(r) for r in rows]

    def entities_created(
        self, since: datetime | None = None, until: datetime | None = None
    ) -> list[Entity]:
        params: list[Any] = [WORKSPACE]
        sql = "SELECT * FROM entities WHERE workspace = ?"
        if since is not None:
            sql += " AND created_at >= ?"
            params.append(to_ts(since))
        if until is not None:
            sql += " AND created_at < ?"
            params.append(to_ts(until))
        return self._hydrate_entities(self._all(sql + " ORDER BY created_at, id", params))

    def thesis_state_as_of(self, thesis_id: str, as_of: datetime) -> ThesisHistoryEntry | None:
        """Status and confidence the thesis had at ``as_of`` (None if not yet created)."""
        row = self._one(
            "SELECT * FROM thesis_history WHERE thesis_id = ? AND at <= ?"
            " ORDER BY at DESC, id DESC LIMIT 1",
            (thesis_id, to_ts(as_of)),
        )
        return _history(row) if row else None

    def _hydrate_theses(self, rows: list[sqlite3.Row]) -> list[Thesis]:
        if not rows:
            return []
        ids = [r["id"] for r in rows]
        marks = _placeholders(len(ids))
        links: dict[str, list[ThesisClaim]] = {i: [] for i in ids}
        for r in self._all(
            f"SELECT * FROM thesis_claims WHERE thesis_id IN ({marks})"
            " AND unlinked_at IS NULL ORDER BY role, claim_id",
            ids,
        ):
            links[r["thesis_id"]].append(_thesis_claim(r))
        triggers: dict[str, list[ThesisTrigger]] = {i: [] for i in ids}
        for r in self._all(
            f"SELECT * FROM thesis_triggers WHERE thesis_id IN ({marks})"
            " AND removed_at IS NULL ORDER BY created_at, id",
            ids,
        ):
            triggers[r["thesis_id"]].append(
                ThesisTrigger(id=r["id"], description=r["description"], entity_id=r["entity_id"])
            )
        return [
            Thesis(
                id=r["id"],
                title=r["title"],
                statement=r["statement"],
                stance=Stance(r["stance"]),
                horizon=r["horizon"],
                confidence=r["confidence"],
                status=ThesisStatus(r["status"]),
                subject_entity_id=r["subject_entity_id"],
                session_id=r["session_id"],
                created_at=from_ts(r["created_at"]),
                updated_at=from_ts(r["updated_at"]),
                claims=tuple(links[r["id"]]),
                triggers=tuple(triggers[r["id"]]),
            )
            for r in rows
        ]

    # -- rejected hypotheses ------------------------------------------------

    def record_rejected_hypothesis(
        self,
        text: str,
        reason: str,
        entity_ids: Sequence[str] = (),
        session_id: str | None = None,
    ) -> RejectedHypothesis:
        with self.transaction():
            entity_ids = [self.live_entity_id(e) for e in entity_ids]
            hyp_id = self._ids.new()
            self._conn.execute(
                "INSERT INTO rejected_hypotheses (id, text, reason, session_id, recorded_at)"
                " VALUES (?, ?, ?, ?, ?)",
                (hyp_id, text, reason, session_id, self._now()),
            )
            self._conn.executemany(
                "INSERT OR IGNORE INTO rejected_hypothesis_entities (hypothesis_id, entity_id)"
                " VALUES (?, ?)",
                [(hyp_id, e) for e in entity_ids],
            )
        return self.rejected_hypotheses(hypothesis_id=hyp_id)[0]

    def rejected_hypotheses(
        self, entity_id: str | None = None, hypothesis_id: str | None = None
    ) -> list[RejectedHypothesis]:
        params: list[Any] = [WORKSPACE]
        sql = "SELECT * FROM rejected_hypotheses WHERE workspace = ?"
        if entity_id is not None:
            entity_id = self._live_or_same(entity_id)
            sql += (
                " AND id IN (SELECT hypothesis_id FROM rejected_hypothesis_entities"
                " WHERE entity_id = ?)"
            )
            params.append(entity_id)
        if hypothesis_id is not None:
            sql += " AND id = ?"
            params.append(hypothesis_id)
        rows = self._all(sql + " ORDER BY recorded_at DESC, id DESC", params)
        result = []
        for r in rows:
            entity_rows = self._all(
                "SELECT entity_id FROM rejected_hypothesis_entities WHERE hypothesis_id = ?"
                " ORDER BY entity_id",
                (r["id"],),
            )
            result.append(
                RejectedHypothesis(
                    id=r["id"],
                    text=r["text"],
                    reason=r["reason"],
                    entity_ids=tuple(e["entity_id"] for e in entity_rows),
                    session_id=r["session_id"],
                    recorded_at=from_ts(r["recorded_at"]),
                )
            )
        return result

    # -- events & alerts ------------------------------------------------------

    def add_event(
        self,
        kind: EventKind,
        title: str,
        occurred_at: datetime,
        entity_ids: Sequence[str] = (),
        source: str = "manual",
        uri: str | None = None,
        summary: str | None = None,
        payload: dict[str, Any] | None = None,
    ) -> tuple[Event, bool]:
        """Record an event; returns (event, created).

        Events dedupe on their URI, or on title + day when there is none, so a
        watcher seeing the same filing twice (or a feed repeating an item) is a
        no-op. A repeat may still add entities the first sighting missed.
        """
        if not title.strip():
            raise ValueError("event title must not be empty")
        basis = uri.strip() if uri else f"{title.strip().casefold()}|{to_ts(occurred_at)[:10]}"
        content_hash = hashlib.sha256(basis.encode("utf-8")).hexdigest()
        with self.transaction():
            live_ids = list(dict.fromkeys(self.live_entity_id(e) for e in entity_ids))
            row = self._one(
                "SELECT id FROM events WHERE workspace = ? AND content_hash = ?",
                (WORKSPACE, content_hash),
            )
            created = row is None
            event_id = self._ids.new() if row is None else row["id"]
            if created:
                self._conn.execute(
                    "INSERT INTO events (id, kind, title, summary, uri, source, occurred_at,"
                    " ingested_at, content_hash, payload_json)"
                    " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        event_id,
                        EventKind(kind).value,
                        title.strip(),
                        summary,
                        uri,
                        source,
                        to_ts(occurred_at),
                        self._now(),
                        content_hash,
                        json.dumps(payload or {}, sort_keys=True),
                    ),
                )
            self._conn.executemany(
                "INSERT OR IGNORE INTO event_entities (event_id, entity_id) VALUES (?, ?)",
                [(event_id, e) for e in live_ids],
            )
            return self.get_event(event_id), created

    def get_event(self, event_id: str) -> Event:
        events = self._hydrate_events(self._all("SELECT * FROM events WHERE id = ?", (event_id,)))
        if not events:
            raise NotFoundError("event", event_id)
        return events[0]

    def events(
        self,
        entity_id: str | None = None,
        since: datetime | None = None,
        limit: int | None = None,
    ) -> list[Event]:
        """Events, newest first by when they happened."""
        params: list[Any] = [WORKSPACE]
        sql = "SELECT e.* FROM events e WHERE e.workspace = ?"
        if entity_id is not None:
            sql += " AND e.id IN (SELECT event_id FROM event_entities WHERE entity_id = ?)"
            params.append(self._live_or_same(entity_id))
        if since is not None:
            sql += " AND e.occurred_at >= ?"
            params.append(to_ts(since))
        sql += " ORDER BY e.occurred_at DESC, e.id DESC"
        if limit is not None:
            sql += " LIMIT ?"
            params.append(limit)
        return self._hydrate_events(self._all(sql, params))

    def _hydrate_events(self, rows: list[sqlite3.Row]) -> list[Event]:
        if not rows:
            return []
        ids = [r["id"] for r in rows]
        ents: dict[str, list[str]] = {i: [] for i in ids}
        for r in self._all(
            f"SELECT * FROM event_entities WHERE event_id IN ({_placeholders(len(ids))})"
            " ORDER BY entity_id",
            ids,
        ):
            ents[r["event_id"]].append(r["entity_id"])
        return [
            Event(
                id=r["id"],
                kind=EventKind(r["kind"]),
                title=r["title"],
                summary=r["summary"],
                uri=r["uri"],
                source=r["source"],
                occurred_at=from_ts(r["occurred_at"]),
                ingested_at=from_ts(r["ingested_at"]),
                content_hash=r["content_hash"],
                payload=json.loads(r["payload_json"]),
                entity_ids=tuple(ents[r["id"]]),
            )
            for r in rows
        ]

    def thesis_triggers_for_entity(self, entity_id: str) -> list[tuple[str, ThesisTrigger]]:
        """(thesis_id, trigger) pairs whose current trigger watches this entity."""
        rows = self._all(
            "SELECT * FROM thesis_triggers WHERE entity_id = ? AND removed_at IS NULL"
            " ORDER BY created_at, id",
            (self._live_or_same(entity_id),),
        )
        return [
            (
                r["thesis_id"],
                ThesisTrigger(id=r["id"], description=r["description"], entity_id=r["entity_id"]),
            )
            for r in rows
        ]

    def upsert_alert(
        self, event_id: str, thesis_id: str, score: float, paths: Sequence[ImpactPath]
    ) -> tuple[Alert, bool]:
        """One alert per (event, thesis); returns (alert, created)."""
        with self.transaction():
            self.get_event(event_id)
            self.get_thesis(thesis_id)
            row = self._one(
                "SELECT id FROM alerts WHERE event_id = ? AND thesis_id = ?", (event_id, thesis_id)
            )
            if row:
                return self.get_alert(row["id"]), False
            alert_id = self._ids.new()
            self._conn.execute(
                "INSERT INTO alerts (id, event_id, thesis_id, score, paths_json, status,"
                " created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    alert_id,
                    event_id,
                    thesis_id,
                    score,
                    json.dumps([p.model_dump() for p in paths]),
                    AlertStatus.OPEN.value,
                    self._now(),
                ),
            )
            return self.get_alert(alert_id), True

    def get_alert(self, alert_id: str) -> Alert:
        row = self._one("SELECT * FROM alerts WHERE id = ?", (alert_id,))
        if row is None:
            raise NotFoundError("alert", alert_id)
        return _alert(row)

    def alerts(
        self,
        statuses: Sequence[AlertStatus] = (AlertStatus.OPEN,),
        thesis_ids: Sequence[str] | None = None,
        event_id: str | None = None,
    ) -> list[Alert]:
        """Alerts, highest score first."""
        params: list[Any] = []
        sql = "SELECT * FROM alerts WHERE 1 = 1"
        if statuses:
            sql += f" AND status IN ({_placeholders(len(statuses))})"
            params += [AlertStatus(s).value for s in statuses]
        if thesis_ids is not None:
            if not thesis_ids:
                return []
            sql += f" AND thesis_id IN ({_placeholders(len(thesis_ids))})"
            params += list(thesis_ids)
        if event_id is not None:
            sql += " AND event_id = ?"
            params.append(event_id)
        sql += " ORDER BY score DESC, created_at, id"
        return [_alert(r) for r in self._all(sql, params)]

    def resolve_alert(
        self,
        alert_id: str,
        verdict: Verdict,
        rationale: str,
        confidence: float | None = None,
        status: ThesisStatus | None = None,
    ) -> Alert:
        """Record the agent's judgement on an alert and apply it to the thesis.

        * ``breaks``    - thesis -> broken
        * ``weakens``   - an active thesis -> at_risk
        * ``supports``  - status unchanged
        * ``unrelated`` - alert dismissed, thesis untouched

        ``status`` overrides the default status change; ``confidence`` sets the
        thesis's new confidence. Thesis changes are logged with the alert id.
        """
        verdict = Verdict(verdict)
        if not rationale.strip():
            raise ValueError("a rationale is required to resolve an alert")
        with self.transaction():
            alert = self.get_alert(alert_id)
            if alert.status is not AlertStatus.OPEN:
                raise NotCurrentError(f"alert {alert_id} is already {alert.status.value}")
            thesis = self.get_thesis(alert.thesis_id)
            new_status = status
            if new_status is None:
                if verdict is Verdict.BREAKS:
                    new_status = ThesisStatus.BROKEN
                elif verdict is Verdict.WEAKENS and thesis.status is ThesisStatus.ACTIVE:
                    new_status = ThesisStatus.AT_RISK
            if verdict is not Verdict.UNRELATED and (
                new_status is not None or confidence is not None
            ):
                event = self.get_event(alert.event_id)
                self.revise_thesis(
                    thesis.id,
                    reason=f"{verdict.value}: {event.title} - {rationale}",
                    status=new_status,
                    confidence=confidence,
                    alert_id=alert.id,
                )
            final = AlertStatus.DISMISSED if verdict is Verdict.UNRELATED else AlertStatus.RESOLVED
            self._conn.execute(
                "UPDATE alerts SET status = ?, verdict = ?, rationale = ?, resolved_at = ?"
                " WHERE id = ?",
                (final.value, verdict.value, rationale, self._now(), alert_id),
            )
            return self.get_alert(alert_id)

    # -- watchers -----------------------------------------------------------------

    def watch_entity(self, entity_id: str, note: str | None = None) -> Entity:
        with self.transaction():
            entity = self.get_entity(entity_id)
            self._conn.execute(
                "INSERT INTO watchlist (entity_id, added_at, note) VALUES (?, ?, ?)"
                " ON CONFLICT (entity_id) DO UPDATE SET note = coalesce(excluded.note, note)",
                (entity.id, self._now(), note),
            )
            return entity

    def unwatch_entity(self, entity_id: str) -> bool:
        with self.transaction():
            live = self.live_entity_id(entity_id)
            cursor = self._conn.execute("DELETE FROM watchlist WHERE entity_id = ?", (live,))
            return cursor.rowcount > 0

    def watchlist(self) -> list[Entity]:
        rows = self._all(
            "SELECT e.* FROM watchlist w JOIN entities e ON e.id = w.entity_id"
            " ORDER BY w.added_at, e.id"
        )
        return self._hydrate_entities(rows)

    def watch_targets(self) -> list[Entity]:
        """Entities watchers should follow: the watchlist, plus the subjects and
        trigger entities of open (active or at-risk) theses."""
        open_statuses = (ThesisStatus.ACTIVE.value, ThesisStatus.AT_RISK.value)
        rows = self._all(
            "SELECT e.* FROM entities e WHERE e.workspace = ? AND e.id IN ("
            "  SELECT entity_id FROM watchlist"
            "  UNION SELECT subject_entity_id FROM theses"
            "   WHERE status IN (?, ?) AND subject_entity_id IS NOT NULL"
            "  UNION SELECT tt.entity_id FROM thesis_triggers tt"
            "   JOIN theses t ON t.id = tt.thesis_id"
            "   WHERE tt.removed_at IS NULL AND tt.entity_id IS NOT NULL AND t.status IN (?, ?)"
            ") ORDER BY e.name, e.id",
            (WORKSPACE, *open_statuses, *open_statuses),
        )
        return self._hydrate_entities(rows)

    def add_feed(self, url: str, name: str | None = None) -> dict[str, str | None]:
        if not url.lower().startswith(("http://", "https://")):
            raise ValueError("feed URL must start with http:// or https://")
        with self.transaction():
            self._conn.execute(
                "INSERT INTO feeds (id, url, name, added_at) VALUES (?, ?, ?, ?)"
                " ON CONFLICT (url) DO UPDATE SET name = coalesce(excluded.name, name)",
                (self._ids.new(), url, name, self._now()),
            )
            row = self._one("SELECT id, url, name FROM feeds WHERE url = ?", (url,))
            assert row is not None
            return {"id": row["id"], "url": row["url"], "name": row["name"]}

    def remove_feed(self, url: str) -> bool:
        with self.transaction():
            return self._conn.execute("DELETE FROM feeds WHERE url = ?", (url,)).rowcount > 0

    def feeds(self) -> list[dict[str, str | None]]:
        rows = self._all("SELECT id, url, name FROM feeds ORDER BY added_at, id")
        return [{"id": r["id"], "url": r["url"], "name": r["name"]} for r in rows]

    def get_state(self, key: str) -> dict[str, Any] | None:
        row = self._one("SELECT value_json FROM watcher_state WHERE key = ?", (key,))
        return json.loads(row["value_json"]) if row else None

    def set_state(self, key: str, value: dict[str, Any]) -> None:
        with self.transaction():
            self._conn.execute(
                "INSERT INTO watcher_state (key, value_json, updated_at) VALUES (?, ?, ?)"
                " ON CONFLICT (key) DO UPDATE SET value_json = excluded.value_json,"
                " updated_at = excluded.updated_at",
                (key, json.dumps(value, sort_keys=True), self._now()),
            )

    def get_setting(self, key: str) -> str | None:
        row = self._one("SELECT value FROM settings WHERE key = ?", (key,))
        return str(row["value"]) if row else None

    def set_setting(self, key: str, value: str | None) -> None:
        with self.transaction():
            if value is None:
                self._conn.execute("DELETE FROM settings WHERE key = ?", (key,))
            else:
                self._conn.execute(
                    "INSERT INTO settings (key, value, updated_at) VALUES (?, ?, ?)"
                    " ON CONFLICT (key) DO UPDATE SET value = excluded.value,"
                    " updated_at = excluded.updated_at",
                    (key, value, self._now()),
                )

    # -- session capture (hooks) ----------------------------------------------

    def add_source_candidate(
        self,
        session_id: str,
        tool: str,
        uri: str | None = None,
        query: str | None = None,
    ) -> SourceCandidate:
        """Note a source the agent looked at; repeated sightings are one row."""
        if not uri and not query:
            raise ValueError("a source candidate needs a uri or a query")
        with self.transaction():
            self.get_session(session_id)
            self._conn.execute(
                "INSERT OR IGNORE INTO source_candidates (id, session_id, tool, uri, query,"
                " seen_at) VALUES (?, ?, ?, ?, ?, ?)",
                (self._ids.new(), session_id, tool, uri, query, self._now()),
            )
            if uri:  # already backed by recorded evidence?
                self._conn.execute(
                    "UPDATE source_candidates SET linked_evidence_id = (SELECT id FROM evidence"
                    " WHERE workspace = ? AND uri = ? ORDER BY recorded_at DESC LIMIT 1)"
                    " WHERE session_id = ? AND uri = ? AND linked_evidence_id IS NULL",
                    (WORKSPACE, uri, session_id, uri),
                )
            row = self._one(
                "SELECT * FROM source_candidates WHERE session_id = ? AND tool = ?"
                " AND coalesce(uri, '') = ? AND coalesce(query, '') = ?",
                (session_id, tool, uri or "", query or ""),
            )
            assert row is not None
            return _source_candidate(row)

    def source_candidates(self, session_id: str) -> list[SourceCandidate]:
        rows = self._all(
            "SELECT * FROM source_candidates WHERE session_id = ? ORDER BY seen_at, id",
            (session_id,),
        )
        return [_source_candidate(r) for r in rows]

    def note_session_entities(self, session_id: str, entity_ids: Sequence[str]) -> None:
        """Record that a session's prompts mentioned these entities."""
        if not entity_ids:
            return
        now = self._now()
        with self.transaction():
            self.get_session(session_id)
            for entity_id in entity_ids:
                self._conn.execute(
                    "INSERT INTO session_entities (session_id, entity_id, first_seen_at)"
                    " VALUES (?, ?, ?) ON CONFLICT (session_id, entity_id)"
                    " DO UPDATE SET mentions = mentions + 1",
                    (session_id, self.live_entity_id(entity_id), now),
                )

    def note_session_record(self, session_id: str, object_type: str, object_id: str) -> None:
        """Record that the agent wrote an object through Torus during a session."""
        with self.transaction():
            self.get_session(session_id)
            self._conn.execute(
                "INSERT OR IGNORE INTO session_records (session_id, object_type, object_id, at)"
                " VALUES (?, ?, ?, ?)",
                (session_id, object_type, object_id, self._now()),
            )

    def adopt_into_session(self, session_id: str, object_type: str, object_id: str) -> str:
        """Re-attribute an object recorded by an anonymous session (e.g. the MCP
        server, which never learns the harness session id) to ``session_id``.

        Returns ``adopted``, ``own`` (already this session's), ``other`` (belongs
        to another harness session, e.g. one the curator is distilling, and is
        left alone) or ``untracked`` (object type has no session, or unknown id).
        """
        tables = {"claim": "claims", "thesis": "theses", "hypothesis": "rejected_hypotheses"}
        table = tables.get(object_type)
        if table is None:
            return "untracked"
        with self.transaction():
            self.get_session(session_id)
            row = self._one(
                f"SELECT o.session_id, s.external_session_id FROM {table} o"
                " LEFT JOIN sessions s ON s.id = o.session_id WHERE o.id = ?",
                (object_id,),
            )
            if row is None:
                return "untracked"
            if row["session_id"] == session_id:
                return "own"
            if row["session_id"] is not None and row["external_session_id"] is not None:
                return "other"
            self._conn.execute(
                f"UPDATE {table} SET session_id = ? WHERE id = ?", (session_id, object_id)
            )
            return "adopted"

    def session_records(self, session_id: str) -> list[tuple[str, str]]:
        """(object_type, object_id) pairs the agent recorded during a session."""
        rows = self._all(
            "SELECT object_type, object_id FROM session_records WHERE session_id = ?"
            " ORDER BY at, object_type, object_id",
            (session_id,),
        )
        return [(r["object_type"], r["object_id"]) for r in rows]

    def session_activity(self, session_id: str) -> SessionActivity:
        self.get_session(session_id)
        entity_rows = self._all(
            "SELECT entity_id FROM session_entities WHERE session_id = ?"
            " ORDER BY first_seen_at, entity_id",
            (session_id,),
        )
        record_rows = self._all(
            "SELECT object_type, count(*) AS n FROM session_records WHERE session_id = ?"
            " GROUP BY object_type",
            (session_id,),
        )
        candidates = self._one(
            "SELECT count(*) AS n FROM source_candidates WHERE session_id = ?", (session_id,)
        )
        return SessionActivity(
            session_id=session_id,
            entity_ids=tuple(r["entity_id"] for r in entity_rows),
            records={r["object_type"]: int(r["n"]) for r in record_rows},
            source_candidates=int(candidates["n"]) if candidates else 0,
        )

    # -- queue ----------------------------------------------------------------

    def enqueue(self, kind: str, dedupe_key: str, payload: dict[str, Any]) -> QueueItem:
        """Add work, or refresh the payload of the pending item with the same key."""
        now = self._now()
        body = json.dumps(payload, sort_keys=True)
        with self.transaction():
            row = self._one(
                "SELECT id FROM queue WHERE dedupe_key = ? AND status = ?",
                (dedupe_key, QueueStatus.PENDING.value),
            )
            if row:
                self._conn.execute(
                    "UPDATE queue SET payload_json = ?, updated_at = ? WHERE id = ?",
                    (body, now, row["id"]),
                )
                return self.get_queue_item(row["id"])
            item_id = self._ids.new()
            self._conn.execute(
                "INSERT INTO queue (id, kind, dedupe_key, payload_json, status, created_at,"
                " updated_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (item_id, kind, dedupe_key, body, QueueStatus.PENDING.value, now, now),
            )
            return self.get_queue_item(item_id)

    def get_queue_item(self, item_id: str) -> QueueItem:
        row = self._one("SELECT * FROM queue WHERE id = ?", (item_id,))
        if row is None:
            raise NotFoundError("queue item", item_id)
        return _queue_item(row)

    def lease_work(self, kind: str, limit: int = 1, lease_seconds: int = 1800) -> list[QueueItem]:
        """Hand out the oldest pending items (and expired leases) for processing.

        Each lease counts as an attempt; an item whose lease expired after
        ``MAX_ATTEMPTS`` attempts is parked instead of handed out again.
        """
        now = self._clock.now()
        now_ts = to_ts(now)
        until = to_ts(now + timedelta(seconds=lease_seconds))
        leased: list[QueueItem] = []
        with self.transaction():
            rows = self._all(
                "SELECT * FROM queue WHERE kind = ? AND (status = ?"
                " OR (status = ? AND lease_until < ?)) ORDER BY created_at, id",
                (kind, QueueStatus.PENDING.value, QueueStatus.LEASED.value, now_ts),
            )
            for row in rows:
                if len(leased) >= limit:
                    break
                if row["attempts"] >= MAX_ATTEMPTS:
                    self._set_queue_status(row["id"], QueueStatus.PARKED, error="lease expired")
                    continue
                self._conn.execute(
                    "UPDATE queue SET status = ?, attempts = attempts + 1, lease_until = ?,"
                    " updated_at = ? WHERE id = ?",
                    (QueueStatus.LEASED.value, until, now_ts, row["id"]),
                )
                leased.append(self.get_queue_item(row["id"]))
        return leased

    def complete_work(self, item_id: str, result: dict[str, Any]) -> QueueItem:
        """Mark a leased item done, storing ``result`` in its payload."""
        with self.transaction():
            item = self._require_leased(item_id)
            payload = {**item.payload, "result": result}
            self._conn.execute(
                "UPDATE queue SET status = ?, payload_json = ?, lease_until = NULL,"
                " updated_at = ? WHERE id = ?",
                (
                    QueueStatus.DONE.value,
                    json.dumps(payload, sort_keys=True),
                    self._now(),
                    item_id,
                ),
            )
            return self.get_queue_item(item_id)

    def release_work(self, item_id: str, error: str) -> QueueItem:
        """Give a leased item back after a failure: retried later, or parked after
        ``MAX_ATTEMPTS``. If newer activity queued a fresh item for the same key
        meanwhile, this one is closed in favour of it."""
        with self.transaction():
            item = self._require_leased(item_id)
            if item.attempts >= MAX_ATTEMPTS:
                self._set_queue_status(item_id, QueueStatus.PARKED, error=error)
                return self.get_queue_item(item_id)
            newer = self._one(
                "SELECT id FROM queue WHERE dedupe_key = ? AND status = ? AND id != ?",
                (item.dedupe_key, QueueStatus.PENDING.value, item_id),
            )
            if newer:
                self._set_queue_status(
                    item_id, QueueStatus.DONE, error=error, merged_into=newer["id"]
                )
            else:
                self._set_queue_status(item_id, QueueStatus.PENDING, error=error)
            return self.get_queue_item(item_id)

    def _require_leased(self, item_id: str) -> QueueItem:
        item = self.get_queue_item(item_id)
        if item.status is not QueueStatus.LEASED:
            raise NotCurrentError(f"queue item {item_id} is {item.status.value}, not leased")
        return item

    def _set_queue_status(self, item_id: str, status: QueueStatus, **notes: str) -> None:
        item = self.get_queue_item(item_id)
        payload = dict(item.payload)
        if "error" in notes:
            payload["errors"] = [*payload.get("errors", []), notes.pop("error")]
        payload.update(notes)
        self._conn.execute(
            "UPDATE queue SET status = ?, payload_json = ?, lease_until = NULL, updated_at = ?"
            " WHERE id = ?",
            (status.value, json.dumps(payload, sort_keys=True), self._now(), item_id),
        )

    def queue_counts(self) -> dict[str, dict[str, int]]:
        """kind -> status -> count."""
        counts: dict[str, dict[str, int]] = {}
        for row in self._all("SELECT kind, status, count(*) AS n FROM queue GROUP BY kind, status"):
            counts.setdefault(row["kind"], {})[row["status"]] = int(row["n"])
        return counts

    def queue_items(
        self, kind: str | None = None, statuses: Sequence[QueueStatus] = ()
    ) -> list[QueueItem]:
        params: list[Any] = []
        sql = "SELECT * FROM queue WHERE 1 = 1"
        if kind is not None:
            sql += " AND kind = ?"
            params.append(kind)
        if statuses:
            sql += f" AND status IN ({_placeholders(len(statuses))})"
            params += [QueueStatus(s).value for s in statuses]
        return [_queue_item(r) for r in self._all(sql + " ORDER BY created_at, id", params)]

    # -- search -------------------------------------------------------------

    def search(
        self,
        query: str,
        object_types: Sequence[str] = ("claim", "evidence", "thesis"),
        limit: int = 20,
        match_any: bool = False,
    ) -> list[SearchHit]:
        """Keyword search over current claims, evidence and theses (BM25 ranked).

        All terms must match unless ``match_any`` (used for natural-language recall).
        """
        match = _fts_query(query, match_any)
        if not match:
            return []
        sources = {
            "claim": ("claims_fts", "claim_id", 1),
            "evidence": ("evidence_fts", "evidence_id", 2),
            "thesis": ("theses_fts", "thesis_id", 2),
        }
        hits: list[SearchHit] = []
        for object_type in object_types:
            table, id_col, snippet_col = sources[object_type]
            rows = self._all(
                f"SELECT {id_col} AS object_id,"
                f" snippet({table}, {snippet_col}, '[', ']', '…', 12) AS snippet,"
                # Not aliased "rank": that is FTS5's hidden column, and ordering
                # by it would compute bm25 a second time.
                f" bm25({table}) AS score"
                f" FROM {table} WHERE {table} MATCH ? ORDER BY score LIMIT ?",
                (match, limit),
            )
            hits += [
                SearchHit(
                    object_type=object_type,
                    object_id=r["object_id"],
                    snippet=r["snippet"],
                    rank=r["score"],
                )
                for r in rows
            ]
        hits.sort(key=lambda h: h.rank)
        return hits[:limit]


def _as_claim_entity(value: str | ClaimEntity) -> ClaimEntity:
    if isinstance(value, ClaimEntity):
        return value
    return ClaimEntity(entity_id=value, role=ClaimEntityRole.SUBJECT)


def _as_claim_evidence(value: str | ClaimEvidence) -> ClaimEvidence:
    if isinstance(value, ClaimEvidence):
        return value
    return ClaimEvidence(evidence_id=value, support=Support.SUPPORTS)


def _relation(row: sqlite3.Row) -> Relation:
    return Relation(
        id=row["id"],
        lineage_id=row["lineage_id"],
        supersedes_id=row["supersedes_id"],
        src_id=row["src_id"],
        rel=RelationType(row["rel"]),
        dst_id=row["dst_id"],
        confidence=row["confidence"],
        claim_id=row["claim_id"],
        valid_from=from_ts_opt(row["valid_from"]),
        valid_to=from_ts_opt(row["valid_to"]),
        recorded_at=from_ts(row["recorded_at"]),
        superseded_at=from_ts_opt(row["superseded_at"]),
        source=row["source"],
    )


def _history(row: sqlite3.Row) -> ThesisHistoryEntry:
    return ThesisHistoryEntry(
        id=row["id"],
        thesis_id=row["thesis_id"],
        status=ThesisStatus(row["status"]),
        confidence=row["confidence"],
        reason=row["reason"],
        alert_id=row["alert_id"],
        at=from_ts(row["at"]),
    )


def _source_candidate(row: sqlite3.Row) -> SourceCandidate:
    return SourceCandidate(
        id=row["id"],
        session_id=row["session_id"],
        tool=row["tool"],
        uri=row["uri"],
        query=row["query"],
        seen_at=from_ts(row["seen_at"]),
        linked_evidence_id=row["linked_evidence_id"],
    )


def _queue_item(row: sqlite3.Row) -> QueueItem:
    return QueueItem(
        id=row["id"],
        kind=row["kind"],
        dedupe_key=row["dedupe_key"],
        payload=json.loads(row["payload_json"]),
        status=QueueStatus(row["status"]),
        attempts=row["attempts"],
        created_at=from_ts(row["created_at"]),
        updated_at=from_ts(row["updated_at"]),
        lease_until=from_ts_opt(row["lease_until"]),
    )


def _thesis_claim(row: sqlite3.Row) -> ThesisClaim:
    return ThesisClaim(
        claim_lineage_id=row["claim_id"],
        role=ThesisClaimRole(row["role"]),
        weight=row["weight"],
    )


def _alert(row: sqlite3.Row) -> Alert:
    return Alert(
        id=row["id"],
        event_id=row["event_id"],
        thesis_id=row["thesis_id"],
        score=row["score"],
        paths=tuple(ImpactPath(**p) for p in json.loads(row["paths_json"])),
        status=AlertStatus(row["status"]),
        verdict=Verdict(row["verdict"]) if row["verdict"] else None,
        rationale=row["rationale"],
        created_at=from_ts(row["created_at"]),
        resolved_at=from_ts_opt(row["resolved_at"]),
    )
