"""V18 A1 dino events: bridge captures dino_tamed/dino_death (team/tribe-scoped, no EOS) and the
get_recent_dino_events tool returns ONLY the asker's own tribe (privacy) — a tame names the tribe,
a death names the team which is mapped to a tribe via the team->tribe cache learned from tames."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sheldon_bridge.server import BridgeServer, _GAME_EVENT_TYPES
from sheldon_bridge.tools.actions import get_recent_dino_events


def test_dino_types_admitted():
    assert _GAME_EVENT_TYPES.get("dino_tamed") == "dino_tamed"
    assert _GAME_EVENT_TYPES.get("dino_death") == "dino_death"


def test_dino_summaries():
    s = BridgeServer._summarize_game_event
    assert "Rex" in s("dino_tamed", {"dino": "Rex", "tribe": "Wolves"})
    assert "killed by a Giga" in s("dino_death", {"dino": "Rex", "killer": "a Giga"})
    assert "died" in s("dino_death", {"dino": "Dodo"})  # no killer -> graceful


async def test_tool_returns_only_my_tribe():
    async def gh(req):  # mock the get_tribe getter -> "Wolves"
        return {"success": True, "data": {"name": "Wolves"}}
    ctx = {
        "game_handler": gh, "eos_id": "p1",
        "team_tribe": {"100": "Wolves", "999": "Enemies"},
        "dino_events": [
            {"event": "dino_tamed", "at": 1, "summary": "Tamed a Rex",
             "data": {"dino": "Rex", "tribe": "Wolves", "team": "100"}},
            {"event": "dino_death", "at": 2, "summary": "Your tribe's Raptor died",
             "data": {"dino": "Raptor", "team": "100"}},          # team->Wolves (mine)
            {"event": "dino_death", "at": 3, "summary": "x",
             "data": {"dino": "Dodo", "team": "999"}},            # enemy tribe -> filtered out
        ],
    }
    r = await get_recent_dino_events(limit=8, ctx=ctx)
    dinos = {e["dino"] for e in r["events"]}
    assert dinos == {"Rex", "Raptor"}        # my tribe only (tame by name, death by team->tribe)
    assert "Dodo" not in dinos               # other team never leaks


async def test_tool_empty_when_no_tribe():
    async def gh(req):
        return {"success": True, "data": {"name": None}}   # solo / unresolved
    r = await get_recent_dino_events(ctx={"game_handler": gh, "eos_id": "p1",
                                          "dino_events": [{"event": "dino_death", "at": 1,
                                                           "data": {"dino": "Rex", "team": "5"}}],
                                          "team_tribe": {}})
    assert r["events"] == []                  # no cross-tribe leak when tribe can't be resolved
