"""V10 getter-reply recovery: getters reply via SendPlayerMessage (the only send that
transmits from the mod's ubergraph). That wraps the payload as a player_message frame
without escaping the payload's quotes, so the frame isn't valid JSON. _extract_embedded_reply
must recover the embedded getter reply by the cooked reply-split technique."""
import json

from sheldon_bridge.server import _extract_embedded_reply, _QUERY_REPLY_TYPES


def wrap(payload: str) -> str:
    """Reproduce SendPlayerMessage's exact wrapper around a raw (unescaped) payload."""
    return ('{"type":"player_message","message":"' + payload +
            '","position":{"x":0,"y":0,"z":0},"facing_yaw":0.0}')


def test_each_getter_type_round_trips():
    payloads = {
        "vitals": '{"type":"vitals","health":"87.5"}',
        "position": '{"type":"position","loc":"X=120.5 Y=-90.0 Z=12.0"}',
        "inventory": '{"type":"inventory","items":"PrimalItemResource_Wood|42|0|0|100.0|false;Hatchet|1|2|0|88.0|false"}',
        "equipped": '{"type":"equipped","items":"PrimalItemArmor_ClothShirt|1|0|0|100.0|false"}',
        "tribe": '{"type":"tribe","name":"The Blue Obelisk Crew"}',
        "progression": '{"type":"progression","level":"104"}',
        "players": '{"type":"players","list":"PlayerOne|0002abc;PlayerTwo|0002def"}',
        "dinos": '{"type":"dinos","list":"Rex|true|X=1 Y=2 Z=3;Raptor|false|X=4 Y=5 Z=6"}',
        "look": '{"type":"look","eye":"X=1 Y=2 Z=3","aim":"P=0 Y=90 R=0"}',
        "buffs": '{"type":"buffs","list":"Well Fed;Mate Boosted"}',
        "engrams": '{"type":"engrams","free":12,"list":"Campfire|true;Forge|false;Spear|true"}',
        "dino_scan": '{"type":"dino_scan","data":"abc123|Rex|150|true|2000|Bob|X=1 Y=2 Z=3|1000|1000;def456|Argy|90|false|0||X=4 Y=5 Z=6|500|500"}',
        "world_time": '{"type":"world_time","day":47,"day_time":13.5}',           # V20-5
        "command_result": '{"type":"command_result","result":"Set time to 13:00"}',  # V20-7
    }
    assert set(payloads) == _QUERY_REPLY_TYPES
    for typ, payload in payloads.items():
        raw = wrap(payload)
        # the wrapped frame must NOT be valid JSON (that's why we need recovery)
        try:
            json.loads(raw)
            assert False, "wrapper unexpectedly parsed as JSON for %s" % typ
        except ValueError:
            pass
        d = _extract_embedded_reply(raw)
        assert d == json.loads(payload), "round-trip failed for %s: %r" % (typ, d)
        assert d["type"] == typ


def test_plain_player_message_is_not_a_reply():
    # a normal chat message is valid JSON and not an embedded getter reply
    assert _extract_embedded_reply('{"type":"player_message","message":"where am I?"}') is None


def test_telemetry_heartbeat_is_not_a_reply():
    assert _extract_embedded_reply(wrap("__telemetry:ws_recv")) is None


def test_unknown_payload_type_is_ignored():
    assert _extract_embedded_reply(wrap('{"type":"banana","x":"1"}')) is None


def test_non_player_message_returns_none():
    assert _extract_embedded_reply('{"type":"pong"}') is None
    assert _extract_embedded_reply('garbage') is None


def test_game_events_recovered_via_embedded_path():
    """Death/damage events ALSO ride SendPlayerMessage (wrapped -> invalid JSON), so the
    embedded-reply guard must admit them or they're silently dropped. (The V15 de-risk pass
    caught that the guard previously allowed only _QUERY_REPLY_TYPES — the clean-JSON dispatch
    never sees these frames because the wrapper is structurally invalid JSON.)"""
    death = '{"type":"death","victim":"PlayerOne","killer":"Rex","cause":"combat"}'
    dmg = '{"type":"damage","amount":42,"cause":"fall"}'
    assert _extract_embedded_reply(wrap(death)) == json.loads(death)
    assert _extract_embedded_reply(wrap(dmg)) == json.loads(dmg)
    # the *_event spellings ride the same path
    assert _extract_embedded_reply(wrap('{"type":"death_event","killer":"Wolf"}')) is not None


def test_server_state_recovered_via_embedded_path():
    """The mod's get_server_state answer (the map's GPS constants) ALSO rides SendPlayerMessage
    (wrapped -> invalid JSON), so the embedded-reply guard must admit 'server_state' or the
    constants are silently dropped and GPS never lights up. Same drop class as the game events."""
    ss = ('{"type":"server_state","gps_lat_origin":"-400000","gps_lon_origin":"-400000",'
          '"gps_lat_scale":"1310","gps_lon_scale":"1310"}')
    assert _extract_embedded_reply(wrap(ss)) == json.loads(ss)
