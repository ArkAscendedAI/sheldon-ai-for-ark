"""World-coordinate -> in-game GPS (latitude / longitude) conversion.

ARK's GPS is a per-map affine map of the world X/Y plane (centimeters):

    latitude  = (world_y + abs(lat_origin)) / (lat_scale * 10)
    longitude = (world_x + abs(lon_origin)) / (lon_scale * 10)

LATITUDE comes from world **Y**, LONGITUDE from world **X** (not swapped). The four
constants live on the map's PrimalWorldSettings (LatitudeOrigin / LongitudeOrigin /
LatitudeScale / LongitudeScale). The mod reads them off the LIVE world and publishes
them to telemetry server_state, so conversion is map-agnostic — Ragnarok, The Island,
a future Valguero all just work from their own constants. Until the mod publishes them
for a map we have NO constants and fall back to raw coordinates: we never show a guessed
GPS (Island defaults on a non-Island map would be silently wrong).

Formula + field names confirmed 2026-06-22 against the ASA ServerAPI SDK
(`FVectorToCoords` in ArkApiUtils.h) + the Purlovia data-miner + working ASA mods.
The in-game GPS HUD floors to one decimal — we match it. POST-COOK: validate against
the live in-game GPS readout at a known off-center point before trusting it.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

# server_state keys the mod publishes for the active map (V17 cook item 4). These are
# INTERNAL constants, never surfaced to the LLM (the assembler filters the `gps_` prefix).
_KEYS = ("gps_lat_origin", "gps_lon_origin", "gps_lat_scale", "gps_lon_scale")


@dataclass(frozen=True)
class MapGPS:
    """The four PrimalWorldSettings GPS constants for one map."""

    lat_origin: float
    lon_origin: float
    lat_scale: float
    lon_scale: float

    def convert(self, x: float, y: float) -> tuple[float, float]:
        """world (x, y) in cm -> (latitude, longitude). Lat from Y, lon from X."""
        lat = (y + abs(self.lat_origin)) / (self.lat_scale * 10.0)
        lon = (x + abs(self.lon_origin)) / (self.lon_scale * 10.0)
        return lat, lon


def from_server_state(state: dict | None) -> MapGPS | None:
    """Build a MapGPS from telemetry server_state (the mod publishes the 4 constants there).

    Returns None — so callers fall back to raw coords — if the constants are absent (no
    cook yet), unparseable, or either scale is 0 (would divide by zero). NEVER guesses."""
    if not state:
        return None
    try:
        lat_o, lon_o, lat_s, lon_s = (float(state[k]) for k in _KEYS)
    except (KeyError, TypeError, ValueError):
        return None
    if lat_s == 0.0 or lon_s == 0.0:
        return None
    return MapGPS(lat_o, lon_o, lat_s, lon_s)


def fmt_gps(lat: float, lon: float) -> str:
    """Format lat/lon the way the in-game GPS HUD does: floored to one decimal."""
    return f"{math.floor(lat * 10) / 10:.1f}, {math.floor(lon * 10) / 10:.1f}"


def describe_location(x: float, y: float, z: float | None = None, mapgps: MapGPS | None = None) -> str:
    """Human-readable location: GPS lat/lon when we have the map constants, always the raw
    world coords (+ altitude from z when present) so the value is never lost."""
    world = f"world ({x:.0f}, {y:.0f}" + (f", alt {z:.0f}" if z is not None else "") + ")"
    if mapgps is not None:
        lat, lon = mapgps.convert(x, y)
        return f"GPS {fmt_gps(lat, lon)} — {world}"
    return world
