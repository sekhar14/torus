-- Desktop notifications: when an alert was notified (NULL: not yet). Alerts from before
-- this migration count as notified, so upgrading doesn't notify old alerts.

ALTER TABLE alerts ADD COLUMN notified_at TEXT;
UPDATE alerts SET notified_at = created_at;
CREATE INDEX alerts_notify ON alerts (status, notified_at);
