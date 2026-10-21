-- Entity resolution support:
--   * entity_aliases.alias_key: name_key() of the alias for suffix-insensitive name matching.
--     torus_name_key() is registered on the connection by the migration runner.
--   * entity_merges: redirects from merged-away entity ids to the surviving entity.

ALTER TABLE entity_aliases ADD COLUMN alias_key TEXT NOT NULL DEFAULT '';
UPDATE entity_aliases SET alias_key = torus_name_key(alias);
CREATE INDEX entity_aliases_key ON entity_aliases (alias_key);

CREATE TABLE entity_merges (
    merged_id TEXT PRIMARY KEY,
    into_id   TEXT NOT NULL,
    reason    TEXT NOT NULL,
    at        TEXT NOT NULL
);
CREATE INDEX entity_merges_into ON entity_merges (into_id);
