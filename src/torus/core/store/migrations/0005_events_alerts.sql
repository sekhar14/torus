-- Events (filings, news, manual notes) and the alerts they raise on theses (M6).

CREATE TABLE events (
    id           TEXT PRIMARY KEY,
    workspace    TEXT NOT NULL DEFAULT 'default',
    kind         TEXT NOT NULL,
    title        TEXT NOT NULL,
    summary      TEXT,
    uri          TEXT,
    source       TEXT NOT NULL,          -- e.g. manual, edgar, rss:<feed>
    occurred_at  TEXT NOT NULL,
    ingested_at  TEXT NOT NULL,
    content_hash TEXT NOT NULL,          -- dedupe key: hash of uri or of title+date
    payload_json TEXT NOT NULL DEFAULT '{}'
);
CREATE UNIQUE INDEX events_dedupe ON events (workspace, content_hash);
CREATE INDEX events_occurred ON events (occurred_at);

CREATE TABLE event_entities (
    event_id  TEXT NOT NULL REFERENCES events (id),
    entity_id TEXT NOT NULL REFERENCES entities (id),
    PRIMARY KEY (event_id, entity_id)
);
CREATE INDEX event_entities_entity ON event_entities (entity_id);

-- One alert per (event, thesis). The agent resolves it with a verdict.
CREATE TABLE alerts (
    id          TEXT PRIMARY KEY,
    event_id    TEXT NOT NULL REFERENCES events (id),
    thesis_id   TEXT NOT NULL REFERENCES theses (id),
    score       REAL NOT NULL,
    paths_json  TEXT NOT NULL,           -- why: event entity -> relations -> claim/trigger -> thesis
    status      TEXT NOT NULL,           -- open | resolved | dismissed
    verdict     TEXT,                    -- supports | weakens | breaks | unrelated
    rationale   TEXT,
    created_at  TEXT NOT NULL,
    resolved_at TEXT
);
CREATE UNIQUE INDEX alerts_event_thesis ON alerts (event_id, thesis_id);
CREATE INDEX alerts_status ON alerts (status, created_at);
CREATE INDEX alerts_thesis ON alerts (thesis_id);

CREATE VIRTUAL TABLE events_fts USING fts5 (event_id UNINDEXED, title, summary);
CREATE TRIGGER events_fts_insert AFTER INSERT ON events
BEGIN
    INSERT INTO events_fts (event_id, title, summary)
    VALUES (NEW.id, NEW.title, coalesce(NEW.summary, ''));
END;
