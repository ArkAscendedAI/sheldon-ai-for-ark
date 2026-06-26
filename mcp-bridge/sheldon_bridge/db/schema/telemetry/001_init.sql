-- telemetry.db — volatile, server-scoped (per map). Disposable; rebuilt from the game.
-- "The world": current player state, the tamed/known dino index (+ spatial rtree),
-- and assorted server state. Durable facts get promoted to the lorebook by curation.

CREATE TABLE IF NOT EXISTS player_state (
    eos_id         TEXT PRIMARY KEY,
    character_name TEXT NOT NULL DEFAULT '',
    x REAL, y REAL, z REAL, yaw REAL,
    health REAL, food REAL, water REAL, stamina REAL, weight REAL, torpor REAL,
    level          INTEGER,
    region         TEXT,
    captured_at    REAL NOT NULL
);

-- dino_index.id is shared with the rtree rowid, so a spatial hit joins straight back.
CREATE TABLE IF NOT EXISTS dino_index (
    id          INTEGER PRIMARY KEY,
    dino_uid    TEXT UNIQUE,                       -- stable in-game id
    species     TEXT,
    name        TEXT,
    level       INTEGER,
    gender      TEXT,
    tribe       TEXT,
    imprint     REAL,
    base_stats  TEXT,                              -- JSON
    x REAL, y REAL, z REAL,
    captured_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_dino_species ON dino_index (species);
CREATE INDEX IF NOT EXISTS idx_dino_tribe   ON dino_index (tribe);

-- 2D spatial index for "dinos within radius"; id == dino_index.id.
CREATE VIRTUAL TABLE IF NOT EXISTS dino_rtree USING rtree (
    id, minx, maxx, miny, maxy
);

CREATE TABLE IF NOT EXISTS server_state (
    key         TEXT PRIMARY KEY,                  -- time_of_day | weather | day | online_players | ...
    value       TEXT,
    captured_at REAL NOT NULL
);
