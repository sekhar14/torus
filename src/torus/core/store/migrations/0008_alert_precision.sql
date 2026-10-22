-- Alert precision: how much an event matters, why an alert scored what it did,
-- per-thesis alert thresholds, and mutes ("no alerts on this thesis for TSMC 10-Qs").

ALTER TABLE events ADD COLUMN materiality REAL NOT NULL DEFAULT 1.0;
ALTER TABLE events ADD COLUMN materiality_reason TEXT;

-- score = graph_score * materiality * (1 + trigger_match); NULL on alerts from before 0008.
ALTER TABLE alerts ADD COLUMN graph_score REAL;
ALTER TABLE alerts ADD COLUMN materiality REAL;
ALTER TABLE alerts ADD COLUMN trigger_match REAL;
ALTER TABLE alerts ADD COLUMN matched_trigger TEXT;

-- NULL: use the global `alert_threshold` setting (or the built-in default).
ALTER TABLE theses ADD COLUMN alert_threshold REAL CHECK (alert_threshold BETWEEN 0 AND 10);

CREATE TABLE thesis_mutes (
    id          TEXT PRIMARY KEY,
    thesis_id   TEXT NOT NULL REFERENCES theses (id),
    entity_id   TEXT REFERENCES entities (id),  -- events about this entity
    event_kind  TEXT,                           -- filing | news | earnings | ...
    form        TEXT,                           -- SEC form, e.g. 10-Q
    until       TEXT,                           -- muted until (NULL: indefinitely)
    note        TEXT,
    created_at  TEXT NOT NULL,
    removed_at  TEXT,
    CHECK (entity_id IS NOT NULL OR event_kind IS NOT NULL OR form IS NOT NULL)
);
CREATE INDEX thesis_mutes_thesis ON thesis_mutes (thesis_id, removed_at);
