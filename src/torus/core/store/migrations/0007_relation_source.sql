-- Where each relation came from: agent (research session), curator, starter:<pack>@<version>,
-- or user. Starter links are curated defaults that agent-recorded links take precedence over.

ALTER TABLE relations ADD COLUMN source TEXT NOT NULL DEFAULT 'agent';
CREATE INDEX relations_source ON relations (source);
