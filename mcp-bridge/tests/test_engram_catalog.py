"""Engram class-key -> clean display name. The cooked getter emits the engram's class key
(GetDisplayName); the bridge maps it to a clean name via the vanilla catalog, with a heuristic
fallback for modded engrams. Tolerant of the unknown runtime key form (Default__ prefix /
_C_<n> instance suffix) so it works on the first cook without re-cooking."""
from sheldon_bridge.engram_catalog import clean_engram_name


def test_known_engram_exact_from_catalog():
    assert clean_engram_name("EngramEntry_Campfire_C") == "Campfire"
    assert clean_engram_name("EngramEntry_StoneHatchet_C") == "Stone Hatchet"
    assert clean_engram_name("EngramEntry_StoneClub_C") == "Wooden Club"


def test_default_prefix_tolerated():
    assert clean_engram_name("Default__EngramEntry_Campfire_C") == "Campfire"


def test_instance_suffix_tolerated():
    assert clean_engram_name("EngramEntry_Campfire_C_12") == "Campfire"
    assert clean_engram_name("Default__EngramEntry_Campfire_C_3") == "Campfire"


def test_unknown_mod_engram_falls_back_to_heuristic():
    # not in the vanilla catalog -> de-camelCase the key
    assert clean_engram_name("EngramEntry_FooBarBaz_C") == "Foo Bar Baz"
    assert clean_engram_name("Default__EngramEntry_FooBarBaz_C") == "Foo Bar Baz"


def test_empty_is_safe():
    assert clean_engram_name("") == ""
    assert clean_engram_name(None) == ""


async def test_get_engrams_v18_requirements_parse():
    """V18 cook format: Name|learned|reqLevel|reqPoints|groupByte (8=Tek). Parser fills a
    requirements{} lookup; a name containing '|' still parses (rsplit from the right)."""
    from sheldon_bridge.tools.actions import get_engrams

    async def gh(_cmd):
        return {"success": True, "data": {"free": 7,
                "list": "EngramEntry_Campfire_C|1|3|0|2;EngramEntry_TekReplicator_C|0|85|45|8"}}

    r = await get_engrams(ctx={"game_handler": gh})
    assert r["success"] and r["free_engram_points"] == 7
    assert r["learned_count"] == 1 and r["unlearned_count"] == 1
    reqs = r["requirements"]
    assert any(v["required_level"] == 85 and v["required_points"] == 45 and v["tek"] for v in reqs.values())
    assert any(v["group"] == "prime" and not v["tek"] for v in reqs.values())


async def test_get_engrams_pre_v18_backcompat():
    """Pre-V18 cook emits just Name|learned -> learned/unlearned unchanged, requirements empty."""
    from sheldon_bridge.tools.actions import get_engrams

    async def gh(_cmd):
        return {"success": True, "data": {"free": 5, "list": "EngramEntry_Campfire_C|1;EngramEntry_Forge_C|0"}}

    r = await get_engrams(ctx={"game_handler": gh})
    assert r["learned_count"] == 1 and r["unlearned_count"] == 1 and r["requirements"] == {}
