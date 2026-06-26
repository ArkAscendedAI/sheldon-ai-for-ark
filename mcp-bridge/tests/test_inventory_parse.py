"""Unit tests for the inventory-getter wire parser (`_parse_inventory_items`) and the
`get_inventory` tool's mock path. The parser turns the cooked getter's compact
`class|qty|qual|rating|durab|isSkin;...` payload into structured items for the LLM.
"""
import asyncio

from sheldon_bridge.tools.actions import (
    _clean_item_class,
    _parse_inventory_items,
    _parse_delimited,
    get_dinos,
    get_equipped,
    get_inventory,
    get_look,
    get_players,
    get_progression,
    get_snapshot,
    get_tribe,
)


def test_clean_item_class_strips_suffix_and_path():
    assert _clean_item_class("PrimalItemResource_Wood_C") == "PrimalItemResource_Wood"
    assert _clean_item_class("/Game/Foo/Bar.PrimalItem_Hatchet_C") == "PrimalItem_Hatchet"
    assert _clean_item_class("Class'/Script/ShooterGame.PrimalItem_Foo_C'") == "PrimalItem_Foo"
    assert _clean_item_class("   ") == ""


def test_full_record():
    items = _parse_inventory_items("PrimalItemResource_Wood_C|42|0|0.0|100.0|false")
    assert items == [{
        "item": "PrimalItemResource_Wood",
        "quantity": 42,
        "quality": "Primitive",
        "rating": 0.0,
        "durability_pct": 100.0,
        "is_skin": False,
    }]


def test_quality_tier_mapping_and_skin_true():
    items = _parse_inventory_items("PrimalItem_WeaponMetalHatchet_C|1|5|2.75|88.4|true")
    it = items[0]
    assert it["quality"] == "Ascendant"
    assert it["rating"] == 2.75
    assert it["durability_pct"] == 88.4
    assert it["is_skin"] is True


def test_unknown_quality_falls_back_to_tier_label():
    items = _parse_inventory_items("PrimalItem_Foo_C|1|9")
    assert items[0]["quality"] == "Tier9"


def test_multiple_items_and_trailing_separator_skipped():
    items = _parse_inventory_items("ItemA_C|2|1;ItemB_C|5|2;")
    assert [i["item"] for i in items] == ["ItemA", "ItemB"]
    assert items[0]["quality"] == "Ramshackle"
    assert items[1]["quantity"] == 5


def test_short_record_only_fills_present_fields():
    items = _parse_inventory_items("ItemA_C")
    assert items == [{"item": "ItemA"}]


def test_empty_and_blank_payloads():
    assert _parse_inventory_items("") == []
    assert _parse_inventory_items("   ") == []
    assert _parse_inventory_items(";;;") == []


def test_record_with_empty_fields_skips_those_fields():
    # qty present, quality blank, rating present
    items = _parse_inventory_items("ItemA_C|3||1.5")
    it = items[0]
    assert it["quantity"] == 3
    assert "quality" not in it
    assert it["rating"] == 1.5


def test_unparseable_numerics_are_dropped_not_raised():
    items = _parse_inventory_items("ItemA_C|notanumber|abc|x|y|maybe")
    it = items[0]
    assert it["item"] == "ItemA"
    assert "quantity" not in it and "quality" not in it
    assert "rating" not in it and "durability_pct" not in it
    assert it["is_skin"] is False  # "maybe" -> not truthy


def test_record_missing_class_is_skipped():
    assert _parse_inventory_items("|5|0") == []


def test_get_inventory_mock_path():
    res = asyncio.run(get_inventory(ctx=None))
    assert res["success"] and res.get("mock") is True
    assert res["count"] == len(res["items"]) == 2


def test_get_inventory_parses_handler_payload():
    async def fake_handler(cmd):
        assert cmd == {"action": "query", "request": "get_inventory", "reply": "inventory", "eos": None}
        return {"success": True, "data": {"items": "Wood_C|10|0;Stone_C|7|0"}}

    res = asyncio.run(get_inventory(ctx={"game_handler": fake_handler}))
    assert res["success"] and res["count"] == 2
    assert res["items"][0] == {"item": "Wood", "quantity": 10, "quality": "Primitive"}


def test_get_inventory_propagates_handler_failure():
    async def fail_handler(cmd):
        return {"success": False, "error": "The server didn't return inventory in time."}

    res = asyncio.run(get_inventory(ctx={"game_handler": fail_handler}))
    assert res["success"] is False and "didn't return inventory" in res["error"]


