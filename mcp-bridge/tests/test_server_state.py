"""Out-of-band server_state push — the V17 GPS item-4 bridge half. The mod answers a
get_server_state request with the map's live PrimalWorldSettings GPS constants; the bridge
persists them to telemetry.server_state, where gps.from_server_state + the assembler/find_dinos
read them (raw-coords fallback until present, so a mod that never publishes can't yield a wrong
GPS). The wire frame is a two-sided contract with the not-yet-built mod GPS graft, pinned here."""
import asyncio
import types

from sheldon_bridge import gps, server as srv
from sheldon_bridge.server import BridgeServer, _query_wire, _STATE_PUSH_TYPES
from sheldon_bridge.db.manager import DBManager
from sheldon_bridge.db.telemetry_store import TelemetryStore

# Ragnarok-ish constants (the live mod will publish the real ones); shape is what matters here.
RAG_STATE = {"type": "server_state", "gps_lat_origin": "-400000", "gps_lon_origin": "-400000",
             "gps_lat_scale": "1310", "gps_lon_scale": "1310"}
_GPS_KEYS = ("gps_lat_origin", "gps_lon_origin", "gps_lat_scale", "gps_lon_scale")


class _FakeWS:
    def __init__(self):
        self.sent = []

    async def send(self, data):
        self.sent.append(data)


def _mgr(tmp_path):
    return DBManager(str(tmp_path), default_cluster_id="examplecluster",
                     default_server_id="ragnarok", server_ip_map={})


def _srv(mgr):
    s = object.__new__(BridgeServer)
    s.db = mgr
    return s


def _scope():
    # _handle_server_state / _request_server_state only touch scope.cluster_id + .server_id
    return types.SimpleNamespace(cluster_id="examplecluster", server_id="ragnarok")


async def _tel(mgr):
    return TelemetryStore(await mgr.telemetry("examplecluster", "ragnarok"))


def test_server_state_is_a_state_push_type_not_a_query_reply():
    # it must be admitted by the embedded-reply guard but NOT a getter waiter type
    from sheldon_bridge.server import _QUERY_REPLY_TYPES
    assert "server_state" in _STATE_PUSH_TYPES
    assert "server_state" not in _QUERY_REPLY_TYPES


def test_handle_server_state_persists_gps_constants(tmp_path):
    async def go():
        mgr = _mgr(tmp_path)
        s = _srv(mgr)
        await s._handle_server_state(RAG_STATE, types.SimpleNamespace(scope=_scope()))
        tel = await _tel(mgr)
        state = await tel.get_server_state()
        assert {k: state[k] for k in _GPS_KEYS} == {k: RAG_STATE[k] for k in _GPS_KEYS}
        # and they're now usable: gps.from_server_state builds a converter (raw-coords fallback gone)
        assert gps.from_server_state(state) is not None
        await mgr.close()
    asyncio.run(go())


def test_handle_server_state_ignores_empty_frame(tmp_path):
    async def go():
        mgr = _mgr(tmp_path)
        s = _srv(mgr)
        await s._handle_server_state({"type": "server_state"}, types.SimpleNamespace(scope=_scope()))
        tel = await _tel(mgr)
        assert gps.from_server_state(await tel.get_server_state()) is None  # nothing stored, still no guess
        await mgr.close()
    asyncio.run(go())


def test_requester_asks_repeatedly_until_exhausted_pre_cook(tmp_path, monkeypatch):
    """Pre-V17 mod has no get_server_state branch and never answers → the requester sends the
    (harmless, unmatched) request each retry and quietly gives up. No constants get invented."""
    monkeypatch.setattr(srv, "_SERVER_STATE_RETRY_S", 0.0)
    monkeypatch.setattr(srv, "_SERVER_STATE_TRIES", 3)

    async def go():
        mgr = _mgr(tmp_path)
        s = _srv(mgr)
        ws = _FakeWS()
        await s._request_server_state(_scope(), ws)
        assert len(ws.sent) == 3
        assert all(m == _query_wire("get_server_state") for m in ws.sent)
        tel = await _tel(mgr)
        assert gps.from_server_state(await tel.get_server_state()) is None
        await mgr.close()
    asyncio.run(go())


def test_requester_check_first_sends_nothing_when_already_cached(tmp_path, monkeypatch):
    """Constants are static per map, so a prior session's valid capture is still correct →
    the requester sends nothing on reconnect (no wasted frames)."""
    monkeypatch.setattr(srv, "_SERVER_STATE_RETRY_S", 0.0)

    async def go():
        mgr = _mgr(tmp_path)
        s = _srv(mgr)
        ws = _FakeWS()
        tel = await _tel(mgr)
        for k in _GPS_KEYS:
            await tel.set_server_state(k, RAG_STATE[k])
        await s._request_server_state(_scope(), ws)
        assert ws.sent == []
        await mgr.close()
    asyncio.run(go())


def test_requester_stops_once_constants_land_mid_loop(tmp_path, monkeypatch):
    """If the answer arrives after the first ask, the requester notices on the next poll and
    stops (it doesn't keep asking for the full retry count)."""
    monkeypatch.setattr(srv, "_SERVER_STATE_RETRY_S", 0.0)
    monkeypatch.setattr(srv, "_SERVER_STATE_TRIES", 5)

    async def go():
        mgr = _mgr(tmp_path)
        s = _srv(mgr)
        tel = await _tel(mgr)

        # WS that "delivers" the mod's answer the first time the bridge asks (writes the
        # constants), simulating _handle_server_state running between retries.
        class _AnsweringWS(_FakeWS):
            async def send(self, data):
                await super().send(data)
                for k in _GPS_KEYS:
                    await tel.set_server_state(k, RAG_STATE[k])

        ws = _AnsweringWS()
        await s._request_server_state(_scope(), ws)
        assert len(ws.sent) == 1  # asked once; next poll saw the constants and returned
        await mgr.close()
    asyncio.run(go())
