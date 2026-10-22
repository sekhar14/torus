-- Scorecard: which instrument a thesis is about, from what price, against what
-- benchmark; daily closes; and each thesis's outcome (for calibration).

ALTER TABLE theses ADD COLUMN instrument TEXT;     -- ticker symbol, e.g. NVDA
ALTER TABLE theses ADD COLUMN entry_price REAL;    -- NULL: the close on entry_at
ALTER TABLE theses ADD COLUMN entry_at TEXT;       -- NULL: when the thesis was recorded
ALTER TABLE theses ADD COLUMN benchmark TEXT;      -- NULL: the `benchmark` setting or SPY

CREATE TABLE prices (
    symbol TEXT NOT NULL,
    day    TEXT NOT NULL,                          -- YYYY-MM-DD
    close  REAL NOT NULL CHECK (close > 0),        -- adjusted for splits and dividends
    source TEXT NOT NULL,
    PRIMARY KEY (symbol, day)
);

CREATE TABLE thesis_outcomes (
    thesis_id     TEXT PRIMARY KEY REFERENCES theses (id),
    outcome       TEXT NOT NULL,                   -- right | wrong | unclear
    method        TEXT NOT NULL,                   -- status | horizon | user
    decided_at    TEXT NOT NULL,
    stance_return REAL,                            -- excess return in the thesis's direction
    note          TEXT
);
