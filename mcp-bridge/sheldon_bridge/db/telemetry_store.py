"""Data-access for telemetry.db (volatile, server-scoped).

Thin async helpers over a single connection: current player state (upsert), the
dino index with an atomic snapshot-swap + R-tree "within radius" query, and a
small server-state key/value table. Everything here is disposable and refreshed
from the game; durable facts get promoted to the lorebook by curation.
"""

from __future__ import annotations

import json
import math
import time

import aiosqlite

from sheldon_bridge.db.locks import conn_write_lock, serialized_write


def _now() -> float:
    return time.time()


def _dedupe_by_uid(dinos: list[dict]) -> list[dict]:
    """Drop earlier duplicates of the same dino_uid (keep the LAST = freshest occurrence),
    preserving order. A single scan reply can list one dino twice (octree boundary overlap,
    recursive subdivision) — without this the second INSERT hits the UNIQUE dino_uid
    constraint, raises IntegrityError, and aborts the whole census pass (H2). Rows with no
    uid are kept as-is (SQLite UNIQUE permits multiple NULLs, so they never collide)."""
    pos: dict = {}
    out: list[dict] = []
    for d in dinos:
        uid = d.get("dino_uid")
        if not uid:
            out.append(d)
        elif uid in pos:
            out[pos[uid]] = d  # newer data wins, kept at the earlier slot
        else:
            pos[uid] = len(out)
            out.append(d)
    return out


