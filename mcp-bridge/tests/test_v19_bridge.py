"""V19 bridge-side items (all backward-compatible with the CURRENTLY-COOKED V18 mod; NO cook).

  V19-2  / LSA-04   getter reply req-id correlation (additive; the global getter lock stays)
  V19-6  / SERVER-10 per-player thinking/error frames carry a `target` (no cross-player broadcast)
  BUFFS-07          dino events scoped per-cluster + by NUMERIC team id (no tribe-name collision)
  LSA-02            getter reply dropped on reauth → retry ONCE after a short backoff
"""
import asyncio
import json
import sys
import types
from collections import deque
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sheldon_bridge.server import BridgeServer, _query_wire, _pin_wire, _GETTER_REAUTH_BACKOFF_S  # noqa: E402
from sheldon_bridge.tools.actions import get_recent_dino_events  # noqa: E402


class _FakeWS:
    def __init__(self):
        self.sent = []

    async def send(self, data):
        self.sent.append(data)


def _getter_srv():
    """A bare BridgeServer with just the getter-correlation machinery wired."""
    s = object.__new__(BridgeServer)
    s._getter_lock = asyncio.Lock()
    s._query_waiters = {}
    s._getter_req_counter = 0
    s._server_link_ws = _FakeWS()
    s._connections = {}
    return s


# ===================================================================================
# V19-2 — getter reply req-id correlation (ADDITIVE; the lock is NOT removed)
# ===================================================================================

def test_query_wire_omits_id_by_default_cooked_v18_compatible():
    """No req_id → the wire is byte-for-byte the V18 form the cooked mod parses (Split on
    '"message":"' … '","type"', Contains '\\"type\\":\\"vitals\\"'). The id must NOT appear."""
    w = _query_wire("get_vitals", "EOS_A")
    assert w == '{"message":"EOS_A","type":"get_vitals",\\"type\\":\\"get_vitals\\"}'
    assert '"id"' not in w


def test_query_wire_id_rides_after_the_type_marker_tail():
    """With a req_id the id rides a trailing field AFTER the backslash type marker — the cooked
    mod's body Split('"message":"' … '","type"') is unaffected (it ignores the tail)."""
    w = _query_wire("get_vitals", "EOS_A", req_id=7)
    # body split delimiters intact and BEFORE the id
    assert w.startswith('{"message":"EOS_A","type":"get_vitals"')
    body = w.split('"message":"', 1)[1].split('","type"', 1)[0]
    assert body == "EOS_A"            # body extraction is unchanged by the id tail
    assert w.endswith(',"id":7}')      # id is a trailing field, after the type marker


async def test_v19_2_id_reply_resolves_matching_waiter_out_of_order():
    """(a) An id-bearing reply resolves the waiter with THAT id even when a DIFFERENT-id waiter is
    pending first — i.e. correlation is by id, not arrival order. (Once a cook echoes ids this is
    what lets the global getter lock be dropped; the lock itself is untouched here.)"""
    s = _getter_srv()
    loop = asyncio.get_event_loop()
    w1 = s._register_getter_waiter("vitals", req_id=1)   # registered first
    w2 = s._register_getter_waiter("vitals", req_id=2)   # registered second
    # reply for id=2 arrives FIRST — must resolve w2 (not the older w1)
    s._deliver_typed_reply("vitals", {"type": "vitals", "id": 2, "health": 22})
    assert w2.fut.done() and w2.fut.result()["health"] == 22
    assert not w1.fut.done(), "the non-matching (older) waiter must stay pending"
    # then id=1 resolves w1
    s._deliver_typed_reply("vitals", {"type": "vitals", "id": 1, "health": 11})
    assert w1.fut.done() and w1.fut.result()["health"] == 11
    assert "vitals" not in s._query_waiters or not s._query_waiters["vitals"]


async def test_v19_2_noid_reply_resolves_via_fifo_backcompat():
    """(b) A NO-id reply (the CURRENTLY-COOKED V18 mod) still resolves via the oldest-waiter FIFO,
    exactly as today — the back-compat path."""
    s = _getter_srv()
    w1 = s._register_getter_waiter("position", req_id=1)
    w2 = s._register_getter_waiter("position", req_id=2)
    s._deliver_typed_reply("position", {"type": "position", "loc": "X=1"})   # no id
    assert w1.fut.done() and w1.fut.result()["loc"] == "X=1"   # oldest resolved (FIFO)
    assert not w2.fut.done()
    s._deliver_typed_reply("position", {"type": "position", "loc": "X=2"})   # no id
    assert w2.fut.done() and w2.fut.result()["loc"] == "X=2"


