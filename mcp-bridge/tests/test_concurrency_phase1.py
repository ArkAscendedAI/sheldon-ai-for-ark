"""V18 Phase-1 multi-user concurrency fixes (no-cook bridge).

Covers the audited races: C1 getter cross-delivery (the getter-serialization lock), H1 shared
shared-connection write interleave (the per-connection write-lock), H2 duplicate-uid abort
(batch dedupe), H3 torn event read (deep-copied snapshot), M2 session leak (LRU cap), and M3
unbounded turn fan-out (admission cap). Async tests run under pytest-asyncio (asyncio_mode=auto).
"""

import asyncio
import sys
import types
from collections import deque
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))  # put mcp-bridge/ on the path

from sheldon_bridge.auth import PlayerContext
from sheldon_bridge.db.manager import DBManager
from sheldon_bridge.db.telemetry_store import TelemetryStore, _dedupe_by_uid
from sheldon_bridge.server import BridgeServer, _MAX_INFLIGHT_TURNS
from sheldon_bridge.session import SessionManager


def _mgr(tmp_path):
    return DBManager(str(tmp_path), default_cluster_id="c", default_server_id="s")


# --- H2: duplicate dino_uid in one batch must not abort the write -----------------

def test_h2_dedupe_helper_keeps_last():
    out = _dedupe_by_uid([
        {"dino_uid": "x", "name": "first"},
        {"dino_uid": "y", "name": "other"},
        {"dino_uid": "x", "name": "second"},   # newer x wins, stays at x's earlier slot
        {"dino_uid": None, "name": "nullA"},    # null uids never collide — both kept
        {"dino_uid": "", "name": "nullB"},
    ])
    assert [d["name"] for d in out] == ["second", "other", "nullA", "nullB"]


async def test_h2_upsert_region_dedupes_duplicate_uid(tmp_path):
    mgr = _mgr(tmp_path)
    tel = TelemetryStore(await mgr.telemetry("c", "s"))
    # same uid twice in one scan reply (octree boundary overlap) — must NOT raise / abort
    n = await tel.upsert_region((0, 0, 100, 100), [
        {"dino_uid": "dup", "species": "Rex", "name": "first", "x": 10, "y": 10},
        {"dino_uid": "dup", "species": "Rex", "name": "second", "x": 20, "y": 20},
        {"dino_uid": "u2", "species": "Argy", "name": "other", "x": 30, "y": 30},
    ])
    assert n == 2 and await tel.count_dinos() == 2
    assert sorted(d["name"] for d in await tel.dinos_within(0, 0, 1000)) == ["other", "second"]
    await mgr.close()


async def test_h2_swap_dino_index_dedupes_duplicate_uid(tmp_path):
    mgr = _mgr(tmp_path)
    tel = TelemetryStore(await mgr.telemetry("c", "s"))
    n = await tel.swap_dino_index([
        {"dino_uid": "d", "species": "Rex", "name": "a", "x": 1, "y": 1},
        {"dino_uid": "d", "species": "Rex", "name": "b", "x": 2, "y": 2},  # dup → would clash
    ])
    assert n == 1 and await tel.count_dinos() == 1
    await mgr.close()


# --- H1: concurrent writes on the SHARED connection stay consistent ---------------

async def test_h1_concurrent_writes_stay_consistent(tmp_path):
    """40 census region writes + 40 player-state writes hammering the same connection at once.
    The per-connection write-lock serializes each multi-statement unit, so no commit splices a
    half-written region and the dino_index stays in sync with its R-tree (no torn rows)."""
    mgr = _mgr(tmp_path)
    tel = TelemetryStore(await mgr.telemetry("c", "s"))

    async def region(i):
        await tel.upsert_region(
            (i * 1000, 0, i * 1000 + 100, 100),
            [{"dino_uid": f"d{i}", "species": "Rex", "name": f"n{i}", "x": i * 1000 + 10, "y": 10}],
        )

    async def player(i):
        await tel.upsert_player_state(f"p{i}", vitals={"health": 0.5})

    await asyncio.gather(*(region(i) for i in range(40)), *(player(i) for i in range(40)))

    assert await tel.count_dinos() == 40                       # every region landed, none lost
    within = await tel.dinos_within(0, 0, 10_000_000, limit=100)
    assert len(within) == 40                                   # rtree row for every index row (no desync)
    await mgr.close()


