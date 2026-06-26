"""Data-access for campaign.db (durable, cluster-scoped).

Thin async helpers over a single connection: survivors, conversation history
(with recent-N-within-budget fetch), the lorebook (sticky + Phase-0 keyword
retrieval; semantic embedding retrieval slots in later), and curation state.
"""

from __future__ import annotations

import json
import re
import time

import aiosqlite

from sheldon_bridge.db.locks import serialized_write


def _now() -> float:
    return time.time()


def _est_tokens(text: str) -> int:
    return max(1, len(text) // 4)


def _tokenize(text: str) -> list[str]:
    return [t for t in re.split(r"[^a-z0-9]+", text.lower()) if t]


class CampaignStore:
    """Durable per-survivor memory for one cluster's campaign.db."""

    def __init__(self, conn: aiosqlite.Connection):
        self.c = conn

    # --- survivors -----------------------------------------------------------

    @serialized_write
    async def upsert_survivor(self, eos_id: str, character_name: str = "", tier: str = "player") -> None:
        now = _now()
        await self.c.execute(
            "INSERT INTO survivors(eos_id,character_name,tier,first_seen,last_seen) VALUES(?,?,?,?,?) "
            "ON CONFLICT(eos_id) DO UPDATE SET "
            " character_name=CASE WHEN excluded.character_name<>'' THEN excluded.character_name "
            "                     ELSE survivors.character_name END, "
            " tier=excluded.tier, last_seen=excluded.last_seen",
            (eos_id, character_name, tier, now, now),
        )
        await self.c.commit()

    async def get_survivor(self, eos_id: str) -> aiosqlite.Row | None:
        cur = await self.c.execute("SELECT * FROM survivors WHERE eos_id=?", (eos_id,))
        return await cur.fetchone()

    # --- conversation history ------------------------------------------------

    @serialized_write
    async def add_message(self, eos_id: str, role: str, content: str) -> None:
        await self.c.execute(
            "INSERT INTO conversation_messages(survivor_eos,role,content,token_estimate,created_at) "
            "VALUES(?,?,?,?,?)",
            (eos_id, role, content, _est_tokens(content), _now()),
        )
        await self.c.commit()

    async def recent_messages(self, eos_id: str, limit: int = 20) -> list[aiosqlite.Row]:
        cur = await self.c.execute(
            "SELECT role,content,token_estimate,created_at FROM conversation_messages "
            "WHERE survivor_eos=? ORDER BY id DESC LIMIT ?",
            (eos_id, limit),
        )
        return list(reversed(await cur.fetchall()))  # chronological order

    async def recent_within_budget(self, eos_id: str, token_budget: int, *, max_rows: int = 400) -> list[aiosqlite.Row]:
        """Most-recent messages whose cumulative token estimate fits the budget,
        returned in chronological order (oldest kept -> newest).

        LEAK-04: bound the fetch with a LIMIT so a heavy survivor's whole history isn't loaded
        every turn. 400 recent rows vastly exceeds any realistic token budget, so the Python
        budget-trim below still decides the cutoff for normal sessions — behavior is unchanged,
        just capped for huge histories (the trim is most-recent-first, so a deeper LIMIT would
        never surface anyway once the budget is hit)."""
        cur = await self.c.execute(
            "SELECT role,content,token_estimate FROM conversation_messages "
            "WHERE survivor_eos=? ORDER BY id DESC LIMIT ?",
            (eos_id, max_rows),
        )
        kept: list[aiosqlite.Row] = []
        used = 0
        for row in await cur.fetchall():
            t = row["token_estimate"] or _est_tokens(row["content"])
            if used + t > token_budget:
                break
            kept.append(row)
            used += t
        return list(reversed(kept))

    # --- lorebook ------------------------------------------------------------

    @serialized_write
    async def add_lore(
        self,
        eos_id: str,
        body: str,
        *,
        title: str = "",
        tier: str = "cold",
        keys: list[str] | None = None,
        source: str = "curation",
        salience: float = 0.0,
    ) -> int:
        now = _now()
        cur = await self.c.execute(
            "INSERT INTO lorebook_entries"
            "(survivor_eos,title,body,tier,keys,source,salience,created_at,updated_at) "
            "VALUES(?,?,?,?,?,?,?,?,?)",
            (eos_id, title, body, tier, json.dumps(keys or []), source, salience, now, now),
        )
        await self.c.commit()
        return cur.lastrowid

    async def get_sticky_lore(self, eos_id: str) -> list[aiosqlite.Row]:
        cur = await self.c.execute(
            "SELECT * FROM lorebook_entries WHERE survivor_eos=? AND tier='sticky' "
            "ORDER BY salience DESC, updated_at DESC",
            (eos_id,),
        )
        return await cur.fetchall()

    async def search_lore(
        self, eos_id: str, query: str, *, limit: int = 8, exclude_sticky: bool = True
    ) -> list[aiosqlite.Row]:
        """Phase-0 keyword retrieval: any query token matching body/keys/title.
        Ranked by token-match count, then salience, then recency. Semantic
        embedding retrieval augments/replaces this in a later phase.
        """
        tokens = [t for t in _tokenize(query) if len(t) >= 3]
        if not tokens:
            return []
        sticky_filter = "AND tier <> 'sticky'" if exclude_sticky else ""
        per_token = "(LOWER(body) LIKE ? OR LOWER(keys) LIKE ? OR LOWER(title) LIKE ?)"
        where = " OR ".join([per_token] * len(tokens))
        params: list = [eos_id]
        for tok in tokens:
            like = f"%{tok}%"
            params += [like, like, like]
        cur = await self.c.execute(
            f"SELECT * FROM lorebook_entries WHERE survivor_eos=? {sticky_filter} AND ({where})",
            params,
        )
        rows = await cur.fetchall()

        def score(r: aiosqlite.Row) -> int:
            blob = f"{r['body']} {r['keys']} {r['title']}".lower()
            return sum(tok in blob for tok in tokens)

        rows = sorted(rows, key=lambda r: (score(r), r["salience"], r["updated_at"]), reverse=True)
        return rows[:limit]

    @serialized_write
    async def touch_lore(self, ids: list[int]) -> None:
        """Bump last_used_at for retrieved entries (feeds future warm/cold tiering)."""
        if not ids:
            return
        now = _now()
        await self.c.executemany(
            "UPDATE lorebook_entries SET last_used_at=? WHERE id=?", [(now, i) for i in ids]
        )
        await self.c.commit()

    async def list_lore(self, eos_id: str, *, limit: int = 50) -> list[aiosqlite.Row]:
        """All of a survivor's lore entries (sticky first, then newest) — for recall
        and management tools. Distinct from search_lore, which is relevance-ranked."""
        cur = await self.c.execute(
            "SELECT * FROM lorebook_entries WHERE survivor_eos=? "
            "ORDER BY (tier='sticky') DESC, updated_at DESC LIMIT ?",
            (eos_id, limit),
        )
        return await cur.fetchall()

    @serialized_write
    async def remove_lore(self, eos_id: str, entry_id: int) -> bool:
        """Delete one of a survivor's lore entries. Scoped to the survivor so a tool
        call can never delete another survivor's memory. Returns True if a row went."""
        cur = await self.c.execute(
            "DELETE FROM lorebook_entries WHERE id=? AND survivor_eos=?", (entry_id, eos_id)
        )
        await self.c.commit()
        return cur.rowcount > 0

    # --- curation state ------------------------------------------------------

    async def get_curation_state(self, eos_id: str) -> aiosqlite.Row | None:
        cur = await self.c.execute("SELECT * FROM curation_state WHERE survivor_eos=?", (eos_id,))
        return await cur.fetchone()

    @serialized_write
    async def set_curation_state(
        self,
        eos_id: str,
        *,
        rolling_diff_cursor: str | None = None,
        thread_tracker_state: str | None = None,
    ) -> None:
        await self.c.execute(
            "INSERT INTO curation_state"
            "(survivor_eos,rolling_diff_cursor,thread_tracker_state,last_curation_run) "
            "VALUES(?,?,?,?) "
            "ON CONFLICT(survivor_eos) DO UPDATE SET "
            " rolling_diff_cursor=COALESCE(excluded.rolling_diff_cursor,curation_state.rolling_diff_cursor), "
            " thread_tracker_state=COALESCE(excluded.thread_tracker_state,curation_state.thread_tracker_state), "
            " last_curation_run=excluded.last_curation_run",
            (eos_id, rolling_diff_cursor, thread_tracker_state, _now()),
        )
        await self.c.commit()