async def test_v19_2_id_reply_for_timed_out_waiter_is_dropped():
    """An id-matched reply for a getter that already TIMED OUT (tombstone) is consumed + dropped,
    never cross-delivered."""
    s = _getter_srv()
    w_live = s._register_getter_waiter("tribe", req_id=10)
    w_dead = s._register_getter_waiter("tribe", req_id=11)
    w_dead.abandoned = True                                   # timed out
    s._deliver_typed_reply("tribe", {"type": "tribe", "id": 11, "name": "Late"})
    assert not w_live.fut.done(), "the live unrelated waiter must NOT receive a tombstoned reply"


async def test_v19_2_full_roundtrip_still_lock_serialized():
    """End-to-end through _game_command_handler: a no-id reply round-trips, AND the getter lock is
    still held across send→await (NOT removed by V19-2 — the lock is the correctness invariant)."""
    s = _getter_srv()

    async def fake_send(wire):
        async def _later():
            await asyncio.sleep(0.01)
            s._deliver_typed_reply("vitals", {"type": "vitals", "health": 99})  # cooked: no id
        asyncio.ensure_future(_later())

    s._server_link_ws = types.SimpleNamespace(send=fake_send)
    r = await s._game_command_handler({"action": "query", "request": "get_vitals",
                                       "reply": "vitals", "eos": "EOS_A"})
    assert r["success"] and r["data"]["health"] == 99
    assert not s._getter_lock.locked(), "the lock must be released after the round-trip"


# ===================================================================================
# V19-6 / SERVER-10 — per-player thinking/error frames carry a `target`
# ===================================================================================

async def test_v19_6_thinking_frame_targets_known_requester():
    """The thinking indicator for an identified requester carries target=<eos> so the cooked B4
    filter renders it ONLY on the asker's client (untargeted = broadcast to everyone)."""
    from sheldon_bridge.auth import PlayerContext
    from sheldon_bridge.session import SessionManager
    from sheldon_bridge.db.manager import DBManager, Scope
    import dataclasses, tempfile

    s = object.__new__(BridgeServer)
    ws = _FakeWS()
    s._server_link_ws = ws
    s.sessions = SessionManager()
    s.rate_limiter = types.SimpleNamespace(check=lambda *a, **k: (True, ""))
    s._team_tribe = {}
    s._recent_game_events = deque(maxlen=20)
    s._recent_dino_game_events = deque(maxlen=20)
    s._turn_semaphore = asyncio.Semaphore(8)
    s._getter_lock = asyncio.Lock()
    s._query_waiters = {}
    s.config = types.SimpleNamespace(build_base_system=lambda: "")  # reached before assemble()

    tmp = tempfile.mkdtemp()
    s.db = DBManager(tmp, default_cluster_id="c", default_server_id="srv")
    link_player = PlayerContext(player_id="server-link", display_name="Survivor", tier="player")
    session = s.sessions.create(link_player)
    session.scope = Scope(cluster_id="c", server_id="srv")

    # stop the turn right after the thinking frame by making assemble blow up (caught nowhere
    # inside this test — we only care that the thinking frame was sent with a target first).
    async def boom(*a, **k):
        raise RuntimeError("stop after thinking")
    s.assembler = types.SimpleNamespace(assemble=boom)

    msg = {"type": "player_message", "message": "EOS_A|||false|||Bob|||how am I doing?"}
    try:
        await s._handle_player_message(msg, session, ws)
    except RuntimeError:
        pass
    await s.db.close()

    thinking = [json.loads(m) for m in ws.sent if '"thinking"' in m]
    assert thinking, "a thinking frame must have been sent"
    assert thinking[0].get("target") == "EOS_A", "thinking must target the resolved requester"


