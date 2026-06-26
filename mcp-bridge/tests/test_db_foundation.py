"""Phase-0 persistence foundation tests (migrations, manager, stores, assembler).

Async tests run under pytest-asyncio (asyncio_mode=auto in pyproject). Each uses
a fresh tmp_path data_root, so nothing here touches the live bridge DBs.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))  # put mcp-bridge/ on the path

import aiosqlite

from sheldon_bridge.db.assembler import ContextAssembler, _STALE_AFTER_S
from sheldon_bridge.db.campaign_store import CampaignStore
from sheldon_bridge.db.manager import DBManager, Scope
from sheldon_bridge.db.migrations import apply_migrations
from sheldon_bridge.db.telemetry_store import TelemetryStore


def _mgr(tmp_path):
    return DBManager(
        str(tmp_path),
        default_cluster_id="examplecluster",
        default_server_id="ragnarok",
        server_ip_map={"10.0.0.11": {"server_id": "valguero", "cluster_id": "examplecluster"}},
    )


async def test_migrations_idempotent(tmp_path):
    async with aiosqlite.connect(str(tmp_path / "c.db")) as db:
        assert await apply_migrations(db, "campaign") == 1
        assert await apply_migrations(db, "campaign") == 0  # already applied
    async with aiosqlite.connect(str(tmp_path / "t.db")) as db:
        assert await apply_migrations(db, "telemetry") == 3  # 001_init + 002_dino_captured_at_idx + 003_oxygen
        assert await apply_migrations(db, "telemetry") == 0


def test_scope_resolution(tmp_path):
    mgr = _mgr(tmp_path)
    assert mgr.resolve_scope(remote_ip="10.0.0.9") == Scope("examplecluster", "ragnarok")  # defaults
    assert mgr.resolve_scope(remote_ip="10.0.0.11") == Scope("examplecluster", "valguero")  # ip map
    assert mgr.resolve_scope(server_id="ab", cluster_id="c2") == Scope("c2", "ab")  # explicit wins


async def test_manager_scoping_and_layout(tmp_path):
    mgr = _mgr(tmp_path)
    assert (await mgr.campaign("examplecluster")) is (await mgr.campaign("examplecluster"))  # cached per cluster
    rag = await mgr.telemetry("examplecluster", "ragnarok")
    val = await mgr.telemetry("examplecluster", "valguero")
    assert rag is not val  # distinct per server
    assert (tmp_path / "examplecluster" / "campaign.db").exists()
    assert (tmp_path / "examplecluster" / "ragnarok" / "telemetry.db").exists()
    assert (tmp_path / "examplecluster" / "valguero" / "telemetry.db").exists()
    await mgr.close()


async def test_campaign_store(tmp_path):
    mgr = _mgr(tmp_path)
    camp = CampaignStore(await mgr.campaign("examplecluster"))
    await camp.upsert_survivor("e", "Jim", tier="superadmin")
    await camp.upsert_survivor("e", "", tier="admin")  # blank name preserved, tier updated
    s = await camp.get_survivor("e")
    assert s["character_name"] == "Jim" and s["tier"] == "admin"

    await camp.add_lore("e", "sticky body", tier="sticky")
    rid = await camp.add_lore("e", "Tamed a Rex named Bob", keys=["rex", "bob"], salience=0.9)
    await camp.add_lore("e", "base near obelisk", keys=["base"])
    assert len(await camp.get_sticky_lore("e")) == 1
    hits = await camp.search_lore("e", "where is rex bob")
    assert hits[0]["id"] == rid and all(h["tier"] != "sticky" for h in hits)

    for i in range(4):
        await camp.add_message("e", "user" if i % 2 == 0 else "assistant", f"chat {i}")
    recent = await camp.recent_messages("e", limit=2)
    assert len(recent) == 2 and recent[-1]["content"] == "chat 3"

    await camp.set_curation_state("e", rolling_diff_cursor="cur1")
    await camp.set_curation_state("e", thread_tracker_state="tt1")  # COALESCE keeps cur1
    cs = await camp.get_curation_state("e")
    assert cs["rolling_diff_cursor"] == "cur1" and cs["thread_tracker_state"] == "tt1"
    await mgr.close()


async def test_telemetry_store_spatial_and_swap(tmp_path):
    mgr = _mgr(tmp_path)
    tel = TelemetryStore(await mgr.telemetry("examplecluster", "ragnarok"))
    await tel.upsert_player_state("e", character_name="Jim", pos={"x": 1, "y": 2}, vitals={"health": 0.5})
    await tel.upsert_player_state("e", pos={"x": 9, "y": 9}, vitals={"health": 0.1})  # name preserved
    ps = await tel.get_player_state("e")
    assert ps["character_name"] == "Jim" and ps["x"] == 9 and ps["health"] == 0.1

    assert await tel.swap_dino_index([
        {"dino_uid": "d1", "species": "Rex", "name": "Bob", "x": 120, "y": 205},
        {"dino_uid": "d2", "species": "Argy", "name": "A", "x": 900, "y": 900},
        {"dino_uid": "d3", "species": "Rex", "name": "Rexy", "x": 130, "y": 215},
    ]) == 3
    assert await tel.count_dinos(species="Rex") == 2
    near = await tel.dinos_within(115, 205, 50)
    assert sorted(d["name"] for d in near) == ["Bob", "Rexy"] and near[0]["name"] == "Bob"  # nearest first
    assert await tel.swap_dino_index([{"dino_uid": "x", "species": "Giga", "name": "G", "x": 0, "y": 0}]) == 1
    assert await tel.count_dinos() == 1  # wholesale replace
    await mgr.close()


async def test_telemetry_upsert_region(tmp_path):
    """The tiled census path: upsert_region accumulates across tiles, departure-sweeps a
    re-scanned tile, and de-dupes a dino that moved between tiles (UNIQUE uid)."""
    mgr = _mgr(tmp_path)
    tel = TelemetryStore(await mgr.telemetry("examplecluster", "ragnarok"))
    # tile A: two dinos
    assert await tel.upsert_region((0, 0, 100, 100), [
        {"dino_uid": "a1", "species": "Rex", "name": "A1", "x": 10, "y": 10},
        {"dino_uid": "a2", "species": "Rex", "name": "A2", "x": 90, "y": 90},
    ]) == 2
    # tile B: one dino — ACCUMULATES (tile A untouched), unlike swap_dino_index
    assert await tel.upsert_region((200, 200, 300, 300), [
        {"dino_uid": "b1", "species": "Argy", "name": "B1", "x": 250, "y": 250},
    ]) == 1
    assert await tel.count_dinos() == 3
    # re-scan tile A with only a1 -> a2 DEPARTED (swept); b1 (other tile) untouched
    assert await tel.upsert_region((0, 0, 100, 100), [
        {"dino_uid": "a1", "species": "Rex", "name": "A1moved", "x": 20, "y": 20},
    ]) == 1
    assert await tel.count_dinos() == 2
    assert sorted(d["name"] for d in await tel.dinos_within(0, 0, 5000)) == ["A1moved", "B1"]
    # b1 moved from tile B into tile A: same uid -> its stale row is cleared (no UNIQUE clash)
    assert await tel.upsert_region((0, 0, 100, 100), [
        {"dino_uid": "a1", "species": "Rex", "name": "A1", "x": 30, "y": 30},
        {"dino_uid": "b1", "species": "Argy", "name": "B1here", "x": 40, "y": 40},
    ]) == 2
    assert await tel.count_dinos() == 2
    # rtree row for b1 followed it into tile A (old tile-B row gone)
    assert sorted(d["name"] for d in await tel.dinos_within(35, 35, 50)) == ["A1", "B1here"]
    await mgr.close()


async def test_find_dinos_tool(tmp_path):
    """The census query tool reads the map-wide dino_index: species filter, level ordering,
    full stat block, and a freshness age."""
    from sheldon_bridge.tools.actions import find_dinos
    mgr = _mgr(tmp_path)
    tel = TelemetryStore(await mgr.telemetry("examplecluster", "ragnarok"))
    await tel.upsert_region((0, 0, 100000, 100000), [
        {"dino_uid": "g1", "species": "Giganotosaurus", "name": "", "level": 295, "gender": "F",
         "tribe": "0", "base_stats": {"hp_m": 80000.0}, "x": 12000, "y": 8000},
        {"dino_uid": "r1", "species": "Rex", "name": "Rexy", "level": 150, "gender": "M",
         "tribe": "0", "base_stats": {"hp_m": 6600.0}, "x": 100, "y": 100},
    ])
    ctx = {"telemetry": tel, "eos_id": "E1"}
    res = await find_dinos(species="Giga", ctx=ctx)
    assert res["success"] and res["count"] == 1
    assert res["dinos"][0]["species"] == "Giganotosaurus" and res["dinos"][0]["level"] == 295
    assert res["dinos"][0]["stats"]["hp_m"] == 80000.0
    assert res["census_age_seconds"] is not None
    alld = await find_dinos(ctx=ctx)  # no filter -> both, highest level first
    assert [d["level"] for d in alld["dinos"]] == [295, 150]
    await mgr.close()


async def test_assembler(tmp_path):
    mgr = _mgr(tmp_path)
    camp = CampaignStore(await mgr.campaign("examplecluster"))
    tel = TelemetryStore(await mgr.telemetry("examplecluster", "ragnarok"))
    await camp.upsert_survivor("e", "Jim")
    await camp.add_lore("e", "Sarcastic implant AI.", tier="sticky")
    await camp.add_lore("e", "Tamed a Rex named Bob", keys=["rex", "bob"], salience=0.9)
    await camp.add_lore("e", "Hates Microraptors.", keys=["microraptor"])
    await tel.upsert_player_state("e", character_name="Jim", pos={"x": 1, "y": 2}, vitals={"health": 0.2}, region="Highlands")

    asm = ContextAssembler(token_budget=4000)
    ctx = await asm.assemble(
        base_system="You are Sheldon.", campaign=camp, telemetry=tel,
        eos_id="e", character_name="Jim", is_admin=False, user_message="hows my rex bob?",
    )
    sp = ctx.system_prompt
    assert "You are Sheldon." in sp and "NOT an admin" in sp
    assert "health 20%" in sp and "Highlands" in sp
    assert "implant AI" in sp and "Rex named Bob" in sp and "Microraptors" not in sp
    assert ctx.meta["telemetry"] == "present" and ctx.meta["lore_entries"] == 2

    ctx2 = await asm.assemble(
        base_system="", campaign=camp, telemetry=None,
        eos_id="e", character_name="Jim", is_admin=True, user_message="hi",
    )
    assert "ARE a server admin" in ctx2.system_prompt and ctx2.meta["telemetry"] == "none"
    await mgr.close()


async def test_assembler_staleness(tmp_path):
    """player_state is framed as a CACHED read (not 'live'), shows its age, and a stale read
    nudges the LLM to call get_vitals — the fix for the stale-150 bug (Sheldon quoting an
    hours-old full-health read as current)."""
    import time

    mgr = _mgr(tmp_path)
    camp = CampaignStore(await mgr.campaign("examplecluster"))
    tel = TelemetryStore(await mgr.telemetry("examplecluster", "ragnarok"))
    await camp.upsert_survivor("e", "Jim")
    await tel.upsert_player_state("e", character_name="Jim", vitals={"health": 1.0}, region="Highlands")
    asm = ContextAssembler(token_budget=4000)

    async def sp():
        ctx = await asm.assemble(
            base_system="", campaign=camp, telemetry=tel,
            eos_id="e", character_name="Jim", is_admin=False, user_message="status?",
        )
        return ctx.system_prompt, ctx.meta

    # fresh read -> framed as cached (never "Live"), trusted (no get_vitals nudge yet)
    fresh, meta = await sp()
    assert "last cached read" in fresh and "Live situational read" not in fresh
    assert "get_vitals" not in fresh
    assert meta["telemetry_age_s"] is not None and meta["telemetry_age_s"] < _STALE_AFTER_S

    # backdate the read 10 min -> stale -> shows age + nudges a fresh get_vitals
    await tel.c.execute("UPDATE player_state SET captured_at=? WHERE eos_id=?", (time.time() - 600, "e"))
    await tel.c.commit()
    stale, meta = await sp()
    assert "get_vitals" in stale and "10m ago" in stale
    assert meta["telemetry_age_s"] >= 600
    await mgr.close()


async def test_assembler_coords_and_state_filter(tmp_path):
    """Coords expose altitude (z) + GPS lat/lon when the map constants are published, and the
    internal census_/gps_ server_state keys are NEVER dumped into the LLM context (they used to
    leak as `Server: census_bounds=[...], census_lastfull=<epoch>`)."""
    mgr = _mgr(tmp_path)
    camp = CampaignStore(await mgr.campaign("examplecluster"))
    tel = TelemetryStore(await mgr.telemetry("examplecluster", "ragnarok"))
    await camp.upsert_survivor("e", "Jim")
    await tel.upsert_player_state("e", character_name="Jim",
                                  pos={"x": 200000, "y": 0, "z": 350}, vitals={"health": 1.0})
    await tel.set_server_state("census_lastcount", "23830")  # internal — must be hidden
    await tel.set_server_state("census_bounds", "[-1,-1,1,1]")
    for k, v in (("gps_lat_origin", -400000), ("gps_lon_origin", -400000),
                 ("gps_lat_scale", 800), ("gps_lon_scale", 800)):  # Island constants drive Coords, stay hidden
        await tel.set_server_state(k, v)

    asm = ContextAssembler(token_budget=4000)
    ctx = await asm.assemble(base_system="", campaign=camp, telemetry=tel,
                             eos_id="e", character_name="Jim", is_admin=False, user_message="where am I?")
    sp = ctx.system_prompt
    assert "alt 350" in sp                          # altitude exposed
    assert "GPS 50.0, 75.0" in sp                    # lat 50 (y=0), lon 75 (x=200000) via Island constants
    assert "census_lastcount" not in sp and "census_bounds" not in sp  # census internals filtered
    assert "gps_lat_origin" not in sp                # GPS constants not leaked
    await mgr.close()


async def test_player_state_oxygen_persists(tmp_path):
    """Oxygen is persisted since telemetry migration 003 (it used to be returned live but had
    no column, so the cached implant block couldn't show it). COALESCE preserves it on a
    partial re-write, like the other vitals."""
    mgr = _mgr(tmp_path)
    tel = TelemetryStore(await mgr.telemetry("examplecluster", "ragnarok"))
    await tel.upsert_player_state("e", character_name="Jim", vitals={"health": 0.8, "oxygen": 0.5})
    row = await tel.get_player_state("e")
    assert row["oxygen"] == 0.5 and row["health"] == 0.8
    # partial re-write (no oxygen) must NOT wipe the stored oxygen
    await tel.upsert_player_state("e", vitals={"health": 0.7})
    row = await tel.get_player_state("e")
    assert row["oxygen"] == 0.5 and row["health"] == 0.7
    await mgr.close()
