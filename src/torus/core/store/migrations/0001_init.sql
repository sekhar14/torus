-- Torus core schema v1: entities, evidence, claims, relations, theses, provenance.
-- Timestamps are fixed-width UTC strings (see torus.core.clock), so text order = time order.
-- `workspace` is reserved for the future multi-user tier; v0 always uses 'default'.

CREATE TABLE sessions (
    id                  TEXT PRIMARY KEY,
    workspace           TEXT NOT NULL DEFAULT 'default',
    harness             TEXT NOT NULL,
    external_session_id TEXT,
    transcript_path     TEXT,
    started_at          TEXT NOT NULL,
    ended_at            TEXT
);
CREATE UNIQUE INDEX sessions_external ON sessions (harness, external_session_id)
    WHERE external_session_id IS NOT NULL;

CREATE TABLE entities (
    id            TEXT PRIMARY KEY,
    workspace     TEXT NOT NULL DEFAULT 'default',
    kind          TEXT NOT NULL,
    name          TEXT NOT NULL,
    canonical_key TEXT,
    attrs_json    TEXT NOT NULL DEFAULT '{}',
    created_at    TEXT NOT NULL,
    updated_at    TEXT NOT NULL
);
CREATE UNIQUE INDEX entities_canonical ON entities (workspace, canonical_key)
    WHERE canonical_key IS NOT NULL;

CREATE TABLE entity_aliases (
    entity_id  TEXT NOT NULL REFERENCES entities (id) ON DELETE CASCADE,
    alias      TEXT NOT NULL,
    alias_norm TEXT NOT NULL,
    alias_type TEXT NOT NULL,
    PRIMARY KEY (entity_id, alias_norm, alias_type)
);
CREATE INDEX entity_aliases_norm ON entity_aliases (alias_norm);

CREATE TABLE evidence (
    id           TEXT PRIMARY KEY,
    workspace    TEXT NOT NULL DEFAULT 'default',
    kind         TEXT NOT NULL,
    uri          TEXT NOT NULL,
    title        TEXT,
    publisher    TEXT,
    published_at TEXT,
    retrieved_at TEXT NOT NULL,
    quote        TEXT,
    content_hash TEXT,
    source_path  TEXT,
    recorded_at  TEXT NOT NULL
);
CREATE UNIQUE INDEX evidence_dedupe ON evidence (workspace, uri, coalesce(content_hash, ''));

CREATE TABLE claims (
    id            TEXT PRIMARY KEY,
    workspace     TEXT NOT NULL DEFAULT 'default',
    lineage_id    TEXT NOT NULL,
    supersedes_id TEXT REFERENCES claims (id),
    text          TEXT NOT NULL,
    claim_type    TEXT NOT NULL,
    confidence    REAL NOT NULL CHECK (confidence BETWEEN 0 AND 1),
    status        TEXT NOT NULL,
    valid_from    TEXT,
    valid_to      TEXT,
    recorded_at   TEXT NOT NULL,
    superseded_at TEXT,
    change_reason TEXT,
    session_id    TEXT REFERENCES sessions (id)
);
CREATE INDEX claims_lineage ON claims (lineage_id, recorded_at);
CREATE INDEX claims_current ON claims (workspace, superseded_at);

CREATE TABLE claim_entities (
    claim_id  TEXT NOT NULL REFERENCES claims (id),
    entity_id TEXT NOT NULL REFERENCES entities (id),
    role      TEXT NOT NULL,
    PRIMARY KEY (claim_id, entity_id, role)
);
CREATE INDEX claim_entities_entity ON claim_entities (entity_id);

CREATE TABLE claim_evidence (
    claim_id    TEXT NOT NULL REFERENCES claims (id),
    evidence_id TEXT NOT NULL REFERENCES evidence (id),
    support     TEXT NOT NULL,
    PRIMARY KEY (claim_id, evidence_id)
);
CREATE INDEX claim_evidence_evidence ON claim_evidence (evidence_id);

CREATE TABLE claim_derivations (
    claim_id      TEXT NOT NULL REFERENCES claims (id),
    from_claim_id TEXT NOT NULL REFERENCES claims (id),
    PRIMARY KEY (claim_id, from_claim_id)
);

CREATE TABLE relations (
    id            TEXT PRIMARY KEY,
    workspace     TEXT NOT NULL DEFAULT 'default',
    lineage_id    TEXT NOT NULL,
    supersedes_id TEXT REFERENCES relations (id),
    src_id        TEXT NOT NULL REFERENCES entities (id),
    rel           TEXT NOT NULL,
    dst_id        TEXT NOT NULL REFERENCES entities (id),
    confidence    REAL NOT NULL CHECK (confidence BETWEEN 0 AND 1),
    claim_id      TEXT REFERENCES claims (id),
    valid_from    TEXT,
    valid_to      TEXT,
    recorded_at   TEXT NOT NULL,
    superseded_at TEXT
);
CREATE INDEX relations_src ON relations (src_id, superseded_at);
CREATE INDEX relations_dst ON relations (dst_id, superseded_at);

