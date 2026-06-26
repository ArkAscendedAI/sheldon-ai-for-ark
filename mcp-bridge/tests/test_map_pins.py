"""Map-pin actuator (V15). The wire frame is a two-sided contract with the not-yet-built
mod routing graft, so its shape is pinned here; and the bridge owns the pin registry so the
mod can stay a dumb add/remove-by-tag primitive (clear is driven bridge-side per pin)."""
import asyncio

from sheldon_bridge.server import BridgeServer, _pin_wire

PIN_MARKER = '\\"type\\":\\"map_pin\\"'  # the backslash marker the cooked mod Contains()-matches


def _payload(frame: str) -> str:
    """Pull the pipe-delimited body the mod extracts via Split('"message":"' … '","type"')."""
    return frame.split('"message":"', 1)[1].split('","type"', 1)[0]


# --- wire format ----------------------------------------------------------------

def test_pin_wire_shape_and_marker():
    f = _pin_wire("add", "sheldon_p1", "Home", 100.4, 200.6, -300.2)
    assert PIN_MARKER in f
    assert _payload(f) == "add|sheldon_p1|Home|100|201|-300"  # coords rounded to int cm


def test_pin_wire_sanitizes_label_so_it_cannot_break_split():
    fields = _payload(_pin_wire("add", "sheldon_p1", 'a|b"c', 0, 0, 0)).split("|")
    assert len(fields) == 6           # op,id,label,x,y,z — the label's '|' was neutralized
    assert fields[2] == "a/bc"        # '|' → '/', '"' stripped


# --- handler: registry + send (no live editor / game needed) --------------------

class _FakeWS:
    def __init__(self):
        self.sent = []

    async def send(self, data):
        self.sent.append(data)


def _srv(connected=True):
    s = object.__new__(BridgeServer)
    s._connections = {"server-link": _FakeWS()} if connected else {}
    # routing now uses the explicit link, not the _connections key (SCC-01)
    s._server_link_ws = s._connections.get("server-link")
    s._map_pins = {}
    s._pin_counter = 0
    return s


def _call(s, **cmd):
    # _handle_map_pin serializes on self._pin_lock (M4). The partial _srv() bypasses __init__,
    # and each _call spins its own asyncio.run() loop while an asyncio.Lock binds to a single
    # loop — so give each call a fresh lock (pin op state lives in _map_pins/_pin_counter, not
    # the lock).
    s._pin_lock = asyncio.Lock()
    cmd.setdefault("eos", "EOS_T")   # pins are now per-requester (MAPPIN-02) — use one test owner
    return asyncio.run(s._game_command_handler({"action": "map_pin", **cmd}))


def test_add_pin_assigns_id_tracks_and_sends_one_frame():
    s = _srv()
    r = _call(s, op="add", label="Home", x=1, y=2, z=3)
    assert r["success"] and r["pin_id"] == "sheldon_p1"
    assert s._map_pins["EOS_T"]["sheldon_p1"]["label"] == "Home"   # per-requester registry
    sent = s._connections["server-link"].sent
    assert len(sent) == 1 and PIN_MARKER in sent[0] and "Home" in sent[0]


def test_remove_unknown_pin_errors_and_sends_nothing():
    s = _srv()
    r = _call(s, op="remove", id="sheldon_p9")
    assert not r["success"]
    assert s._connections["server-link"].sent == []


def test_remove_drops_tracked_pin():
    s = _srv()
    _call(s, op="add", label="X", x=0, y=0, z=0)
    r = _call(s, op="remove", id="sheldon_p1")
    assert r["success"] and r["removed"] == "sheldon_p1"
    assert "sheldon_p1" not in s._map_pins.get("EOS_T", {})


def test_clear_removes_every_tracked_pin_one_frame_each():
    s = _srv()
    for i in range(3):
        _call(s, op="add", label=f"P{i}", x=0, y=0, z=0)
    s._connections["server-link"].sent.clear()
    r = _call(s, op="clear")
    assert r["success"] and r["cleared"] == 3
    assert s._map_pins.get("EOS_T", {}) == {}   # owner's pins all cleared from the registry
    assert len(s._connections["server-link"].sent) == 3  # mod stays dumb: one remove per pin


def test_no_server_connected_is_a_clean_error():
    s = _srv(connected=False)
    r = _call(s, op="add", label="X", x=0, y=0, z=0)
    assert not r["success"]


def test_unknown_op_is_rejected():
    s = _srv()
    r = _call(s, op="frobnicate")
    assert not r["success"]

# NOTE: player-tier registration is verified against the REAL production registry (not the
# synthetic `registry` fixture, which clears the global tool list). See the runtime check in
# the deploy step / `python -c "...ToolRegistry().get_tools_for_tier('player')..."`.
