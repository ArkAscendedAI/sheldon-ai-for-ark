"""V19 bridge-side audit-finding fixes (no cook, no feature regressions).

  MAPPIN-02/CDP-12  per-requester pin ownership (no cross-player clobber)
  MAPPIN-06         clear_pins scoped + pops-as-sent
  BUFFS-09          dino events use a separate ring (not evicted by death/damage)
  C2-05             empty-EOS player getter refused (would resolve the wrong player mod-side)
  BUFFS-10          parse_actor_ref never yields an empty attacker name for a non-null actor
  LSA-06            the auth-ok frame is a frozen wire contract ('auth_success')
"""
import asyncio
import sys
from collections import deque
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from sheldon_bridge.server import BridgeServer, _wire  # noqa: E402
from sheldon_bridge import names  # noqa: E402


class _FakeWS:
    def __init__(self):
        self.sent = []

    async def send(self, data):
        self.sent.append(data)


def _pinsrv():
    s = object.__new__(BridgeServer)
    s._pin_lock = asyncio.Lock()
    s._map_pins = {}
    s._pin_counter = 0
    s._server_link_ws = _FakeWS()
    return s


# --- MAPPIN-02 / CDP-12 / MAPPIN-06 : per-requester pin ownership ---------------------------------

async def test_pins_are_per_requester_no_cross_clobber():
    s = _pinsrv()
    a = await s._handle_map_pin(s._server_link_ws, {"op": "add", "label": "A base", "x": 1, "y": 2, "z": 3, "eos": "EOS_A"})
    assert a["success"]
    pid = a["pin_id"]
    # player B CANNOT remove player A's pin
    rb = await s._handle_map_pin(s._server_link_ws, {"op": "remove", "id": pid, "eos": "EOS_B"})
    assert not rb["success"] and "yours" in rb["error"]
    # player B's clear does NOT touch player A's pins
    cb = await s._handle_map_pin(s._server_link_ws, {"op": "clear", "eos": "EOS_B"})
    assert cb["success"] and cb["cleared"] == 0
    assert pid in s._map_pins.get("EOS_A", {}), "A's pin must survive B's clear"
    # player A CAN remove their own pin
    ra = await s._handle_map_pin(s._server_link_ws, {"op": "remove", "id": pid, "eos": "EOS_A"})
    assert ra["success"] and ra["removed"] == pid


async def test_clear_pins_scoped_to_owner_and_pops_as_sent():
    s = _pinsrv()
    await s._handle_map_pin(s._server_link_ws, {"op": "add", "label": "p1", "x": 0, "y": 0, "z": 0, "eos": "EOS_A"})
    await s._handle_map_pin(s._server_link_ws, {"op": "add", "label": "p2", "x": 0, "y": 0, "z": 0, "eos": "EOS_A"})
    c = await s._handle_map_pin(s._server_link_ws, {"op": "clear", "eos": "EOS_A"})
    assert c["success"] and c["cleared"] == 2
    assert s._map_pins.get("EOS_A", {}) == {}   # all of A's pins cleared, none left in the registry


# --- BUFFS-09 : dino events get their own ring ----------------------------------------------------

def test_dino_events_use_separate_ring():
    s = object.__new__(BridgeServer)
    s._recent_game_events = deque(maxlen=20)
    s._recent_dino_game_events = deque(maxlen=20)
    s._team_tribe = {}
    s._damage_ingest_times = deque(maxlen=64)
    s._damage_dropped = 0
    s._handle_game_event("dino_tamed", {"type": "dino_tamed", "dino": "Rex", "team": "5", "tribe": "Wolfpack"})
    s._handle_game_event("dino_death", {"type": "dino_death", "dino": "Raptor", "team": "5"})
    assert len(s._recent_dino_game_events) == 2
    assert len(s._recent_game_events) == 0, "dino events must NOT land in the death/damage ring"
    assert len(s._recent_dino_events()) == 2


# --- C2-05 : empty-EOS player getter is refused ---------------------------------------------------

async def test_empty_eos_player_getter_refused():
    s = object.__new__(BridgeServer)
    s._getter_lock = asyncio.Lock()
    s._query_waiters = {}
    s._server_link_ws = _FakeWS()
    r = await s._game_command_handler({"action": "query", "request": "get_vitals", "reply": "vitals", "eos": ""})
    assert not r["success"] and "requester id" in r["error"]
    assert s._server_link_ws.sent == [], "no getter should have been sent to the mod"


# --- BUFFS-10 : robust actor-name fallback --------------------------------------------------------

def test_parse_actor_ref_name_never_empty_for_real_actor():
    assert names.parse_actor_ref("Rex_Character_BP_C_2147")["name"]          # normal
    assert names.parse_actor_ref("SomeWeirdModActor_42")["name"]             # unexpected format → still a name
    assert names.parse_actor_ref("StructureTurret_C")["name"]               # structure killer → still a name
    assert names.parse_actor_ref("None") == {}                               # null-ish still correctly drops


# --- LSA-06 : auth-ok wire contract ---------------------------------------------------------------

def test_auth_success_is_a_frozen_wire_contract():
    # the cooked mod sets IsAuthenticated on Contains(inbound, 'auth_success') — freeze the literal.
    frame = _wire({"type": "auth_success", "player_id": "x", "tier": "player", "tools_available": 3})
    assert "auth_success" in frame
