-- Watchers (M7): what to watch, where to look, and how far they have read.

-- Entities to watch explicitly (open theses' subjects and triggers are watched too).
CREATE TABLE watchlist (
    entity_id TEXT PRIMARY KEY REFERENCES entities (id),
    added_at  TEXT NOT NULL,
    note      TEXT
);

-- RSS / Atom feeds whose items are matched to entities.
CREATE TABLE feeds (
    id       TEXT PRIMARY KEY,
    url      TEXT NOT NULL UNIQUE,
    name     TEXT,
    added_at TEXT NOT NULL
);

-- Per-source cursors, e.g. "edgar:0001045810" -> last filing date seen.
CREATE TABLE watcher_state (
    key        TEXT PRIMARY KEY,
    value_json TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

-- Small user settings (e.g. the SEC contact email for the User-Agent).
CREATE TABLE settings (
    key        TEXT PRIMARY KEY,
    value      TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