class TelemetryStore:
    """Live 'world' state for one server's telemetry.db."""

    def __init__(self, conn: aiosqlite.Connection):
        self.c = conn

    # --- player state --------------------------------------------------------

    @serialized_write
    async def upsert_player_state(
        self,
        eos_id: str,
        *,
        character_name: str = "",
        pos: dict | None = None,
        vitals: dict | None = None,
        level: int | None = None,
        region: str | None = None,
    ) -> None:
        pos = pos or {}
        vitals = vitals or {}
        # COALESCE-preserve: a field passed as None keeps its stored value rather than
        # wiping it, so a PARTIAL write (proactive refresh where only vitals or only
        # position came back, or a mod pushing a subset) never clobbers good prior
        # state. captured_at always advances; character_name preserved unless non-empty.
        await self.c.execute(
            "INSERT INTO player_state"
            "(eos_id,character_name,x,y,z,yaw,health,food,water,stamina,weight,torpor,oxygen,level,region,captured_at) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?) "
            "ON CONFLICT(eos_id) DO UPDATE SET "
            " character_name=CASE WHEN excluded.character_name<>'' THEN excluded.character_name "
            "                     ELSE player_state.character_name END, "
            " x=COALESCE(excluded.x,player_state.x),y=COALESCE(excluded.y,player_state.y),"
            " z=COALESCE(excluded.z,player_state.z),yaw=COALESCE(excluded.yaw,player_state.yaw), "
            " health=COALESCE(excluded.health,player_state.health),food=COALESCE(excluded.food,player_state.food),"
            " water=COALESCE(excluded.water,player_state.water),stamina=COALESCE(excluded.stamina,player_state.stamina), "
            " weight=COALESCE(excluded.weight,player_state.weight),torpor=COALESCE(excluded.torpor,player_state.torpor),"
            " oxygen=COALESCE(excluded.oxygen,player_state.oxygen),"
            " level=COALESCE(excluded.level,player_state.level),region=COALESCE(excluded.region,player_state.region), "
            " captured_at=excluded.captured_at",
            (
                eos_id, character_name,
                pos.get("x"), pos.get("y"), pos.get("z"), pos.get("yaw"),
                vitals.get("health"), vitals.get("food"), vitals.get("water"),
                vitals.get("stamina"), vitals.get("weight"), vitals.get("torpor"),
                vitals.get("oxygen"),
                level, region, _now(),
            ),
        )
        await self.c.commit()

    async def get_player_state(self, eos_id: str) -> aiosqlite.Row | None:
        cur = await self.c.execute("SELECT * FROM player_state WHERE eos_id=?", (eos_id,))
        return await cur.fetchone()

    async def recent_players(self, since: float) -> list[aiosqlite.Row]:
        """player_state rows captured since `since` (epoch s) with a known position — the
        census active layer scans around these last-known positions to keep near-player dinos
        fresh (woken dinos read full stats, and that's where the world actually changes)."""
        cur = await self.c.execute(
            "SELECT eos_id,x,y,z,captured_at FROM player_state WHERE captured_at>=? AND x IS NOT NULL",
            (since,))
        return await cur.fetchall()

    # --- dino index (atomic snapshot-swap + spatial) -------------------------

    @serialized_write
    async def swap_dino_index(self, dinos: list[dict]) -> int:
        """Atomically replace the whole dino index (and its R-tree).

        Each dino dict may carry: dino_uid, species, name, level, gender, tribe,
        imprint, base_stats(dict), x, y, z. Returns the number of rows written.
        Wrapped in one implicit transaction so readers never see a half-swap.
        """
        now = _now()
        dinos = _dedupe_by_uid(dinos)  # H2: a dup uid in one batch would abort the swap
        try:
            await self.c.execute("DELETE FROM dino_index")
            await self.c.execute("DELETE FROM dino_rtree")
            for i, d in enumerate(dinos, start=1):
                await self.c.execute(
                    "INSERT INTO dino_index"
                    "(id,dino_uid,species,name,level,gender,tribe,imprint,base_stats,x,y,z,captured_at) "
                    "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        i, d.get("dino_uid"), d.get("species"), d.get("name"), d.get("level"),
                        d.get("gender"), d.get("tribe"), d.get("imprint"),
                        json.dumps(d.get("base_stats") or {}),
                        d.get("x"), d.get("y"), d.get("z"), now,
                    ),
                )
                x = d.get("x") or 0.0
                y = d.get("y") or 0.0
                await self.c.execute(
                    "INSERT INTO dino_rtree(id,minx,maxx,miny,maxy) VALUES(?,?,?,?,?)",
                    (i, x, x, y, y),
                )
            await self.c.commit()
        except Exception:
            await self.c.rollback()
            raise
        return len(dinos)

    @serialized_write
    async def upsert_region(self, bounds: tuple, dinos: list[dict]) -> int:
        """Replace the dino index within ONE tile region (departure-sweep + insert).

        `bounds` = (minx, miny, maxx, maxy). Used by the tiled census so the index
        ACCUMULATES across tiles instead of being wiped (that's what swap_dino_index
        does for a single full snapshot). Everything currently inside the tile is
        cleared first — this is the departure sweep, so dinos that died or wandered
        out of the tile disappear — then the fresh scan for this tile is inserted.
        Rows in other tiles are untouched. A dino that moved INTO this tile from
        elsewhere is de-duped by its UNIQUE uid (its stale row is cleared too).
        id is INTEGER PRIMARY KEY (rowid): INSERT omits it, the rtree row uses the
        resulting lastrowid. One transaction so readers never see a half-region.
        Returns rows written for this tile.
        """
        minx, miny, maxx, maxy = bounds
        now = _now()
        dinos = _dedupe_by_uid(dinos)  # H2: a dup uid in one tile batch would abort the region
        try:
            cur = await self.c.execute(
                "SELECT id FROM dino_index WHERE x>=? AND x<? AND y>=? AND y<?",
                (minx, maxx, miny, maxy))
            old = [r["id"] for r in await cur.fetchall()]
            uids = [d.get("dino_uid") for d in dinos if d.get("dino_uid")]
            if uids:
                qs = ",".join("?" * len(uids))
                cur = await self.c.execute(
                    f"SELECT id FROM dino_index WHERE dino_uid IN ({qs})", uids)
                old += [r["id"] for r in await cur.fetchall()]
            if old:
                qs = ",".join("?" * len(old))
                await self.c.execute(f"DELETE FROM dino_index WHERE id IN ({qs})", old)
                await self.c.execute(f"DELETE FROM dino_rtree WHERE id IN ({qs})", old)
            n = 0
            for d in dinos:
                cur = await self.c.execute(
                    "INSERT INTO dino_index"
                    "(dino_uid,species,name,level,gender,tribe,imprint,base_stats,x,y,z,captured_at) "
                    "VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        d.get("dino_uid"), d.get("species"), d.get("name"), d.get("level"),
                        d.get("gender"), d.get("tribe"), d.get("imprint"),
                        json.dumps(d.get("base_stats") or {}),
                        d.get("x"), d.get("y"), d.get("z"), now,
                    ),
                )
                rid = cur.lastrowid
                x = d.get("x") or 0.0
                y = d.get("y") or 0.0
                await self.c.execute(
                    "INSERT INTO dino_rtree(id,minx,maxx,miny,maxy) VALUES(?,?,?,?,?)",
                    (rid, x, x, y, y),
                )
                n += 1
            await self.c.commit()
        except Exception:
            await self.c.rollback()
            raise
        return n

    async def dinos_within(self, x: float, y: float, radius: float, *, limit: int = 50) -> list[aiosqlite.Row]:
        """Dinos within `radius` of (x, y): R-tree bounding-box prefilter, then an
        exact circle test, nearest first."""
        # DBSTORE-01: hold the connection write-lock so this read can't interleave inside
        # the census upsert_region's open DELETE-then-INSERT transaction (shared conn).
        async with conn_write_lock(self.c):
            cur = await self.c.execute(
                "SELECT d.* FROM dino_rtree r JOIN dino_index d ON d.id=r.id "
                "WHERE r.minx>=? AND r.maxx<=? AND r.miny>=? AND r.maxy<=?",
                (x - radius, x + radius, y - radius, y + radius),
            )
            rows = await cur.fetchall()
        scored = []
        r2 = radius * radius
        for row in rows:
            dx = (row["x"] or 0.0) - x
            dy = (row["y"] or 0.0) - y
            d2 = dx * dx + dy * dy
            if d2 <= r2:
                scored.append((row, d2))
        scored.sort(key=lambda t: t[1])
        return [row for row, _ in scored[:limit]]

    async def count_dinos(self, *, species: str | None = None, tribe: str | None = None) -> int:
        clauses, params = [], []
        if species:
            clauses.append("species=?")
            params.append(species)
        if tribe:
            clauses.append("tribe=?")
            params.append(tribe)
        where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
        # DBSTORE-01: serialize against an in-flight upsert_region transaction (shared conn).
        async with conn_write_lock(self.c):
            cur = await self.c.execute(f"SELECT COUNT(*) AS n FROM dino_index {where}", params)
            return (await cur.fetchone())["n"]

    async def search_dinos(self, *, species: str | None = None, tribe: str | None = None,
                           min_level: int = 0, gender: str | None = None,
                           limit: int = 20) -> list[aiosqlite.Row]:
        """Map-wide dino_index search, highest level first — the 'best X anywhere' /
        'where are the gigas' query the live tribe getter (get_dinos) can't answer.
        SQLite ranks NULL level last under DESC, so unleveled rows fall to the bottom."""
        clauses, params = [], []
        if species:
            clauses.append("species LIKE ?"); params.append(f"%{species}%")
        if tribe:
            clauses.append("tribe=?"); params.append(tribe)
        if gender:
            clauses.append("gender=?"); params.append(gender)
        if min_level:
            clauses.append("level>=?"); params.append(min_level)
        where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
        params.append(limit)
        # DBSTORE-01: the live find_dinos reader — hold the write-lock so it can't observe the
        # half-cleared tile mid-upsert_region (a same-conn reader sees the uncommitted DELETE).
        async with conn_write_lock(self.c):
            cur = await self.c.execute(
                f"SELECT * FROM dino_index {where} ORDER BY level DESC LIMIT ?", params)
            return await cur.fetchall()

    async def newest_capture(self) -> float | None:
        """Timestamp of the freshest dino row — for staleness display to the LLM."""
        cur = await self.c.execute("SELECT MAX(captured_at) AS t FROM dino_index")
        return (await cur.fetchone())["t"]

    @serialized_write
    async def reap_stale(self, older_than: float) -> int:
        """Delete dino rows not refreshed since `older_than` (epoch seconds). Returns rows reaped.

        captured_at is effectively a last_seen — upsert_region DELETEs+re-INSERTs a dino on
        every re-scan, bumping it to now — so a row older than the cutoff is one the census
        has STOPPED seeing (died / tamed away from wild / wiped, or sitting in a region that
        dropped out of coverage). upsert_region's per-tile departure-sweep already clears dinos
        that vanish from a tile that gets RE-scanned; this TTL pass is the backstop for the
        cases that sweep can't see: bounds shrank, a wipe landed before the next full pass, or
        the populated extent moved. The dino_uid identity is what makes captured_at trustworthy
        as last_seen (a re-seen dino keeps ONE row, refreshed — never a stale duplicate)."""
        cur = await self.c.execute(
            "SELECT id FROM dino_index WHERE captured_at < ?", (older_than,))
        ids = [r["id"] for r in await cur.fetchall()]
        if not ids:
            return 0
        try:
            qs = ",".join("?" * len(ids))
            await self.c.execute(f"DELETE FROM dino_index WHERE id IN ({qs})", ids)
            await self.c.execute(f"DELETE FROM dino_rtree WHERE id IN ({qs})", ids)
            await self.c.commit()
        except Exception:
            await self.c.rollback()
            raise
        return len(ids)

    # --- server state (key/value) -------------------------------------------

    @serialized_write
    async def set_server_state(self, key: str, value) -> None:
        await self.c.execute(
            "INSERT INTO server_state(key,value,captured_at) VALUES(?,?,?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value, captured_at=excluded.captured_at",
            (key, str(value), _now()),
        )
        await self.c.commit()

    async def get_server_state(self, key: str | None = None):
        if key is None:
            cur = await self.c.execute("SELECT key,value FROM server_state")
            return {r["key"]: r["value"] for r in await cur.fetchall()}
        cur = await self.c.execute("SELECT value FROM server_state WHERE key=?", (key,))
        row = await cur.fetchone()
        return row["value"] if row else None
