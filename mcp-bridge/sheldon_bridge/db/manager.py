"""Per-cluster / per-server SQLite connection manager.

Scoping:
  - campaign.db  is CLUSTER-scoped  -> <data_root>/<cluster_id>/campaign.db
  - telemetry.db is SERVER-scoped   -> <data_root>/<cluster_id>/<server_id>/telemetry.db

Connections are opened lazily on first use, cached, migrated-on-open, and closed
together on shutdown. The (cluster_id, server_id) for a connection is resolved from
a source-IP map with config defaults (works with no cook; the mod hardens it later
by sending explicit ids in its auth message).
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from pathlib import Path

import aiosqlite

from sheldon_bridge.db.migrations import apply_migrations

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class Scope:
    """Resolved identity scope for a connection/session."""

    cluster_id: str
    server_id: str


class DBManager:
    """Owns and caches the SQLite connections for every connected cluster/server."""

    def __init__(
        self,
        data_root: str,
        default_cluster_id: str = "default",
        default_server_id: str = "default",
        server_ip_map: dict | None = None,
    ):
        self._root = Path(data_root)
        self._default_cluster = default_cluster_id
        self._default_server = default_server_id
        # source IP -> {"server_id": ..., "cluster_id": ...}
        self._ip_map = server_ip_map or {}
        self._campaign: dict[str, aiosqlite.Connection] = {}
        self._telemetry: dict[tuple[str, str], aiosqlite.Connection] = {}
        self._lock = asyncio.Lock()

    # --- identity resolution -------------------------------------------------

    def resolve_scope(
        self,
        *,
        remote_ip: str | None = None,
        server_id: str | None = None,
        cluster_id: str | None = None,
    ) -> Scope:
        """Resolve (cluster_id, server_id). Explicit ids (from the mod's auth, once
        it sends them) win; otherwise the source-IP map; otherwise the defaults."""
        entry = self._ip_map.get(remote_ip or "", {})
        sid = server_id or entry.get("server_id") or self._default_server
        cid = cluster_id or entry.get("cluster_id") or self._default_cluster
        return Scope(cluster_id=cid, server_id=sid)

    # --- paths ---------------------------------------------------------------

    def campaign_path(self, cluster_id: str) -> Path:
        return self._root / cluster_id / "campaign.db"

    def telemetry_path(self, cluster_id: str, server_id: str) -> Path:
        return self._root / cluster_id / server_id / "telemetry.db"

    # --- connection access (lazy, cached, migrated-on-open) ------------------

    async def _open(self, path: Path, kind: str) -> aiosqlite.Connection:
        path.parent.mkdir(parents=True, exist_ok=True)
        conn = await aiosqlite.connect(str(path))
        conn.row_factory = aiosqlite.Row
        await conn.execute("PRAGMA journal_mode=WAL")
        await conn.execute("PRAGMA busy_timeout=5000")
        await conn.execute("PRAGMA foreign_keys=ON")
        applied = await apply_migrations(conn, kind)
        logger.info("opened %s db %s (migrations applied: %d)", kind, path, applied)
        return conn

    async def campaign(self, cluster_id: str) -> aiosqlite.Connection:
        """Get (opening if needed) the cluster's campaign.db connection."""
        async with self._lock:
            conn = self._campaign.get(cluster_id)
            if conn is None:
                conn = await self._open(self.campaign_path(cluster_id), "campaign")
                self._campaign[cluster_id] = conn
            return conn

    async def telemetry(self, cluster_id: str, server_id: str) -> aiosqlite.Connection:
        """Get (opening if needed) the server's telemetry.db connection."""
        key = (cluster_id, server_id)
        async with self._lock:
            conn = self._telemetry.get(key)
            if conn is None:
                conn = await self._open(self.telemetry_path(cluster_id, server_id), "telemetry")
                self._telemetry[key] = conn
            return conn

    async def close(self) -> None:
        """Close every cached connection. Safe to call once at shutdown."""
        async with self._lock:
            for conn in list(self._campaign.values()) + list(self._telemetry.values()):
                try:
                    await conn.close()
                except Exception as e:  # noqa: BLE001 — best-effort on shutdown
                    logger.warning("error closing db connection: %s", e)
            self._campaign.clear()
            self._telemetry.clear()