# --- H3: the recent-events snapshot is isolated from in-place ring mutation -------

def test_h3_recent_events_snapshot_is_deepcopied():
    s = object.__new__(BridgeServer)
    s._recent_game_events = deque(
        [{"event": "damage_event", "data": {"amount": 10}, "summary": "ow", "at": 1.0}], maxlen=20
    )
    snap = s._recent_events_for("EOS_X")
    # mutate the live ring entry in place (exactly what _coalesce_into_recent_damage does)
    s._recent_game_events[-1]["data"]["amount"] = 999
    s._recent_game_events[-1]["summary"] = "MUTATED"
    assert snap[0]["data"]["amount"] == 10 and snap[0]["summary"] == "ow"


# --- C1: the getter lock stops type-only-FIFO cross-delivery ----------------------

def _deliver(s, reply_type, payload):
    """Route a reply via the server's REAL tombstone-aware router (XDEL-01 fix)."""
    s._deliver_typed_reply(reply_type, payload)


async def test_c1_getter_correlation_by_id():
    """V20-1: the getter lock is DROPPED — concurrent getters run in PARALLEL and are routed by the
    echoed request id, NOT by type-only FIFO. Two concurrent get_vitals where the FIRST sender's reply
    arrives LATER than the second's (out-of-order — the condition a type-only FIFO router cross-delivers
    under). The cooked V20 mod echoes the id, so each call still receives ITS OWN reply via id-correlation.
    (Verified live 2026-06-25: [reply-corr] vitals id=6/7, position id=13/18.)"""
    s = object.__new__(BridgeServer)
    s._getter_lock = asyncio.Lock()   # still defined; no longer wrapped around player getters (V20-1)
    s._query_waiters = {}
    sent = []

    async def fake_send(wire):
        idx = len(sent)
        sent.append(wire)
        # The cooked V20 mod echoes the request id the bridge stamped into the wire (trailing ,"id":<n>}).
        rid = int(wire.split('"id":', 1)[1].split('}', 1)[0]) if '"id":' in wire else None
        delay = 0.05 if idx == 0 else 0.01   # first send's reply is the SLOWER one (out-of-order)

        async def _later():
            await asyncio.sleep(delay)
            _deliver(s, "vitals", {"type": "vitals", "who": idx, "id": rid})

        asyncio.ensure_future(_later())

    ws = types.SimpleNamespace(send=fake_send)
    s._server_link_ws = ws          # routing uses the explicit link, not the _connections key
    s._connections = {}
    q = {"action": "query", "request": "get_vitals", "reply": "vitals", "eos": "EOS_T"}
    r0, r1 = await asyncio.gather(s._game_command_handler(q), s._game_command_handler(q))
    # Concurrent + out-of-order, but id-correlation routes each reply to its OWN waiter.
    assert r0["success"] and r0["data"]["who"] == 0
    assert r1["success"] and r1["data"]["who"] == 1


# --- V21-3: reply pacing spaces bunched replies (single ReplyText var coalesces) --

