"""Regression tests for two audit-pass DB fixes (branch v9-staging):

  * DBSTORE-01 — torn read: the live `search_dinos` reader (find_dinos tool) and the
    test-only `dinos_within`/`count_dinos` must NOT observe the half-cleared dino_index
    while the census's `upsert_region` is mid DELETE-then-INSERT on the SAME shared
    aiosqlite connection. The fix has the readers take the connection write-lock.
  * LEAK-04 — unbounded history: `recent_within_budget` must bound its SQL fetch with a
    LIMIT instead of SELECTing a heavy survivor's entire conversation every turn.

Async tests run under pytest-asyncio (asyncio_mode=auto). Each uses a fresh tmp_path
data_root, so nothing here touches the live bridge DBs.
"""

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))  # put mcp-bridge/ on the path

from sheldon_bridge.db.campaign_store import CampaignStore
from sheldon_bridge.db.locks import conn_write_lock
from sheldon_bridge.db.manager import DBManager
from sheldon_bridge.db.telemetry_store import TelemetryStore


def _mgr(tmp_path):
    return DBManager(
        str(tmp_path),
        default_cluster_id="examplecluster",
        default_server_id="ragnarok",
    )


# --- DBSTORE-01 torn read -------------------------------------------------------

async def test_search_dinos_blocks_on_open_write_transaction(tmp_path):
    """A coroutine holding the connection write-lock (as upsert_region does for its whole
    DELETE→INSERT→commit unit) must block search_dinos until it commits — so the reader
    can never run while the index is half-cleared. We prove serialization by asserting the
    reader is still pending while the lock is held, and only completes once it's released."""
    mgr = _mgr(tmp_path)
    tel = TelemetryStore(await mgr.telemetry("examplecluster", "ragnarok"))
    await tel.upsert_region((0, 0, 100000, 100000), [
        {"dino_uid": "r1", "species": "Rex", "name": "Rexy", "level": 150, "x": 100, "y": 100},
        {"dino_uid": "g1", "species": "Giga", "name": "G", "level": 290, "x": 200, "y": 200},
    ])

    lock = conn_write_lock(tel.c)
    await lock.acquire()  # stand in for upsert_region's in-flight write transaction
    try:
        reader = asyncio.ensure_future(tel.search_dinos())
        await asyncio.sleep(0)  # give the reader a chance to run; it must NOT proceed
        assert not reader.done(), "search_dinos ran while a write held the lock (torn-read window)"
    finally:
        lock.release()  # the writer "commits" and frees the lock

    rows = await asyncio.wait_for(reader, timeout=2.0)  # now the reader gets through
    assert sorted(r["name"] for r in rows) == ["G", "Rexy"]  # full, consistent view
    await mgr.close()


async def test_search_dinos_never_sees_half_cleared_tile(tmp_path):
    """End-to-end torn-read guard: run a real upsert_region (DELETE-then-INSERT) concurrently
    with search_dinos on the SHARED connection. Without the read-lock a same-conn reader can
    observe the uncommitted DELETE (an empty/partial index); with it the reader serializes and
    always sees a whole snapshot — never zero rows for a non-empty index."""
    mgr = _mgr(tmp_path)
    tel = TelemetryStore(await mgr.telemetry("examplecluster", "ragnarok"))
    await tel.upsert_region((0, 0, 1000, 1000), [
        {"dino_uid": f"d{i}", "species": "Rex", "name": f"R{i}", "level": 100 + i,
         "x": 10 + i, "y": 10 + i} for i in range(8)
    ])

    async def rescan():
        # same tile, fresh contents -> departure-sweep DELETE then re-INSERT, one transaction
        for _ in range(15):
            await tel.upsert_region((0, 0, 1000, 1000), [
                {"dino_uid": f"d{i}", "species": "Rex", "name": f"R{i}", "level": 100 + i,
                 "x": 10 + i, "y": 10 + i} for i in range(8)
            ])

    async def reads():
        seen_counts = []
        for _ in range(40):
            rows = await tel.search_dinos(species="Rex")
            seen_counts.append(len(rows))
            await asyncio.sleep(0)
        return seen_counts

    writer = asyncio.ensure_future(rescan())
    counts = await reads()
    await writer
    # the tile is always fully populated (8) across the swap; a torn read would show <8 (esp. 0)
    assert all(c == 8 for c in counts), f"observed a half-cleared index: {sorted(set(counts))}"
    await mgr.close()


