"""Forward-only SQL migration runner for the bridge's SQLite stores.

Each store ("campaign" | "telemetry") has a folder of numbered .sql files under
schema/<kind>/. Applied versions are tracked in a schema_migrations table inside
that DB, so apply_migrations() is idempotent and safe to call on every open.
"""

from __future__ import annotations

import logging
import time
from pathlib import Path

import aiosqlite

logger = logging.getLogger(__name__)

SCHEMA_DIR = Path(__file__).parent / "schema"


async def apply_migrations(db: aiosqlite.Connection, kind: str) -> int:
    """Apply any pending migrations for ``kind`` to an open connection.

    Returns the number of migrations newly applied. Idempotent — already-applied
    versions are skipped.
    """
    kind_dir = SCHEMA_DIR / kind
    if not kind_dir.is_dir():
        raise ValueError(f"No schema directory for kind {kind!r} ({kind_dir})")

    await db.execute(
        "CREATE TABLE IF NOT EXISTS schema_migrations ("
        "version TEXT PRIMARY KEY, applied_at REAL NOT NULL)"
    )
    await db.commit()

    cursor = await db.execute("SELECT version FROM schema_migrations")
    applied = {row[0] for row in await cursor.fetchall()}
    await cursor.close()

    count = 0
    for sql_file in sorted(kind_dir.glob("*.sql")):
        version = sql_file.name
        if version in applied:
            continue
        await db.executescript(sql_file.read_text())
        await db.execute(
            "INSERT INTO schema_migrations (version, applied_at) VALUES (?, ?)",
            (version, time.time()),
        )
        await db.commit()
        count += 1
        logger.info("[%s] applied migration %s", kind, version)

    return count
