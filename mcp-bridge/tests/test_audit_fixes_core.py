"""V18 bridge audit-remediation — core concurrency/security fixes.

Covers the Critical/High bridge fixes from audit_report_2026_06_23.md:
  XDEL-01  getter timeout -> late-reply cross-delivery (THE #1 missing test)
  LEAK-01  lock-aware session eviction (split-brain guard)
  LEAK-02  transient canned rejections are not persisted as assistant history
"""
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from sheldon_bridge.server import BridgeServer, _GETTER_REPLY_LATENESS  # noqa: E402
from sheldon_bridge.session import SessionManager  # noqa: E402
from sheldon_bridge.auth import PlayerContext  # noqa: E402
from sheldon_bridge.agent import AgentResult  # noqa: E402


def _bare_server():
    s = object.__new__(BridgeServer)
    s._getter_lock = asyncio.Lock()
    s._query_waiters = {}
    return s


def _pc(pid):
    return PlayerContext(player_id=pid, display_name=pid, tier="player")


# --- XDEL-01: the timeout-then-late-reply cross-delivery (the recon's #1 missing test) -----------

async def test_xdel_late_reply_after_timeout_is_not_cross_delivered():
    """Getter A times out and leaves a tombstone; A's LATE reply (A's private vitals) must be
    DROPPED against A's own tombstone, NEVER handed to player B's live waiter. The pre-fix router
    popped the oldest waiter and delivered A's data to B (Player B shown Player A's health)."""
    s = _bare_server()
    wA = s._register_getter_waiter("vitals")   # player A's getter, in flight
    wA.abandoned = True                         # A timed out (exactly what _game_command_handler sets)
    wB = s._register_getter_waiter("vitals")   # player B's getter now in flight (same type, FIFO)
    # A's slow reply (A's private data) finally arrives on the link:
    s._deliver_typed_reply("vitals", {"type": "vitals", "who": "A", "health": 0.12})
    assert not wB.fut.done(), "CROSS-DELIVERY: player B received player A's late reply"
    # B's OWN reply then arrives and is delivered to B:
    s._deliver_typed_reply("vitals", {"type": "vitals", "who": "B", "health": 0.99})
    assert wB.fut.done() and wB.fut.result()["who"] == "B"


async def test_xdel_unsolicited_reply_with_no_waiter_is_dropped():
    """A reply with no pending waiter (late/unsolicited) is dropped cleanly, never crashes, never
    resurrects a future."""
    s = _bare_server()
    s._deliver_typed_reply("vitals", {"type": "vitals", "who": "ghost"})
    assert s._query_waiters.get("vitals", []) == []


async def test_xdel_expired_tombstone_does_not_starve_next_getter():
    """If a getter's reply is genuinely LOST, its tombstone EXPIRES and is purged so it cannot eat
    the next getter's reply (the only residual of the bridge-only fix is a bounded extra timeout,
    never a cross-delivery)."""
    s = _bare_server()
    wA = s._register_getter_waiter("vitals")
    wA.abandoned = True
    wA.sent_at -= (_GETTER_REPLY_LATENESS + 1.0)   # A's reply never came; tombstone now stale
    wB = s._register_getter_waiter("vitals")       # registering purges the stale tombstone
    assert len(s._query_waiters["vitals"]) == 1    # only B's live waiter remains
    s._deliver_typed_reply("vitals", {"type": "vitals", "who": "B"})
    assert wB.fut.done() and wB.fut.result()["who"] == "B"


async def test_xdel_distinct_types_do_not_interfere():
    """A timed-out getter of one type never affects another type's FIFO."""
    s = _bare_server()
    wV = s._register_getter_waiter("vitals"); wV.abandoned = True
    wP = s._register_getter_waiter("position")
    s._deliver_typed_reply("position", {"type": "position", "x": 1})
    assert wP.fut.done() and wP.fut.result()["x"] == 1


# --- LEAK-01 / SESSION-02: lock-aware eviction (split-brain guard) --------------------------------

async def test_leak01_eviction_skips_in_flight_locked_session():
    """A session whose turn is in-flight (lock held) must NOT be evicted — otherwise a concurrent
    get_or_create mints a duplicate Session for the same player (split-brain -> two locks)."""
    sm = SessionManager(max_sessions=2)
    sa = sm.get_or_create(_pc("a"))
    sm.get_or_create(_pc("b"))
    sm.get("a").last_active -= 1000          # 'a' is the LRU victim by idle...
    await sa.lock.acquire()                  # ...but 'a' is mid-turn (locked)
    try:
        sm.get_or_create(_pc("c"))           # eviction must skip locked 'a' and evict 'b' instead
        assert sm.get("a") is not None, "locked in-flight session was evicted (split-brain risk)"
        assert sm.get("c") is not None
    finally:
        sa.lock.release()


async def test_leak01_all_locked_defers_eviction_over_cap():
    """If EVERY session is mid-turn, eviction defers (temporarily over cap) rather than split-brain."""
    sm = SessionManager(max_sessions=1)
    sa = sm.get_or_create(_pc("a"))
    await sa.lock.acquire()
    try:
        sm.get_or_create(_pc("b"))           # cannot evict locked 'a' -> defer -> keep both
        assert sm.get("a") is not None and sm.get("b") is not None
        assert sm.active_count == 2          # temporarily over cap, by design
    finally:
        sa.lock.release()


# --- LEAK-02: transient canned rejections are not persisted ---------------------------------------

def test_leak02_agentresult_transient_flag():
    """Canned rejections (max-iter / turn-timeout) carry transient=True so the bridge skips
    persisting them as assistant history; transient is NOT an error (the UX text still shows)."""
    normal = AgentResult(response_text="hi", tool_calls_made=0, iterations=1, total_input_tokens=0,
                         total_output_tokens=0, total_cost=0.0, duration_ms=0.0)
    assert normal.transient is False
    canned = AgentResult(response_text="too hard", tool_calls_made=0, iterations=25,
                         total_input_tokens=0, total_output_tokens=0, total_cost=0.0,
                         duration_ms=0.0, transient=True)
    assert canned.transient is True and canned.error is None
