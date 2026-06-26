"""Lorebook/memory tools + telemetry-persistence wiring (Phase-0 integration).

Async tests run under pytest-asyncio (asyncio_mode=auto). Each uses a fresh
tmp_path data_root, so nothing here touches the live bridge DBs.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sheldon_bridge.db.campaign_store import CampaignStore
from sheldon_bridge.db.telemetry_store import TelemetryStore
from sheldon_bridge.db.manager import DBManager
from sheldon_bridge.tools import memory
from sheldon_bridge.tools.actions import _parse_loc


def _ctx(camp, eos="EOS_1"):
    return {"campaign": camp, "eos_id": eos, "player": None}


async def _campaign(tmp_path):
    mgr = DBManager(str(tmp_path), default_cluster_id="c", default_server_id="s")
    return mgr, CampaignStore(await mgr.campaign("c"))


async def test_remember_recall_forget(tmp_path):
    mgr, camp = await _campaign(tmp_path)
    await camp.upsert_survivor("EOS_1", "Jim")

    r = await memory.remember("Tamed a Rex named Bob", topic="rex bob", ctx=_ctx(camp))
    assert r["success"] and r["permanent"] is False
    rid = r["id"]
    r2 = await memory.remember("Hates Microraptors", permanent=True, ctx=_ctx(camp))
    assert r2["success"] and r2["permanent"] is True

    # recall (no query) lists all, sticky first
    allm = await memory.recall(ctx=_ctx(camp))
    assert allm["count"] == 2 and allm["memories"][0]["permanent"] is True

    # recall with a query surfaces the matching cold entry
    hit = await memory.recall(query="where is rex bob", ctx=_ctx(camp))
    assert any(m["id"] == rid and "Bob" in m["fact"] for m in hit["memories"])

    # forget removes exactly that entry
    assert (await memory.forget(rid, ctx=_ctx(camp)))["success"] is True
    after = await memory.recall(ctx=_ctx(camp))
    assert after["count"] == 1 and all(m["id"] != rid for m in after["memories"])
    await mgr.close()


async def test_memory_is_scoped_per_survivor(tmp_path):
    mgr, camp = await _campaign(tmp_path)
    await camp.upsert_survivor("EOS_1", "Jim")
    await camp.upsert_survivor("EOS_2", "Sue")
    r = await memory.remember("base at the volcano", ctx=_ctx(camp, "EOS_1"))
    # Sue can neither see nor forget Jim's memory
    assert (await memory.recall(ctx=_ctx(camp, "EOS_2")))["count"] == 0
    assert (await memory.forget(r["id"], ctx=_ctx(camp, "EOS_2")))["success"] is False
    # Jim still has it
    assert (await memory.recall(ctx=_ctx(camp, "EOS_1")))["count"] == 1
    await mgr.close()


async def test_memory_tool_guards(tmp_path):
    mgr, camp = await _campaign(tmp_path)
    assert (await memory.remember("x", ctx=None))["success"] is False          # no store
    assert (await memory.remember("   ", ctx=_ctx(camp)))["success"] is False   # empty fact
    assert (await memory.recall(ctx=None))["success"] is False
    assert (await memory.forget("not-a-number", ctx=_ctx(camp)))["success"] is False
    await mgr.close()


def test_parse_loc():
    assert _parse_loc("X=123.5 Y=-67 Z=9000") == {"x": 123.5, "y": -67.0, "z": 9000.0}
    assert _parse_loc("x=1 y=2") == {"x": 1.0, "y": 2.0}
    assert _parse_loc("") is None
    assert _parse_loc(None) is None
    assert _parse_loc("no coords here") is None


async def test_player_state_coalesce_preserves_partial(tmp_path):
    """A partial proactive refresh (only vitals, or only position) must not wipe the
    fields it didn't carry — the COALESCE upsert keeps prior coords/level/name."""
    mgr = DBManager(str(tmp_path), default_cluster_id="c", default_server_id="s")
    tel = TelemetryStore(await mgr.telemetry("c", "s"))
    await tel.upsert_player_state("e", character_name="Jim",
                                  pos={"x": 10, "y": 20, "z": 30}, vitals={"health": 0.9}, level=42)
    await tel.upsert_player_state("e", vitals={"health": 0.4})        # health-only refresh
    ps = await tel.get_player_state("e")
    assert ps["health"] == 0.4 and ps["x"] == 10 and ps["y"] == 20
    assert ps["level"] == 42 and ps["character_name"] == "Jim"
    await mgr.close()
