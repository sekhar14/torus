-- When a thesis is due: its horizon ("12m", "FY2026", "Q3 2026") as a date, so the
-- audit can flag theses past their horizon. NULL when the horizon isn't understood.

ALTER TABLE theses ADD COLUMN horizon_date TEXT;
UPDATE theses SET horizon_date = torus_horizon_date(horizon, created_at);