async def test_v19_6_admission_error_targets_requester_from_prefix():
    """The admission-cap shed notice (sent BEFORE the turn parse) targets the asker parsed from the
    |||-prefix on the server link; an unidentified frame falls back to empty target (broadcast)."""
    from sheldon_bridge.server import _MAX_INFLIGHT_TURNS
    s = object.__new__(BridgeServer)
    ws = _FakeWS()
    s._server_link_ws = ws
    s._turn_tasks = set(range(_MAX_INFLIGHT_TURNS))   # at capacity → shed
    await s._handle_message(
        {"type": "player_message", "message": "EOS_Z|||false|||Bob|||hi"}, None, ws)
    shed = [json.loads(m) for m in ws.sent if '"error"' in m]
    assert shed and shed[0]["target"] == "EOS_Z"

    # unidentified frame (no prefix) → empty target (accepted before-identity broadcast)
    ws2 = _FakeWS()
    s._server_link_ws = ws2
    await s._handle_message({"type": "player_message", "message": "no prefix here"}, None, ws2)
    shed2 = [json.loads(m) for m in ws2.sent if '"error"' in m]
    assert shed2 and shed2[0]["target"] == ""


def test_v19_6_requester_eos_only_honored_on_server_link():
    """SEC-01/ADD-03: a |||-prefix on a NON-link socket is ignored → empty target (no impersonation
    via a targeted system frame)."""
    s = object.__new__(BridgeServer)
    s._server_link_ws = _FakeWS()
    other = _FakeWS()
    msg = {"type": "player_message", "message": "EOS_SPOOF|||true|||x|||hi"}
    assert s._requester_eos_of(msg, other) == ""                     # not the link → ignored
    assert s._requester_eos_of(msg, s._server_link_ws) == "EOS_SPOOF"  # the link → honored


# ===================================================================================
# BUFFS-07 — dino events scoped per-cluster + by NUMERIC team id
# ===================================================================================

def _ev(kind, team, tribe=None, dino="Rex"):
    d = {"dino": dino, "team": team}
    if tribe is not None:
        d["tribe"] = tribe
    return {"event": kind, "at": 1, "summary": "x", "data": d}


async def test_buffs07_cross_tribe_blocked_by_numeric_team():
    """When the getter reports the asker's NUMERIC team, dino events are matched by exact team id —
    tribe B's events never reach tribe A even if a tame named tribe B's tribe."""
    async def gh(req):
        # the (future-cook) tribe getter reports the asker's team id 100 + name "Alpha"
        return {"success": True, "data": {"name": "Alpha", "team": "100"}}
    ctx = {
        "game_handler": gh, "eos_id": "p1",
        "team_tribe": {"100": "Alpha", "200": "Bravo"},
        "dino_events": [
            _ev("dino_tamed", "100", "Alpha", "MyRex"),    # mine
            _ev("dino_death", "100", dino="MyRaptor"),     # mine (team only)
            _ev("dino_death", "200", dino="EnemyDodo"),    # tribe B → must be excluded
        ],
    }
    r = await get_recent_dino_events(ctx=ctx)
    dinos = {e["dino"] for e in r["events"]}
    assert dinos == {"MyRex", "MyRaptor"}
    assert "EnemyDodo" not in dinos


async def test_buffs07_same_tribe_name_different_clusters_does_not_collide():
    """Two clusters both have a tribe literally named "Raiders". The server injects ONLY the asker's
    own-cluster team->tribe map, so the other cluster's identically-named tribe can't leak in via the
    death->team->tribe fallback (no-team getter path)."""
    async def gh(req):  # cooked V18 getter: name only, NO team id → fallback path
        return {"success": True, "data": {"name": "Raiders"}}
    # asker is on cluster-A; the injected map is cluster-A's (team 5 = Raiders here).
    ctx_a = {
        "game_handler": gh, "eos_id": "p1",
        "team_tribe": {"5": "Raiders"},        # cluster A's map ONLY
        "dino_events": [
            _ev("dino_death", "5", dino="A_Rex"),     # cluster A team 5 → Raiders (mine)
            _ev("dino_death", "9", dino="B_Rex"),     # cluster B's team 9 (not in A's map) → excluded
        ],
    }
    r = await get_recent_dino_events(ctx=ctx_a)
    dinos = {e["dino"] for e in r["events"]}
    assert dinos == {"A_Rex"}, "only the asker's own-cluster team resolves to the tribe name"
    assert "B_Rex" not in dinos


