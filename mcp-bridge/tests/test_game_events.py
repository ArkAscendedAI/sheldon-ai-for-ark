"""Unsolicited death/damage event frames from the mod's universal event buff.

Pinned here because they're mod-version-fragile and scale-sensitive:
1. The bridge must accept BOTH the V13 spellings (death_event/damage_event) AND the V15
   rebuild's spellings (death/damage) → a stable canonical kind, so a mod-side type-string
   choice can't silently drop the frame (the bug class that dropped buffs/vitals replies).
2. get_recent_events surfaces a field-tolerant one-line 'summary' (never KeyErrors).
3. Damage is a HOT path — the bridge rate-caps + coalesces damage so a raid-scale burst
   (50 players taking AOE) can't overwhelm the pipeline or evict rare death events from the
   bounded ring. Death is NEVER throttled.
"""
import asyncio
from collections import deque

from sheldon_bridge.server import (
    BridgeServer, _GAME_EVENT_TYPES, _DAMAGE_RATE_CAP, _DAMAGE_COALESCE_WINDOW_S,
    _DAMAGE_MIN_AMOUNT,
)

S = BridgeServer._summarize_game_event


def _srv() -> BridgeServer:
    """A BridgeServer with just the event-ring state set, bypassing the heavy __init__ —
    enough to exercise _handle_game_event / _admit_damage_event / _coalesce_*."""
    s = object.__new__(BridgeServer)
    s._recent_game_events = deque(maxlen=20)
    s._damage_ingest_times = deque(maxlen=64)
    s._damage_dropped = 0
    return s


def _damage(amount=50, cause="combat"):  # default ABOVE the report floor so flood tests store it
    return {"type": "damage", "amount": amount, "cause": cause}


# --- routing / canonicalization -------------------------------------------------

def test_both_spellings_map_to_stable_canonical_kind():
    assert _GAME_EVENT_TYPES["death"] == "death_event"
    assert _GAME_EVENT_TYPES["death_event"] == "death_event"
    assert _GAME_EVENT_TYPES["damage"] == "damage_event"
    assert _GAME_EVENT_TYPES["damage_event"] == "damage_event"


def test_unrelated_type_is_not_a_game_event():
    assert "player_message" not in _GAME_EVENT_TYPES
    assert "vitals" not in _GAME_EVENT_TYPES


# --- summary gloss --------------------------------------------------------------

def test_death_summary_is_field_tolerant():
    assert S("death_event", {"killer": "Rex", "cause": "combat"}) == "Killed by Rex (combat)"
    assert S("death_event", {"killer": "Allosaurus"}) == "Killed by Allosaurus"
    assert S("death_event", {"cause": "fall"}) == "Died: fall"
    assert S("death_event", {}) == "Died (cause unknown)"
    assert S("death_event", {"victim": "PlayerOne", "killer": "Wolf"}) == "Killed by Wolf"


def test_damage_summary_is_field_tolerant():
    assert S("damage_event", {"amount": 42, "cause": "fall"}) == "Took 42 damage from fall"
    assert S("damage_event", {"amount": 42}) == "Took 42 damage"
    assert S("damage_event", {"cause": "fire"}) == "Took damage from fire"
    assert S("damage_event", {}) == "Took damage"
    assert S("damage_event", {"amount": 0}) == "Took 0 damage"  # 0 is a real value, not "absent"


def test_unknown_kind_falls_back_to_kind_string():
    assert S("weird_event", {"x": 1}) == "weird_event"


# --- flood control (damage hot path) --------------------------------------------

def test_death_is_never_throttled_or_coalesced():
    s = _srv()
    for i in range(3):
        s._handle_game_event("death_event", {"type": "death", "killer": f"K{i}"})
    assert len(s._recent_game_events) == 3
    assert all(e["event"] == "death_event" for e in s._recent_game_events)
    assert s._damage_dropped == 0


def test_rapid_damage_coalesces_into_one_rolling_entry():
    s = _srv()
    n = _DAMAGE_RATE_CAP - 2  # under the cap → none dropped, all coalesced
    for _ in range(n):
        s._handle_game_event("damage_event", _damage())
    dmg = [e for e in s._recent_game_events if e["event"] == "damage_event"]
    assert len(dmg) == 1
    assert dmg[0]["data"]["count"] == n
    assert dmg[0]["summary"].endswith(f"(x{n})")
    assert s._damage_dropped == 0


def test_rate_cap_bounds_ingestion_under_a_burst():
    s = _srv()
    burst = 30
    for _ in range(burst):
        s._handle_game_event("damage_event", _damage())
    dmg = [e for e in s._recent_game_events if e["event"] == "damage_event"]
    assert len(dmg) == 1
    admitted = dmg[0]["data"].get("count", 1)
    assert admitted <= _DAMAGE_RATE_CAP          # never ingest more than the cap per window
    assert s._damage_dropped > 0                 # excess was shed
    assert admitted + s._damage_dropped == burst  # nothing lost unaccounted-for


def test_a_death_survives_a_damage_burst():
    s = _srv()
    s._handle_game_event("death_event", {"type": "death", "killer": "Alpha Rex"})
    for _ in range(30):
        s._handle_game_event("damage_event", _damage())
    # the (rare, critical) death must still be in the ring after the burst
    assert s._recent_game_events[0]["event"] == "death_event"
    assert s._recent_game_events[0]["data"]["killer"] == "Alpha Rex"
    dmg = [e for e in s._recent_game_events if e["event"] == "damage_event"]
    assert len(dmg) == 1  # the whole burst collapsed to one rolling entry