async def test_dinos_within_and_count_acquire_lock(tmp_path):
    """The test-only spatial readers are fixed too: both block while the write-lock is held."""
    mgr = _mgr(tmp_path)
    tel = TelemetryStore(await mgr.telemetry("examplecluster", "ragnarok"))
    await tel.upsert_region((0, 0, 1000, 1000), [
        {"dino_uid": "d1", "species": "Rex", "name": "A", "x": 50, "y": 50},
        {"dino_uid": "d2", "species": "Rex", "name": "B", "x": 60, "y": 60},
    ])

    lock = conn_write_lock(tel.c)
    await lock.acquire()
    try:
        within = asyncio.ensure_future(tel.dinos_within(50, 50, 100))
        cnt = asyncio.ensure_future(tel.count_dinos(species="Rex"))
        await asyncio.sleep(0)
        assert not within.done() and not cnt.done(), "spatial reader ran mid-write-transaction"
    finally:
        lock.release()

    assert len(await asyncio.wait_for(within, timeout=2.0)) == 2
    assert await asyncio.wait_for(cnt, timeout=2.0) == 2
    await mgr.close()


# --- LEAK-04 unbounded history --------------------------------------------------

async def test_recent_within_budget_bounds_the_fetch(tmp_path):
    """A heavy survivor's whole history must not be loaded: the SQL is bounded by max_rows.
    With a huge token_budget (so the Python trim never fires), the result is capped at the
    fetch limit rather than returning every stored message."""
    mgr = _mgr(tmp_path)
    camp = CampaignStore(await mgr.campaign("examplecluster"))
    await camp.upsert_survivor("e", "Jim")
    for i in range(50):
        await camp.add_message("e", "user" if i % 2 == 0 else "assistant", f"chat {i}")

    huge_budget = 10_000_000  # far exceeds 50 tiny messages -> trim never triggers
    # bounded fetch caps the rows even though the budget would admit all 50
    rows = await camp.recent_within_budget("e", huge_budget, max_rows=10)
    assert len(rows) == 10, "fetch was not bounded by max_rows (LEAK-04 unbounded SELECT)"
    # bound preserves most-recent-first selection, returned oldest-kept -> newest
    assert rows[-1]["content"] == "chat 49"   # newest stored message is present
    assert rows[0]["content"] == "chat 40"    # exactly the 10 most-recent, chronological
    await mgr.close()


async def test_recent_within_budget_default_limit_preserves_normal_sessions(tmp_path):
    """For a normal-sized history the default LIMIT (400) is never the binding constraint, so
    the budget-trim still decides the cutoff — behavior is unchanged for real sessions."""
    mgr = _mgr(tmp_path)
    camp = CampaignStore(await mgr.campaign("examplecluster"))
    await camp.upsert_survivor("e", "Jim")
    body = "x" * 40  # _est_tokens = 40//4 = 10 tokens each, deterministic
    for i in range(12):
        await camp.add_message("e", "user", f"{i:02d}{body}")

    # tight budget (35 tok -> 3 messages @ 10-11 tok) -> the Python trim limits the set
    # (most-recent-first), NOT the default 400-row LIMIT, which is never the binding cap here
    rows = await camp.recent_within_budget("e", token_budget=35)
    assert 0 < len(rows) < 12  # trimmed by budget, newest kept
    assert rows[-1]["content"].startswith("11")  # newest survives, chronological order preserved
    # all 12 fit comfortably under the default 400-row cap when budget is generous
    allrows = await camp.recent_within_budget("e", token_budget=1_000_000)
    assert len(allrows) == 12 and allrows[0]["content"].startswith("00")
    await mgr.close()