def test_buffs07_team_tribe_keyed_per_cluster_in_handle_game_event():
    """_handle_game_event stores the learned team->tribe under the originating cluster, so the same
    team id in two clusters maps to each cluster's own tribe (no last-writer-wins clobber)."""
    s = object.__new__(BridgeServer)
    s._recent_game_events = deque(maxlen=20)
    s._recent_dino_game_events = deque(maxlen=20)
    s._team_tribe = {}
    s._damage_ingest_times = deque(maxlen=64)
    s._damage_dropped = 0
    s._handle_game_event("dino_tamed", {"type": "dino_tamed", "dino": "Rex", "team": "5",
                                        "tribe": "Alpha"}, cluster_id="clusterA")
    s._handle_game_event("dino_tamed", {"type": "dino_tamed", "dino": "Rex", "team": "5",
                                        "tribe": "Bravo"}, cluster_id="clusterB")
    assert s._team_tribe["clusterA"]["5"] == "Alpha"
    assert s._team_tribe["clusterB"]["5"] == "Bravo"   # NOT clobbered by the same team id elsewhere


# ===================================================================================
# LSA-02 — getter reply dropped on reauth → retry ONCE after a short backoff
# ===================================================================================

def _cap_getter_await(monkeypatch):
    """Cap the 8s getter reply-await to 0.05s so timeout tests are fast (leaves the smaller send
    timeout untouched — min() picks it). Also no-op the reauth backoff sleep."""
    real_wait_for = asyncio.wait_for
    real_sleep = asyncio.sleep  # capture BEFORE patching — else the lambda calls the PATCHED sleep
                                # (self-reference → runaway recursion/alloc → OOM). Bug fixed 2026-06-24.

    async def capped_wait_for(aw, timeout):
        return await real_wait_for(aw, min(timeout, 0.05))

    monkeypatch.setattr("sheldon_bridge.server.asyncio.wait_for", capped_wait_for)
    monkeypatch.setattr("sheldon_bridge.server.asyncio.sleep", lambda *_a, **_k: real_sleep(0))


async def test_lsa02_getter_retries_once_then_succeeds(monkeypatch):
    """A getter that TIMES OUT once (the reconnect→reauth drop) then succeeds on the retry returns
    the value — the bridge no longer surfaces the transient error immediately."""
    s = _getter_srv()
    _cap_getter_await(monkeypatch)
    attempts = {"n": 0}

    async def fake_send(wire):
        attempts["n"] += 1
        if attempts["n"] == 1:
            return  # 1st attempt: reply silently dropped (reauth) → the await times out
        async def _later():
            await asyncio.sleep(0)
            s._deliver_typed_reply("vitals", {"type": "vitals", "health": 88})
        asyncio.ensure_future(_later())

    s._server_link_ws = types.SimpleNamespace(send=fake_send)
    r = await s._game_command_handler({"action": "query", "request": "get_vitals",
                                       "reply": "vitals", "eos": "EOS_A"})
    assert attempts["n"] == 2, "the getter must have retried exactly once"
    assert r["success"] and r["data"]["health"] == 88
    assert "retryable" not in r, "the internal retry flag must be stripped from the result"


async def test_lsa02_no_retry_on_success(monkeypatch):
    """A getter that succeeds on the FIRST attempt is not retried (the retry is timeout-only)."""
    s = _getter_srv()
    attempts = {"n": 0}

    async def fake_send(wire):
        attempts["n"] += 1
        async def _later():
            await asyncio.sleep(0.01)
            s._deliver_typed_reply("position", {"type": "position", "loc": "X=5"})
        asyncio.ensure_future(_later())

    s._server_link_ws = types.SimpleNamespace(send=fake_send)
    r = await s._game_command_handler({"action": "query", "request": "get_position",
                                       "reply": "position", "eos": "EOS_A"})
    assert attempts["n"] == 1 and r["success"] and r["data"]["loc"] == "X=5"


