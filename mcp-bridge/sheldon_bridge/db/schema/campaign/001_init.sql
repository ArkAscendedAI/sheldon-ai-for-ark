-- campaign.db — durable, cluster-scoped.
-- The implant AI's memory of a survivor; follows them across the cluster.
-- Survivor identity, lorebook (sticky/warm/cold), conversation history, curation state.

CREATE TABLE IF NOT EXISTS survivors (
    eos_id            TEXT PRIMARY KEY,
    character_name    TEXT NOT NULL DEFAULT '',
    tier              TEXT NOT NULL DEFAULT 'player',
    first_seen        REAL NOT NULL,
    last_seen         REAL NOT NULL,
    persona_overrides TEXT                         -- JSON, nullable
);

CREATE TABLE IF NOT EXISTS lorebook_entries (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    survivor_eos  TEXT NOT NULL,
    title         TEXT NOT NULL DEFAULT '',
    body          TEXT NOT NULL,
    tier          TEXT NOT NULL DEFAULT 'cold',    -- sticky | warm | cold
    keys          TEXT NOT NULL DEFAULT '[]',      -- JSON array of keyword strings
    source        TEXT NOT NULL DEFAULT 'curation',-- curation | chat | manual
    salience      REAL NOT NULL DEFAULT 0.0,
    embedding     BLOB,                            -- nullable until the semantic phase
    created_at    REAL NOT NULL,
    updated_at    REAL NOT NULL,
    last_used_at  REAL
);
CREATE INDEX IF NOT EXISTS idx_lore_survivor      ON lorebook_entries (survivor_eos);
CREATE INDEX IF NOT EXISTS idx_lore_survivor_tier ON lorebook_entries (survivor_eos, tier);

CREATE TABLE IF NOT EXISTS conversation_messages (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    survivor_eos   TEXT NOT NULL,
    role           TEXT NOT NULL,                  -- user | assistant | tool
    content        TEXT NOT NULL,
    token_estimate INTEGER NOT NULL DEFAULT 0,
    created_at     REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_msg_survivor ON conversation_messages (survivor_eos, id);

CREATE TABLE IF NOT EXISTS curation_state (
    survivor_eos         TEXT PRIMARY KEY,
    rolling_diff_cursor  TEXT,
    thread_tracker_state TEXT,
    last_curation_run    REAL
);
