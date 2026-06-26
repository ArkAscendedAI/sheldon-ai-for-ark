-- Index on captured_at for the stale-entry reaper (TelemetryStore.reap_stale): the
-- census deletes dino rows whose captured_at (== last_seen — upsert_region re-inserts a
-- dino on every re-scan, bumping it to now) is older than a cutoff. Without this index the
-- reaper full-scans dino_index on every pass.
CREATE INDEX IF NOT EXISTS idx_dino_captured ON dino_index (captured_at);
