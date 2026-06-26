"""clean_actor_name: raw ARK actor/class names -> readable species (death killer/victim + census)."""
from sheldon_bridge.names import clean_actor_name, parse_actor_ref


def test_runtime_instance_name():
    assert clean_actor_name("Raptor_Character_BP_C_2147482977") == "Raptor"
    assert clean_actor_name("Rex_Character_BP_C_12") == "Rex"


def test_default_prefix_and_bp_decorations():
    assert clean_actor_name("Default__Raptor_Character_BP_C") == "Raptor"
    assert clean_actor_name("Raptor_Character_BP_C") == "Raptor"


def test_already_clean_passes_through():
    assert clean_actor_name("Raptor") == "Raptor"
    assert clean_actor_name("Alpha Rex") == "Alpha Rex"


def test_modded_key_is_decamelcased():
    assert clean_actor_name("BobRaptor_Character_BP_C") == "Bob Raptor"
    assert clean_actor_name("Default__SuperRaptor_Character_BP_C_7") == "Super Raptor"


def test_nullish_is_empty():
    assert clean_actor_name("None") == ""
    assert clean_actor_name("") == ""
    assert clean_actor_name(None) == ""


def test_parse_actor_ref_full_instance_name():
    # what Blueprint GetObjectName yields at runtime for a wild dino killer
    r = parse_actor_ref("Raptor_Character_BP_C_2147482977")
    assert r == {"name": "Raptor", "class_key": "Raptor_Character_BP_C", "uid": "2147482977"}


def test_parse_actor_ref_modded_and_player():
    r = parse_actor_ref("BobRaptor_Character_BP_C_42")
    assert r["name"] == "Bob Raptor" and r["class_key"] == "BobRaptor_Character_BP_C" and r["uid"] == "42"
    p = parse_actor_ref("PlayerPawnTest_C_99")
    assert p["uid"] == "99" and p["class_key"] == "PlayerPawnTest_C"


def test_parse_actor_ref_no_suffix_and_nullish():
    r = parse_actor_ref("Raptor_Character_BP_C")        # defensive: no instance suffix
    assert r["name"] == "Raptor" and r["class_key"] == "Raptor_Character_BP_C" and r["uid"] is None
    assert parse_actor_ref("None") == {}
    assert parse_actor_ref("") == {}


# --- identity aliasing (legacy id <-> canonical id across an identity-scheme change) ---

from sheldon_bridge import names
from sheldon_bridge.names import canonical_id


def test_canonical_id_maps_alias_to_canonical(monkeypatch):
    # inject a generic alias map; the built-in default is empty (no deployment ids in source)
    monkeypatch.setattr(names, "IDENTITY_ALIASES", {"legacy_id": "canonical_id_value"})
    assert canonical_id("legacy_id") == "canonical_id_value"
    assert canonical_id("canonical_id_value") == "canonical_id_value"   # idempotent: a canonical id is not an alias key
    assert canonical_id("  legacy_id  ") == "canonical_id_value"        # whitespace-tolerant


def test_canonical_id_passthrough_and_safety():
    assert canonical_id("unknown_player") == "unknown_player"
    assert canonical_id("") == ""
    assert canonical_id(None) is None
