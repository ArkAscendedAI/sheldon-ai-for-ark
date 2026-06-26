"""Telemetry persistence side-effect: when a getter returns LIVE data, it should upsert the
requesting survivor's player_state (so the assembler's 'Live situational read' populates).
Mock data or a missing store must NOT write, and must never break the getter."""
import asyncio

from sheldon_bridge.tools import actions


class FakeTel:
    def __init__(self):
        self.calls = []

    async def upsert_player_state(self, eos, *, character_name="", vitals=None, pos=None,
                                  level=None, region=None):
        self.calls.append({"eos": eos, "name": character_name, "vitals": vitals,
                           "pos": pos, "level": level})


async def _gh(reply, data):
    async def h(cmd):
        return {"success": True, "data": data}
    return h


def ctx(gh, tel):
    return {"game_handler": gh, "telemetry": tel, "eos_id": "EOS1", "character_name": "PlayerOne"}


def test_vitals_persists_live():
    tel = FakeTel()
    gh = asyncio.run(_gh("vitals", {"health": 87.5}))
    r = asyncio.run(actions.get_vitals(ctx=ctx(gh, tel)))
    assert r["health"] == 87.5
    assert len(tel.calls) == 1 and tel.calls[0]["vitals"] == {"health": 87.5}
    assert tel.calls[0]["eos"] == "EOS1" and tel.calls[0]["name"] == "PlayerOne"


def test_vitals_surfaces_max_and_persists_oxygen():
    """V17 getter sends `<stat>_max` beside current — surfaced to the LLM so it can say
    '57.9/180, badly hurt'. Oxygen now persists (migration 003); max is static per level → live only."""
    tel = FakeTel()
    data = {"health": 57.9, "health_max": 180.0, "oxygen": 93.8, "oxygen_max": 150.0,
            "stamina": 100.0, "stamina_max": 100.0}
    gh = asyncio.run(_gh("vitals", data))
    r = asyncio.run(actions.get_vitals(ctx=ctx(gh, tel)))
    assert r["health"] == 57.9 and r["health_max"] == 180.0
    assert r["oxygen"] == 93.8 and r["oxygen_max"] == 150.0
    # oxygen persists (it's in _DB_VITALS now); the *_max values are NOT persisted (static)
    assert tel.calls[0]["vitals"]["oxygen"] == 93.8
    assert not any(k.endswith("_max") for k in tel.calls[0]["vitals"])


def test_vitals_pre_v17_unchanged_no_max():
    """A pre-V17 mod sends current-only — no `_max` keys appear and nothing breaks (backward-compat)."""
    tel = FakeTel()
    gh = asyncio.run(_gh("vitals", {"health": 87.5}))
    r = asyncio.run(actions.get_vitals(ctx=ctx(gh, tel)))
    assert r["health"] == 87.5
    assert not any(k.endswith("_max") for k in r)
    assert tel.calls[0]["vitals"] == {"health": 87.5}  # unchanged persist shape


def test_position_persists_parsed():
    tel = FakeTel()
    gh = asyncio.run(_gh("position", {"loc": "X=120.5 Y=-90.0 Z=12.0"}))
    asyncio.run(actions.get_position(ctx=ctx(gh, tel)))
    assert tel.calls[0]["pos"] == {"x": 120.5, "y": -90.0, "z": 12.0}


def test_progression_persists_int():
    tel = FakeTel()
    gh = asyncio.run(_gh("progression", {"level": "104"}))
    asyncio.run(actions.get_progression(ctx=ctx(gh, tel)))
    assert tel.calls[0]["level"] == 104


def test_mock_path_does_not_persist():
    tel = FakeTel()  # no game_handler -> mock branch -> must not write live telemetry
    asyncio.run(actions.get_vitals(ctx={"telemetry": tel, "eos_id": "EOS1"}))
    assert tel.calls == []


def test_getter_works_without_telemetry_store():
    gh = asyncio.run(_gh("vitals", {"health": 50.0}))
    r = asyncio.run(actions.get_vitals(ctx={"game_handler": gh}))
    assert r["health"] == 50.0  # no telemetry in ctx -> still returns fine


def test_parse_loc():
    assert actions._parse_loc("X=1.0 Y=2 Z=-3.5") == {"x": 1.0, "y": 2.0, "z": -3.5}
    assert actions._parse_loc("") is None
    assert actions._parse_loc(None) is None