async def test_v21_3_reply_pacing_spaces_bunched_replies():
    """V21-3: every reply sets the mod's ONE replicated ReplyText var, which UE coalesces last-
    writer-wins, so two replies in one net-update window clobber and the loser's requester never
    sees their reply. _send_reply_paced serializes reply sends with a min gap so each value
    replicates to its target before the next overwrites it. Two bunched replies must be spaced
    >= _REPLY_MIN_GAP_S; a lone reply after idle pays ~0."""
    from sheldon_bridge.server import _REPLY_MIN_GAP_S
    s = object.__new__(BridgeServer)
    s._reply_lock = asyncio.Lock()
    s._last_reply_send = 0.0
    times = []

    async def fake_send(wire):
        times.append(asyncio.get_event_loop().time())

    ws = types.SimpleNamespace(send=fake_send)
    # two bunched replies (e.g. two players answered in the same tick) -> must be spaced out
    await asyncio.gather(s._send_reply_paced(ws, "A"), s._send_reply_paced(ws, "B"))
    assert len(times) == 2
    assert abs(times[1] - times[0]) >= _REPLY_MIN_GAP_S - 0.01, "bunched replies were not spaced"
    # a lone reply long after the last one pays ~0 (no needless latency on normal single replies)
    await asyncio.sleep(_REPLY_MIN_GAP_S + 0.05)
    t0 = asyncio.get_event_loop().time()
    await s._send_reply_paced(ws, "C")
    assert times[2] - t0 < 0.05, "a lone idle reply should not wait"


# --- V21-1: console_command routes to the requester's client via exec_command ----

async def test_v21_1_console_command_routes_to_requester_client():
    """V21-1: a console command is delivered to the requesting admin's CLIENT (real cheat manager
    → no server crash, Gotcha #34) via the cooked exec_command RepNotify channel — elevated with
    admincheat (no double-prefix), targeted by the requester eos, and REFUSED without one (an empty
    target would broadcast the admin command to every client)."""
    s = object.__new__(BridgeServer)
    sent = []

    async def fake_send(w):
        sent.append(w)

    s._server_link_ws = types.SimpleNamespace(send=fake_send)
    # with eos → an exec_command frame, admincheat-elevated, targeted
    r = await s._game_command_handler(
        {"action": "console_command", "command": "settimeofday 13:00:00", "eos": "EOS1"})
    assert r["success"] and sent
    assert '"type":"exec_command"' in sent[-1] and '"target":"EOS1"' in sent[-1]
    assert "admincheat settimeofday 13:00:00" in sent[-1]
    # already-elevated command is not double-prefixed
    sent.clear()
    await s._game_command_handler(
        {"action": "console_command", "command": "admincheat foo", "eos": "EOS1"})
    assert sent[-1].count("admincheat") == 1
    # no eos → refused (broadcast footgun)
    r2 = await s._game_command_handler(
        {"action": "console_command", "command": "settimeofday 1", "eos": ""})
    assert not r2["success"] and "broadcast" in r2["error"].lower()


# --- M2: session cap evicts the least-recently-active session ---------------------

def _pc(pid):
    return PlayerContext(player_id=pid, display_name=pid, tier="player")


def test_m2_session_cap_evicts_lru():
    sm = SessionManager(max_sessions=3)
    for pid in ("a", "b", "c"):
        sm.get_or_create(_pc(pid))
    assert sm.active_count == 3
    # make "a" the least-recently-active (largest idle); b and c are fresh
    sm.get("a").last_active -= 1000
    sm.get_or_create(_pc("d"))            # 4th → evict LRU
    assert sm.active_count == 3
    assert sm.get("a") is None and sm.get("d") is not None


# --- M3: turn fan-out admission cap sheds instead of spawning unbounded tasks ------

async def test_m3_admission_cap_sheds_load():
    s = object.__new__(BridgeServer)
    s._turn_tasks = set(range(_MAX_INFLIGHT_TURNS))   # already at capacity (dummy entries)
    sent = []

    async def send(w):
        sent.append(w)

    ws = types.SimpleNamespace(send=send)
    await s._handle_message({"type": "player_message", "message": "hi"}, None, ws)
    assert len(s._turn_tasks) == _MAX_INFLIGHT_TURNS    # no new task spawned
    assert sent and "error" in sent[0]                  # shed with an error frame
