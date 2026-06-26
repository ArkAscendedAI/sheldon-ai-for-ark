"""World -> in-game GPS conversion (sheldon_bridge.gps).

The Island constants (origin -400000, scale 800 -> multiplier 8000, shift 50) are the
worked reference: world (0,0) is map center (50,50). The off-center, non-diagonal cases
catch an X/Y swap or a multiply/divide mistake that the symmetric center test would miss.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sheldon_bridge import gps

ISLAND = gps.MapGPS(lat_origin=-400000.0, lon_origin=-400000.0, lat_scale=800.0, lon_scale=800.0)


def test_convert_center_and_offcenter():
    assert tuple(round(v, 6) for v in ISLAND.convert(0.0, 0.0)) == (50.0, 50.0)  # map center
    # lon from X only (lat must NOT move) — catches an X/Y swap
    lat, lon = ISLAND.convert(200000.0, 0.0)
    assert round(lat, 6) == 50.0 and round(lon, 6) == 75.0
    # lat from Y only
    lat, lon = ISLAND.convert(0.0, 200000.0)
    assert round(lat, 6) == 75.0 and round(lon, 6) == 50.0


def test_from_server_state():
    state = {"gps_lat_origin": "-400000", "gps_lon_origin": "-400000",
             "gps_lat_scale": "800", "gps_lon_scale": "800", "census_bounds": "[1,2,3,4]"}
    m = gps.from_server_state(state)
    assert m is not None and m.convert(0, 0) == (50.0, 50.0)
    # absent constants (no cook yet) -> None: caller falls back to raw coords, never a guess
    assert gps.from_server_state({"census_bounds": "[1,2,3,4]"}) is None
    assert gps.from_server_state(None) is None and gps.from_server_state({}) is None
    # zero scale -> None (no divide-by-zero); unparseable -> None
    assert gps.from_server_state({"gps_lat_origin": "0", "gps_lon_origin": "0",
                                  "gps_lat_scale": "0", "gps_lon_scale": "800"}) is None
    assert gps.from_server_state({"gps_lat_origin": "x", "gps_lon_origin": "0",
                                  "gps_lat_scale": "1", "gps_lon_scale": "1"}) is None


def test_fmt_floors_like_hud():
    assert gps.fmt_gps(50.06, 74.99) == "50.0, 74.9"  # floor to 1 decimal, matching the GPS HUD


def test_describe_location():
    s = gps.describe_location(200000.0, 0.0, 350.0, ISLAND)
    assert s.startswith("GPS 50.0, 75.0") and "alt 350" in s and "world (200000, 0" in s
    # no constants -> raw world (+altitude) only, never a guessed GPS
    assert gps.describe_location(200000.0, 0.0, 350.0, None) == "world (200000, 0, alt 350)"
    # no z -> no altitude term
    assert gps.describe_location(1.0, 2.0) == "world (1, 2)"