CREATE TABLE theses (
    id                TEXT PRIMARY KEY,
    workspace         TEXT NOT NULL DEFAULT 'default',
    title             TEXT NOT NULL,
    statement         TEXT NOT NULL,
    stance            TEXT NOT NULL,
    horizon           TEXT,
    confidence        REAL NOT NULL CHECK (confidence BETWEEN 0 AND 1),
    status            TEXT NOT NULL,
    subject_entity_id TEXT REFERENCES entities (id),
    session_id        TEXT REFERENCES sessions (id),
    created_at        TEXT NOT NULL,
    updated_at        TEXT NOT NULL
);
CREATE INDEX theses_subject ON theses (subject_entity_id);

CREATE TABLE thesis_claims (
    thesis_id TEXT NOT NULL REFERENCES theses (id),
    claim_id  TEXT NOT NULL REFERENCES claims (id),
    role      TEXT NOT NULL,
    weight    REAL NOT NULL DEFAULT 1.0,
    PRIMARY KEY (thesis_id, claim_id)
);
CREATE INDEX thesis_claims_claim ON thesis_claims (claim_id);

CREATE TABLE thesis_triggers (
    id          TEXT PRIMARY KEY,
    thesis_id   TEXT NOT NULL REFERENCES theses (id),
    description TEXT NOT NULL,
    entity_id   TEXT REFERENCES entities (id)
);

-- Append-only audit trail of thesis status/confidence.
CREATE TABLE thesis_history (
    id         TEXT PRIMARY KEY,
    thesis_id  TEXT NOT NULL REFERENCES theses (id),
    status     TEXT NOT NULL,
    confidence REAL NOT NULL,
    reason     TEXT,
    alert_id   TEXT,
    at         TEXT NOT NULL
);
CREATE INDEX thesis_history_thesis ON thesis_history (thesis_id, at);

CREATE TABLE rejected_hypotheses (
    id          TEXT PRIMARY KEY,
    workspace   TEXT NOT NULL DEFAULT 'default',
    text        TEXT NOT NULL,
    reason      TEXT NOT NULL,
    session_id  TEXT REFERENCES sessions (id),
    recorded_at TEXT NOT NULL
);

CREATE TABLE rejected_hypothesis_entities (
    hypothesis_id TEXT NOT NULL REFERENCES rejected_hypotheses (id),
    entity_id     TEXT NOT NULL REFERENCES entities (id),
    PRIMARY KEY (hypothesis_id, entity_id)
);

-- Full-text search. Claims index holds only current versions.
CREATE VIRTUAL TABLE claims_fts USING fts5 (claim_id UNINDEXED, text);
CREATE VIRTUAL TABLE evidence_fts USING fts5 (evidence_id UNINDEXED, title, quote);
CREATE VIRTUAL TABLE theses_fts USING fts5 (thesis_id UNINDEXED, title, statement);

CREATE TRIGGER claims_fts_insert AFTER INSERT ON claims WHEN NEW.superseded_at IS NULL
BEGIN
    INSERT INTO claims_fts (claim_id, text) VALUES (NEW.id, NEW.text);
END;

CREATE TRIGGER claims_fts_supersede AFTER UPDATE OF superseded_at ON claims
WHEN NEW.superseded_at IS NOT NULL AND OLD.superseded_at IS NULL
BEGIN
    DELETE FROM claims_fts WHERE claim_id = OLD.id;
END;

CREATE TRIGGER evidence_fts_insert AFTER INSERT ON evidence
BEGIN
    INSERT INTO evidence_fts (evidence_id, title, quote)
    VALUES (NEW.id, coalesce(NEW.title, ''), coalesce(NEW.quote, ''));
END;

CREATE TRIGGER theses_fts_insert AFTER INSERT ON theses
BEGIN
    INSERT INTO theses_fts (thesis_id, title, statement) VALUES (NEW.id, NEW.title, NEW.statement);
END;

CREATE TRIGGER theses_fts_update AFTER UPDATE OF title, statement ON theses
BEGIN
    DELETE FROM theses_fts WHERE thesis_id = OLD.id;
    INSERT INTO theses_fts (thesis_id, title, statement) VALUES (NEW.id, NEW.title, NEW.statement);
END;
