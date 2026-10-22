-- Undo: a rejected hypothesis can be withdrawn (kept for history, no longer recalled).

ALTER TABLE rejected_hypotheses ADD COLUMN withdrawn_at TEXT;
ALTER TABLE rejected_hypotheses ADD COLUMN withdrawn_reason TEXT;
CREATE INDEX session_records_at ON session_records (at);
