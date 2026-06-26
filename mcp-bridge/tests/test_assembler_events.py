"""The assembler PUSHES recent deaths into the system prompt so 'what killed me?' works
without the LLM having to call get_recent_events (the V17 'death looks dead' regression: the
frame arrived but the model never pulled it, so it claimed the log was empty)."""
import time

from sheldon_bridge.db.assembler import ContextAssembler, _DEATH_PUSH_MAX


def _a():
    return ContextAssembler()


def test_pushes_a_recent_death_with_anti_hallucination_framing():
    now = time.time()
    blk = _a()._recent_deaths_block(
        [{"event": "death_event", "at": now - 60, "summary": "Killed by Sauropod", "data": {"victim": "e"}}]
    )
    assert "Sauropod" in blk
    assert "event log" in blk.lower()           # framed as THE log so it won't say "empty"
    assert "not listed" in blk.lower()          # and won't invent a cause


def test_skips_stale_deaths_and_all_damage():
    now = time.time()
    evs = [
        {"event": "death_event", "at": now - 99999, "summary": "ancient death"},
        {"event": "damage_event", "at": now, "summary": "took 5 damage"},
    ]
    assert _a()._recent_deaths_block(evs) == ""      # too old + damage is tool-only
    assert _a()._recent_deaths_block([]) == ""
    assert _a()._recent_deaths_block(None) == ""


def test_caps_and_orders_newest_first():
    now = time.time()
    # deque order is oldest -> newest (append puts newest at the end)
    evs = [{"event": "death_event", "at": now - age, "summary": f"d{age}"}
           for age in (300, 240, 180, 120, 60, 1)]
    blk = _a()._recent_deaths_block(evs)
    assert blk.count("\n- ") == _DEATH_PUSH_MAX                       # capped
    assert blk.index("d1") < blk.index("d60") < blk.index("d120")     # newest first
    assert "d300" not in blk                                          # oldest dropped by the cap
