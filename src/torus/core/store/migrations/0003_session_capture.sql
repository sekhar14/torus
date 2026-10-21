-- Passive capture from harness hooks (M4) and the distillation queue (M5).

-- Sources the agent looked at (WebFetch URLs, WebSearch queries), whether or not it
-- recorded them as evidence. The curator links them to claims later.
CREATE TABLE source_candidates (
    id                 TEXT PRIMARY KEY,
    session_id         TEXT NOT NULL REFERENCES sessions (id),
    tool               TEXT NOT NULL,
    uri                TEXT,
    query              TEXT,
    seen_at            TEXT NOT NULL,
    linked_evidence_id TEXT REFERENCES evidence (id)
);
CREATE UNIQUE INDEX source_candidates_dedupe
    ON source_candidates (session_id, tool, coalesce(uri, ''), coalesce(query, ''));

-- Entities a session's prompts mentioned: marks it as a research session.
CREATE TABLE session_entities (
    session_id    TEXT NOT NULL REFERENCES sessions (id),
    entity_id     TEXT NOT NULL REFERENCES entities (id),
    first_seen_at TEXT NOT NULL,
    mentions      INTEGER NOT NULL DEFAULT 1,
    PRIMARY KEY (session_id, entity_id)
);

-- Objects the agent recorded through Torus tools during a session.
CREATE TABLE session_records (
    session_id  TEXT NOT NULL REFERENCES sessions (id),
    object_type TEXT NOT NULL,
    object_id   TEXT NOT NULL,
    at          TEXT NOT NULL,
    PRIMARY KEY (session_id, object_type, object_id)
);

-- Work for agents to pick up later (e.g. distil a session transcript).
-- At most one *pending* item per dedupe_key.
CREATE TABLE queue (
    id           TEXT PRIMARY KEY,
    kind         TEXT NOT NULL,
    dedupe_key   TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    status       TEXT NOT NULL,
    attempts     INTEGER NOT NULL DEFAULT 0,
    created_at   TEXT NOT NULL,
    updated_at   TEXT NOT NULL,
    lease_until  TEXT
);
CREATE UNIQUE INDEX queue_pending_dedupe ON queue (dedupe_key) WHERE status = 'pending';
CREATE INDEX queue_status ON queue (status, created_at);