def test_recovered_death_routes_through_handle_message_to_ring():
    """End-to-end seam (the de-risk critic flagged this as untested): a death frame — once the
    embedded guard admits it — must route through _handle_message -> _handle_game_event into the
    ring. The death msg_type skips the player_message/position/query branches and hits the
    _GAME_EVENT_TYPES branch, so session/websocket are unused on this path."""
    s = _srv()
    frame = {"type": "death", "victim": "PlayerOne", "killer": "Alpha Rex", "cause": "combat"}
    asyncio.run(s._handle_message(frame, None, None))
    assert len(s._recent_game_events) == 1
    e = s._recent_game_events[0]
    assert e["event"] == "death_event" and e["data"]["killer"] == "Alpha Rex"
    assert "Alpha Rex" in e["summary"]


# --- per-player death attribution + identity-scheme aliasing (legacy id <-> canonical) -----------

from sheldon_bridge import names

# A generic alias pair (legacy id -> canonical id). The built-in default is empty — no
# deployment-specific ids live in source — so tests that exercise the aliasing inject it.
_NUM, _EOS = "legacy_player_id", "canonical_eos_id"


def _activate_alias(monkeypatch):
    monkeypatch.setattr(names, "IDENTITY_ALIASES", {_NUM: _EOS})


def test_death_victim_is_canonicalized_on_ingest(monkeypatch):
    _activate_alias(monkeypatch)
    s = _srv()
    s._handle_game_event("death_event", {"type": "death", "victim": _NUM, "killer": "Bronto"})
    assert s._recent_game_events[0]["data"]["victim"] == _EOS


def test_recent_events_for_matches_across_numeric_eos_alias(monkeypatch):
    """An identity-scheme switch in one assertion: the death buff stamps the canonical victim while
    the chat C-identity still sends the legacy id. A survivor asking "what killed me?" must see their
    own death whether their turn's requester id arrives as the legacy id OR the canonical id."""
    _activate_alias(monkeypatch)
    s = _srv()
    s._handle_game_event("death_event", {"type": "death", "victim": _EOS, "killer": "Bronto"})
    assert any(e["event"] == "death_event" for e in s._recent_events_for(_NUM))  # legacy id
    assert any(e["event"] == "death_event" for e in s._recent_events_for(_EOS))  # canonical id


def test_recent_events_for_scopes_death_to_its_victim():
    s = _srv()
    s._handle_game_event("death_event", {"type": "death", "victim": _EOS, "killer": "Bronto"})
    assert not any(e["event"] == "death_event" for e in s._recent_events_for("someone_else"))


def test_recent_events_for_passes_victimless_death_and_damage():
    s = _srv()
    s._handle_game_event("death_event", {"type": "death", "killer": "Lava"})  # env death, no victim
    s._handle_game_event("damage_event", _damage())                           # no victim set
    seen = s._recent_events_for("anybody")
    assert any(e["event"] == "death_event" for e in seen)   # victimless death = timing fallback
    assert any(e["event"] == "damage_event" for e in seen)  # victimless damage = timing fallback


# --- V18: damage is a per-player event (multiplayer scoping) + amount floor + cause cleaning ----

def test_trivial_damage_below_floor_is_dropped():
    """The mod has no amount threshold (only a rate-limit) — chip damage is filtered HERE so it's
    tunable with no cook. A hit below the floor never reaches the ring."""
    s = _srv()
    s._handle_game_event("damage_event", {"type": "damage", "amount": _DAMAGE_MIN_AMOUNT - 1, "victim": _EOS})
    assert not any(e["event"] == "damage_event" for e in s._recent_game_events)


def test_significant_damage_above_floor_is_stored():
    s = _srv()
    s._handle_game_event("damage_event", {"type": "damage", "amount": _DAMAGE_MIN_AMOUNT, "victim": _EOS})
    assert any(e["event"] == "damage_event" for e in s._recent_game_events)


def test_damage_is_scoped_to_its_victim(monkeypatch):
    """A buffed player's damage carries their EOS as victim — so player A's combat must NOT bleed
    into player B's turn (the 20-player privacy/cross-attribution bar, same as deaths)."""
    _activate_alias(monkeypatch)
    s = _srv()
    s._handle_game_event("damage_event", {"type": "damage", "amount": 80, "victim": _EOS, "cause": "Rex"})
    assert any(e["event"] == "damage_event" for e in s._recent_events_for(_EOS))      # mine
    assert any(e["event"] == "damage_event" for e in s._recent_events_for(_NUM))      # mine (alias)
    assert not any(e["event"] == "damage_event" for e in s._recent_events_for("someone_else"))


def test_damage_cause_actor_is_cleaned_to_species():
    s = _srv()
    s._handle_game_event("damage_event", {"type": "damage", "amount": 80, "victim": _EOS,
                                          "cause": "Rex_Character_BP_C_2147483647"})
    e = next(x for x in s._recent_game_events if x["event"] == "damage_event")
    assert e["data"]["cause"] == "Rex"                 # GetObjectName actor -> clean species
    assert e["summary"] == "Took 80 damage from Rex"
