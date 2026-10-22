-- Readable company names: SEC titles arrive in capitals ("TAIWAN SEMICONDUCTOR
-- MANUFACTURING CO LTD"). Tidy the ones already stored for SEC-keyed companies; names
-- with any lower-case letter were set deliberately and are left alone. The old
-- spelling stays a NAME alias, and lookups ignore case anyway.

UPDATE entities
SET name = torus_display_name(name, attrs_json)
WHERE canonical_key LIKE 'cik:%' AND name = upper(name);
