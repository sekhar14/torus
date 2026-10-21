-- Thesis links and triggers become versioned, so a thesis can drop or replace the
-- claims it rests on without losing what it rested on before (as_of recall).

CREATE TABLE thesis_claims_v2 (
    id          TEXT PRIMARY KEY,
    thesis_id   TEXT NOT NULL REFERENCES theses (id),
    claim_id    TEXT NOT NULL,          -- claim lineage id
    role        TEXT NOT NULL,
    weight      REAL NOT NULL DEFAULT 1.0,
    linked_at   TEXT NOT NULL,
    unlinked_at TEXT,
    reason      TEXT                    -- why it was unlinked
);
INSERT INTO thesis_claims_v2 (id, thesis_id, claim_id, role, weight, linked_at)
SELECT lower(hex(randomblob(13))), tc.thesis_id, tc.claim_id, tc.role, tc.weight, t.created_at
FROM thesis_claims tc JOIN theses t ON t.id = tc.thesis_id;
DROP TABLE thesis_claims;
ALTER TABLE thesis_claims_v2 RENAME TO thesis_claims;
CREATE UNIQUE INDEX thesis_claims_current ON thesis_claims (thesis_id, claim_id)
    WHERE unlinked_at IS NULL;
CREATE INDEX thesis_claims_claim ON thesis_claims (claim_id);

ALTER TABLE thesis_triggers ADD COLUMN created_at TEXT NOT NULL DEFAULT '';
UPDATE thesis_triggers SET created_at = (
    SELECT created_at FROM theses WHERE theses.id = thesis_triggers.thesis_id
);
ALTER TABLE thesis_triggers ADD COLUMN removed_at TEXT;