def test_get_equipped_reuses_inventory_parser():
    async def fake_handler(cmd):
        assert cmd == {"action": "query", "request": "get_equipped", "reply": "equipped", "eos": None}
        return {"success": True, "data": {"items": "PrimalItemArmor_MetalHelmet_C|1|4|1.2|73.0|false"}}

    res = asyncio.run(get_equipped(ctx={"game_handler": fake_handler}))
    assert res["success"] and res["count"] == 1
    eq = res["equipped"][0]
    assert eq["item"] == "PrimalItemArmor_MetalHelmet" and eq["quality"] == "Mastercraft"
    assert eq["durability_pct"] == 73.0


def test_get_equipped_mock_path():
    res = asyncio.run(get_equipped(ctx=None))
    assert res["success"] and res.get("mock") is True and res["count"] == 1


def test_get_tribe_handler_and_solo():
    async def in_tribe(cmd):
        assert cmd == {"action": "query", "request": "get_tribe", "reply": "tribe", "eos": None}
        return {"success": True, "data": {"name": "Bronze"}}

    async def solo(cmd):
        return {"success": True, "data": {"name": ""}}   # empty -> None (not in a tribe)

    assert asyncio.run(get_tribe(ctx={"game_handler": in_tribe}))["tribe"] == "Bronze"
    assert asyncio.run(get_tribe(ctx={"game_handler": solo}))["tribe"] is None
    assert asyncio.run(get_tribe(ctx=None)).get("mock") is True


def test_get_progression_coerces_level_to_int():
    async def handler(cmd):
        assert cmd == {"action": "query", "request": "get_progression", "reply": "progression", "eos": None}
        return {"success": True, "data": {"level": "73"}}   # getter sends it as a string

    assert asyncio.run(get_progression(ctx={"game_handler": handler}))["level"] == 73
    assert asyncio.run(get_progression(ctx=None))["level"] == 50


def test_parse_delimited_and_get_players():
    assert _parse_delimited("Alice|0001;Bob|0002;", ["name", "eos"]) == [
        {"name": "Alice", "eos": "0001"}, {"name": "Bob", "eos": "0002"}]
    assert _parse_delimited("", ["name", "eos"]) == []
    assert _parse_delimited("Solo", ["name", "eos"]) == [{"name": "Solo"}]

    async def handler(cmd):
        assert cmd == {"action": "query", "request": "get_players", "reply": "players", "eos": None}
        return {"success": True, "data": {"list": "Alice|0001;Bob|0002"}}

    res = asyncio.run(get_players(ctx={"game_handler": handler}))
    assert res["count"] == 2 and res["players"][0] == {"name": "Alice", "eos": "0001"}
    assert asyncio.run(get_players(ctx=None)).get("mock") is True


def test_get_dinos_coerces_tamed_to_bool():
    async def handler(cmd):
        assert cmd == {"action": "query", "request": "get_dinos", "reply": "dinos", "eos": None}
        return {"success": True, "data": {"list": "Raptor|false|X=1 Y=2 Z=3;Rex|true|X=9"}}

    res = asyncio.run(get_dinos(ctx={"game_handler": handler}))
    assert res["count"] == 2
    assert res["dinos"][0] == {"species": "Raptor", "tamed": False, "loc": "X=1 Y=2 Z=3"}
    assert res["dinos"][1]["tamed"] is True


def test_get_look_passthrough():
    async def handler(cmd):
        assert cmd == {"action": "query", "request": "get_look", "reply": "look", "eos": None}
        return {"success": True, "data": {"eye": "X=1 Y=2 Z=3", "aim": "P=0 Y=90 R=0"}}

    res = asyncio.run(get_look(ctx={"game_handler": handler}))
    assert res["eye"] == "X=1 Y=2 Z=3" and res["aim"] == "P=0 Y=90 R=0"
    assert asyncio.run(get_look(ctx=None)).get("mock") is True


def test_get_snapshot_lite_and_full_profiles():
    DATA = {
        "vitals": {"health": 90.0}, "position": {"loc": "X=1"},
        "inventory": {"items": "Wood_C|10|0"}, "equipped": {"items": "Helmet_C|1|4|1|80|false"},
        "tribe": {"name": "Bronze"}, "progression": {"level": "60"},
    }

    async def handler(cmd):
        return {"success": True, "data": DATA[cmd["reply"]]}

    lite = asyncio.run(get_snapshot(profile="lite", ctx={"game_handler": handler}))
    assert lite["profile"] == "lite" and set(lite["snapshot"]) == {"vitals", "position"}
    full = asyncio.run(get_snapshot(profile="full", ctx={"game_handler": handler}))
    assert set(full["snapshot"]) == {"vitals", "position", "inventory", "equipped", "tribe", "progression"}
    assert full["snapshot"]["progression"]["level"] == 60
    assert full["snapshot"]["inventory"]["items"][0]["item"] == "Wood"
    # unknown profile falls back to lite
    assert asyncio.run(get_snapshot(profile="bogus", ctx={"game_handler": handler}))["profile"] == "lite"