async def test_lsa02_retry_is_bounded_to_one(monkeypatch):
    """A getter that times out on BOTH attempts fails (the retry can never loop — at most one extra
    attempt)."""
    s = _getter_srv()
    _cap_getter_await(monkeypatch)
    attempts = {"n": 0}

    async def fake_send(wire):
        attempts["n"] += 1   # never deliver → both attempts time out

    s._server_link_ws = types.SimpleNamespace(send=fake_send)
    r = await s._game_command_handler({"action": "query", "request": "get_vitals",
                                       "reply": "vitals", "eos": "EOS_A"})
    assert attempts["n"] == 2, "exactly two attempts (initial + one retry), never more"
    assert not r["success"]


# ===================================================================================
# V19-5 (MAPPIN-01) bridge half — per-player render target on the pin wire
# ===================================================================================

def test_v19_5_pin_wire_target_is_trailing_and_cooked_safe():
    """The render target rides a trailing `,"target":"<eos>"` AFTER the type marker — the cooked V18
    mod's body Split('"message":"'…'","type"') is byte-identical, so the pin payload is unchanged
    (the cooked parser ignores the target); the V19 cook reads it via Split('"target":"'…'"').
    The field is ALWAYS emitted (even empty) so the server-side Split is deterministic — empty target
    → Contains(eos,"")==true in the cooked mod (C2-05) → broadcast (matches V18 all-clients render)."""
    w = _pin_wire("add", "sheldon_p1", "Base camp", 100.4, -50.6, 7.0, target_eos="EOS_A")
    body = w.split('"message":"', 1)[1].split('","type"', 1)[0]
    assert body == "add|sheldon_p1|Base camp|100|-51|7"   # payload identical regardless of target
    assert w.endswith(',"target":"EOS_A"}')                # target is a trailing field, outside the body
    # empty target -> the field is STILL present (deterministic server Split), value empty -> broadcast
    w0 = _pin_wire("remove", "sheldon_p1")
    body0 = w0.split('"message":"', 1)[1].split('","type"', 1)[0]
    assert body0 == "remove|sheldon_p1||0|0|0"             # body still byte-identical -> cooked V18 ignores target
    assert w0.endswith(',"target":""}')                    # empty field present, not omitted
    assert w0.count('"target"') == 1                       # exactly one target field, trailing


def test_recover_player_message_unescaped_quotes():
    """A real chat message whose text has an unescaped " breaks json.loads; the recv loop must
    RECOVER it (re-escape just the body + re-parse) rather than drop it as [recv-badjson]."""
    from sheldon_bridge.server import _recover_player_message
    raw = '{"type":"player_message","message":"eos|||false|||N|||call it "Main Base"?","position":{"x":1,"y":2,"z":3},"facing_yaw":0.0}'
    d = _recover_player_message(raw)
    assert d and d["type"] == "player_message"
    assert d["message"].endswith('"Main Base"?')          # body preserved, quotes intact
    assert d["position"] == {"x": 1, "y": 2, "z": 3}      # position recovered too
    assert _recover_player_message('{"type":"ping"') is None   # non-player frame -> no mis-handle


def test_reply_wire_quotes_render_clean_no_backslashes():
    """OUTBOUND reply display: the cooked mod raw-Splits the body out and has NO JSON unescape, so a
    backslash-escaped \\" would DISPLAY literally (the 2026-06-24 \\"Main Base\\" bug). _reply_wire must
    map " -> ' so the body renders clean AND can never form the `","type"` split delimiter early."""
    from sheldon_bridge.server import _reply_wire
    w = _reply_wire('Sheldon: Done! I dropped a "Main Base" pin.')
    body = w.split('"message":"', 1)[1].split('","type"', 1)[0]   # exactly what the cooked mod extracts
    assert body == "Sheldon: Done! I dropped a 'Main Base' pin."  # " -> ' , no backslashes shown
    assert '"' not in body and '\\' not in body                   # delimiter-safe + nothing to mis-display
    assert '\\"type\\":\\"reply\\"' in w                          # routing marker (real backslashes) intact
    # worst case: inline JSON in a reply can no longer truncate the extracted body at a fake delimiter
    body2 = (_reply_wire('cfg {"name":"x","type":"y"}')
             .split('"message":"', 1)[1].split('","type"', 1)[0])
    assert body2 == "cfg {'name':'x','type':'y'}"                 # whole reply survives extraction
