"""Admin action tools — commands that interact with the game server.

These tools require the game mod to be connected via WebSocket. In v0.1
(mock client mode), they simulate game interactions. When the real mod
is connected, they send commands over the WebSocket and await responses.

Available to admin and superadmin tiers only.
"""

from __future__ import annotations

import json
import math
import re
import time
from typing import Any

from sheldon_bridge import gps
from sheldon_bridge.tools.registry import tool


def _parse_loc(loc: str | None) -> dict | None:
    """Parse a raw ARK location string 'X=.. Y=.. Z=..' into {x,y,z} floats.
    Tolerant: missing axes dropped; returns None if nothing parses."""
    out: dict[str, float] = {}
    for axis in ("x", "y", "z"):
        m = re.search(rf"\b{axis}\s*=\s*(-?\d+(?:\.\d+)?)", loc or "", re.IGNORECASE)
        if m:
            out[axis] = float(m.group(1))
    return out or None


async def _persist_state(ctx: dict | None, *, vitals=None, pos=None, level=None) -> None:
    """Best-effort post-read side-effect: write a live getter result to telemetry.db
    player_state for the requesting survivor, so the assembler's always-on 'Live
    situational read' reflects it on later turns. Persists only what the LLM already
    fetched (zero added latency); never raises (telemetry is disposable + refreshable).
    No-op without an injected telemetry store + eos_id (e.g. mock client, or no game)."""
    if not ctx:
        return
    tel = ctx.get("telemetry")
    eos = ctx.get("eos_id")
    if tel is None or not eos or (vitals is None and pos is None and level is None):
        return
    try:
        await tel.upsert_player_state(eos, character_name=ctx.get("character_name") or "",
                                      vitals=vitals, pos=pos, level=level)
    except Exception:
        pass  # telemetry persistence must never break a getter


def _calculate_spawn_position(
    player_x: float, player_y: float, player_z: float,
    facing_yaw: float, distance_feet: float
) -> tuple[float, float, float]:
    """Calculate a world position N feet in front of the player.

    ARK uses Unreal Engine units: 1 foot ≈ 30.48 UE units.
    Yaw: 0°=North(+X), 90°=East(+Y), 180°=South, 270°=West.
    """
    ue_distance = distance_feet * 30.48
    yaw_rad = math.radians(facing_yaw)
    spawn_x = player_x + (ue_distance * math.cos(yaw_rad))
    spawn_y = player_y + (ue_distance * math.sin(yaw_rad))
    return spawn_x, spawn_y, player_z


@tool(tier="admin", description="Spawn a dino near a player at a specified distance in front of them",
      constraints={"max_level": 500})
async def spawn_dino_at_player(
    blueprint: str,
    level: int = 150,
    gender: str = "random",
    distance_feet: float = 30.0,
    force_tame: bool = False,
    ctx: dict | None = None,
) -> dict[str, Any]:
    """Spawn a dino in front of the requesting player.

    The position is calculated from the player's current location and facing
    direction. The bridge handles all coordinate math — the LLM just provides
    the parameters.

    Args:
        blueprint: Full blueprint path for the dino to spawn
        level: Dino level (1-500 for admin tier)
        gender: "male", "female", or "random"
        distance_feet: Distance in front of the player in feet (default 30)
        force_tame: Whether to force-tame the spawned dino
        ctx: Injected context with player info and game handler
    """
    if not ctx or "player" not in ctx:
        return {"success": False, "error": "No player context available"}

    player = ctx["player"]
    pos = player.position
    if not pos:
        return {"success": False, "error": "Player position not available"}

    # Calculate spawn position
    spawn_x, spawn_y, spawn_z = _calculate_spawn_position(
        pos.get("x", 0), pos.get("y", 0), pos.get("z", 0),
        player.facing_yaw, distance_feet,
    )

    # Build the game command
    command = {
        "action": "spawn_dino",
        "blueprint": blueprint,
        "x": spawn_x,
        "y": spawn_y,
        "z": spawn_z,
        "level": level,
        "gender": gender,
        "force_tame": force_tame,
    }

    # Send to game mod via WebSocket (or mock)
    game_handler = ctx.get("game_handler")
    if game_handler:
        result = await game_handler(command)
        return result

    # Mock response (no game mod connected)
    return {
        "success": True,
        "mock": True,
        "message": (
            f"[MOCK] Would spawn {blueprint.split('.')[-1]} at "
            f"({spawn_x:.0f}, {spawn_y:.0f}, {spawn_z:.0f}) "
            f"level {level}, gender={gender}, tamed={force_tame}"
        ),
        "spawn_position": {"x": spawn_x, "y": spawn_y, "z": spawn_z},
    }


@tool(tier="admin", description="Set the in-game time of day")
async def set_time(hour: int, minute: int = 0, ctx: dict | None = None) -> dict[str, Any]:
    """Change the in-game time of day.

    Args:
        hour: Hour in 24-hour format (0-23). 6=morning, 12=noon, 18=evening, 0=midnight.
        minute: Minute (0-59), defaults to 0.
        ctx: Injected context with game handler.
    """
    if hour < 0 or hour > 23 or minute < 0 or minute > 59:
        return {"success": False, "error": f"Invalid time: {hour}:{minute:02d}"}

    command = {
        "action": "console_command",
        "command": f"settimeofday {hour:02d}:{minute:02d}:00",
        "eos": (ctx or {}).get("eos_id"),   # V21-1: run on the requesting admin's client
    }

    game_handler = ctx.get("game_handler") if ctx else None
    if game_handler:
        return await game_handler(command)

    return {
        "success": True,
        "mock": True,
        "message": f"[MOCK] Would set time to {hour:02d}:{minute:02d}",
    }


@tool(tier="admin", description="Give an item to a player by blueprint path",
      constraints={"max_quantity": 1000, "min_quantity": 1})
async def give_item(
    player_name: str,
    blueprint: str,
    quantity: int = 1,
    quality: int = 0,
    ctx: dict | None = None,
) -> dict[str, Any]:
    """Give an item directly to a player's inventory.

    Args:
        player_name: The player's character name or EOS ID.
        blueprint: Full blueprint path for the item.
        quantity: Number of items to give (1-1000 for admin tier).
        quality: Quality level (0=primitive, 1=ramshackle, ... 5=ascendant).
        ctx: Injected context with game handler.
    """
    command = {
        "action": "console_command",
        "command": f'GiveItemToPlayer "{player_name}" "{blueprint}" {quantity} {quality} false',
        "eos": (ctx or {}).get("eos_id"),   # V21-1: run on the requesting admin's client
    }

    game_handler = ctx.get("game_handler") if ctx else None
    if game_handler:
        return await game_handler(command)

    return {
        "success": True,
        "mock": True,
        "message": f"[MOCK] Would give {quantity}x {blueprint.split('.')[-1]} to {player_name}",
    }


@tool(tier="admin", description="Execute a raw console command on the server")
async def execute_console_command(
    command: str, ctx: dict | None = None
) -> dict[str, Any]:
    """Execute any admin console command on the ARK server.

    Use this as a fallback when no specific tool exists for what you need.
    The command should be a valid ARK admin console command.

    Args:
        command: The full console command string (e.g., "destroywilddinos", "saveworld")
        ctx: Injected context with game handler.
    """
    game_command = {
        "action": "console_command",
        "command": command,
        # V21-1: the requester EOS is the per-player target — the cooked mod runs the command on
        # THIS admin's client (real cheat manager → no server crash, Gotcha #34). The handler
        # refuses an empty eos (it would broadcast to every client).
        "eos": (ctx or {}).get("eos_id"),
    }

    game_handler = ctx.get("game_handler") if ctx else None
    if game_handler:
        return await game_handler(game_command)

    return {
        "success": True,
        "mock": True,
        "message": f"[MOCK] Would execute: {command}",
    }


_VITAL_STATS = ("health", "stamina", "oxygen", "food", "water", "weight", "torpor",
                "melee", "speed", "fortitude", "crafting", "temperature")
_DB_VITALS = ("health", "food", "water", "stamina", "weight", "torpor", "oxygen")  # the telemetry-DB columns (oxygen since migration 003)


@tool(tier="player", description="Read the requesting survivor's full live stat block (health, stamina, oxygen, food, water, weight, torpor) from the game")
async def get_vitals(ctx: dict | None = None) -> dict[str, Any]:
    """Query the requesting player's live stat block from the cooked vitals getter:
    health, stamina, oxygen, food, water, weight, torpor (whichever the cooked mod
    reports — pre-V13 mods send only health). The V17 getter also reports a `<stat>_max`
    per bar stat (health/stamina/oxygen/food/weight/torpor — there's no water_max node),
    surfaced alongside current so the LLM can say "57.9/180, badly hurt". Use this when the
    player asks how they're doing or about a specific stat (e.g. oxygen while diving), or to
    ground a reaction in their actual state. Returns the stats present, or a mock when no
    server is connected.

    Args:
        ctx: Injected context with the game handler.
    """
    game_handler = ctx.get("game_handler") if ctx else None
    if game_handler:
        r = await game_handler({"action": "query", "request": "get_vitals", "reply": "vitals", "eos": (ctx or {}).get("eos_id")})
        if r.get("success"):
            data = r.get("data") or {}
            stats = {k: data[k] for k in _VITAL_STATS if data.get(k) is not None}
            # Pick up any `<stat>_max` the cooked getter sends (V17+) — whichever maxes are
            # present, no hardcoded set, so a future water_max node just works. Pre-V17 mods
            # send none → unchanged behaviour.
            maxes = {f"{k}_max": data[f"{k}_max"] for k in _VITAL_STATS
                     if data.get(f"{k}_max") is not None}
            # The multipliers/temperature have no telemetry-DB columns; return everything to the
            # LLM live, persist only the DB-backed current stats (max is static per level — not stored).
            db_vitals = {k: v for k, v in stats.items() if k in _DB_VITALS}
            await _persist_state(ctx, vitals=db_vitals or None)
            return {"success": True, **stats, **maxes}
        return r
    return {"success": True, "mock": True, "health": 100.0, "health_max": 100.0,
            "stamina": 100.0, "stamina_max": 100.0, "oxygen": 100.0, "oxygen_max": 100.0,
            "food": 100.0, "food_max": 100.0, "water": 100.0, "weight": 50.0,
            "weight_max": 150.0, "torpor": 0.0, "torpor_max": 100.0,
            "message": "[MOCK] Would read the survivor's stat block from the server."}


@tool(tier="player", description="Read the requesting survivor's current world position from the game (live)")
async def get_position(ctx: dict | None = None) -> dict[str, Any]:
    """Query the requesting player's live world location from the cooked position getter.
    Use this to know where the survivor is (e.g. to comment on their surroundings). Returns
    a raw ARK location string (e.g. "X=.. Y=.. Z=.."); the LLM/bridge can interpret it.

    Args:
        ctx: Injected context with the game handler.
    """
    game_handler = ctx.get("game_handler") if ctx else None
    if game_handler:
        r = await game_handler({"action": "query", "request": "get_position", "reply": "position", "eos": (ctx or {}).get("eos_id")})
        if r.get("success"):
            loc = (r.get("data") or {}).get("loc")
            await _persist_state(ctx, pos=_parse_loc(loc))
            return {"success": True, "loc": loc}
        return r
    return {"success": True, "mock": True, "loc": "X=0.0 Y=0.0 Z=0.0",
            "message": "[MOCK] Would read the survivor's world position from the server."}


def _format_day_time(dt):
    """Format ShooterGameState.DayTime -> 'HH:MM'. ⚠ V20-5: DayTime's exact range is ASSUMED to be
    HOURS-of-day (0..24) — the common ARK convention. The raw value is ALSO returned so the LLM/operator
    can sanity-check; if the in-game clock disagrees post-cook this is a cheap bridge-only fix (NO recook):
    if DayTime is a 0..1 fraction multiply by 24; if seconds divide by 3600. (Gotcha #33 — verify in-game.)"""
    if dt is None:
        return None
    try:
        dt = float(dt)
    except (TypeError, ValueError):
        return None
    h = int(dt) % 24
    m = int(round((dt - int(dt)) * 60)) % 60
    return "%02d:%02d" % (h, m)


@tool(tier="player", description="Read the current in-game DAY NUMBER and time of day from the game (live). "
      "Use to know how far into the playthrough it is or whether it's day vs night.")
async def get_world_time(ctx: dict | None = None) -> dict[str, Any]:
    """Query the live in-game world clock from the cooked get_world_time getter (V20-5).

    Returns the in-game `day` (integer day number — unambiguous + reliable) and `time_of_day`
    ('HH:MM', best-effort) plus the raw `day_time`. WORLD value (same for all players).
    """
    game_handler = ctx.get("game_handler") if ctx else None
    if game_handler:
        r = await game_handler({"action": "query", "request": "get_world_time",
                                "reply": "world_time", "eos": (ctx or {}).get("eos_id")})
        if r.get("success"):
            d = r.get("data") or {}
            day = d.get("day")
            day_time = d.get("day_time")
            return {"success": True, "day": day, "day_time": day_time,
                    "time_of_day": _format_day_time(day_time)}
        return r
    return {"success": True, "mock": True, "day": 1, "day_time": 10.0, "time_of_day": "10:00",
            "message": "[MOCK] Would read the in-game day + time of day from the server."}


# ARK item quality tiers, indexed by ItemQualityIndex (byte) the cooked getter reports.
_QUALITY_TIERS = {
    0: "Primitive", 1: "Ramshackle", 2: "Apprentice",
    3: "Journeyman", 4: "Mastercraft", 5: "Ascendant",
}


def _clean_item_class(raw: str) -> str:
    """'PrimalItemResource_Wood_C' or a full object path -> a friendlier short token.
    Stays class-based (stable) — the system prompt maps these to display names."""
    s = (raw or "").strip()
    if not s:
        return ""
    if "'" in s:                       # unwrap Class'/Script/...Foo_C' -> /Script/...Foo_C
        a, b = s.find("'"), s.rfind("'")
        if b > a:
            s = s[a + 1:b]
    if "." in s:                       # strip package path, keep final object name
        s = s.rsplit(".", 1)[-1]
    if s.endswith("_C"):
        s = s[:-2]
    return s


def _parse_inventory_items(items_str: str) -> list[dict[str, Any]]:
    """Parse the cooked inventory getter's compact wire payload into structured items.

    Wire format (kept Blueprint-cheap — built with Append nodes in the mod getter):
        record  := class '|' qty '|' qualityIdx '|' rating '|' durabilityPct '|' isSkin
        payload := record (';' record)*
    Tolerant by design: blank/trailing records are skipped, short records fill only the
    fields present, and unparseable numerics are dropped rather than raising — so a mod-side
    format tweak never breaks the bridge.
    """
    items: list[dict[str, Any]] = []
    for rec in (items_str or "").split(";"):
        rec = rec.strip()
        if not rec:
            continue
        fields = rec.split("|")

        def field(i: int) -> str | None:
            return fields[i].strip() if i < len(fields) and fields[i].strip() else None

        cls = _clean_item_class(field(0) or "")
        if not cls:
            continue
        item: dict[str, Any] = {"item": cls}
        if (qty := field(1)) is not None:
            try:
                item["quantity"] = int(float(qty))
            except ValueError:
                pass
        if (qidx := field(2)) is not None:
            try:
                qi = int(float(qidx))
                item["quality"] = _QUALITY_TIERS.get(qi, f"Tier{qi}")
            except ValueError:
                pass
        if (rating := field(3)) is not None:
            try:
                item["rating"] = round(float(rating), 2)
            except ValueError:
                pass
        if (durab := field(4)) is not None:
            try:
                item["durability_pct"] = round(float(durab), 1)
            except ValueError:
                pass
        if (skin := field(5)) is not None:
            item["is_skin"] = skin.lower() in ("true", "1", "yes")
        items.append(item)
    return items


@tool(tier="player", description="Read the requesting survivor's full inventory from the game (live)")
async def get_inventory(ctx: dict | None = None) -> dict[str, Any]:
    """Query the requesting player's live inventory from the cooked inventory getter.

    Returns a structured item list — each entry has the item class plus, when the getter
    reports them, quantity, quality tier, rating, durability %, and whether it's a skin.
    Use this to ground a reaction in what the survivor is actually carrying.

    Args:
        ctx: Injected context with the game handler.
    """
    game_handler = ctx.get("game_handler") if ctx else None
    if game_handler:
        r = await game_handler({"action": "query", "request": "get_inventory", "reply": "inventory", "eos": (ctx or {}).get("eos_id")})
        if r.get("success"):
            items = _parse_inventory_items((r.get("data") or {}).get("items", ""))
            return {"success": True, "items": items, "count": len(items)}
        return r
    return {"success": True, "mock": True, "count": 2,
            "items": [{"item": "Hatchet", "quantity": 1, "quality": "Primitive"},
                      {"item": "Wood", "quantity": 42}],
            "message": "[MOCK] Would read the survivor's inventory from the server."}


@tool(tier="player", description="Read what the requesting survivor currently has equipped (live)")
async def get_equipped(ctx: dict | None = None) -> dict[str, Any]:
    """Query the requesting player's currently-equipped items from the cooked equipped getter.
    Equipped entries are PrimalItems too, so they share the inventory wire format and parser.
    Use this to know what armor/tools/weapons the survivor is wearing or holding.

    Args:
        ctx: Injected context with the game handler.
    """
    game_handler = ctx.get("game_handler") if ctx else None
    if game_handler:
        r = await game_handler({"action": "query", "request": "get_equipped", "reply": "equipped", "eos": (ctx or {}).get("eos_id")})
        if r.get("success"):
            items = _parse_inventory_items((r.get("data") or {}).get("items", ""))
            return {"success": True, "equipped": items, "count": len(items)}
        return r
    return {"success": True, "mock": True, "count": 1,
            "equipped": [{"item": "PrimalItemArmor_ClothShirt", "quality": "Primitive",
                          "durability_pct": 100.0}],
            "message": "[MOCK] Would read the survivor's equipped items from the server."}


@tool(tier="player", description="Read the requesting survivor's tribe name from the game (live)")
async def get_tribe(ctx: dict | None = None) -> dict[str, Any]:
    """Query the requesting player's tribe from the cooked tribe getter. Returns the tribe name
    (or null if the survivor isn't in a tribe / is solo).

    Args:
        ctx: Injected context with the game handler.
    """
    game_handler = ctx.get("game_handler") if ctx else None
    if game_handler:
        r = await game_handler({"action": "query", "request": "get_tribe", "reply": "tribe", "eos": (ctx or {}).get("eos_id")})
        if r.get("success"):
            data = r.get("data") or {}
            out = {"success": True, "tribe": (data.get("name") or None)}
            # BUFFS-07: surface the survivor's NUMERIC team id when the getter reports it (a future
            # cook). It lets get_recent_dino_events filter dino events by exact team id instead of
            # tribe-name string equality (collision-free). Absent on the cooked V18 getter → omitted,
            # and the consumer falls back to cluster-scoped tribe-name matching.
            if data.get("team") is not None:
                out["team"] = str(data.get("team")).strip()
            return out
        return r
    return {"success": True, "mock": True, "tribe": "Mock Tribe",
            "message": "[MOCK] Would read the survivor's tribe from the server."}


@tool(tier="player", description="Read the requesting survivor's character level from the game (live)")
async def get_progression(ctx: dict | None = None) -> dict[str, Any]:
    """Query the requesting player's character level from the cooked progression getter.
    Use this to gauge how far along the survivor is.

    Args:
        ctx: Injected context with the game handler.
    """
    game_handler = ctx.get("game_handler") if ctx else None
    if game_handler:
        r = await game_handler({"action": "query", "request": "get_progression", "reply": "progression", "eos": (ctx or {}).get("eos_id")})
        if r.get("success"):
            lvl = (r.get("data") or {}).get("level")
            try:
                lvl = int(lvl)
            except (TypeError, ValueError):
                pass
            await _persist_state(ctx, level=lvl if isinstance(lvl, int) else None)
            return {"success": True, "level": lvl}
        return r
    return {"success": True, "mock": True, "level": 50,
            "message": "[MOCK] Would read the survivor's character level from the server."}


# Snapshot profiles — which cooked getters to compose. The bridge picks per call to keep
# game-thread cost down (a "weight" lever); all values are cheap to retune (no cook).
_SNAPSHOT_PROFILES = {
    "lite": ["vitals", "position"],
    "full": ["vitals", "position", "inventory", "equipped", "tribe", "progression"],
    "inventory_only": ["inventory"],
}


@tool(tier="player",
      description="Read a combined live snapshot of the requesting survivor (profile: lite|full|inventory_only)")
async def get_snapshot(profile: str = "lite", ctx: dict | None = None) -> dict[str, Any]:
    """Compose a survivor snapshot from the cooked getters. 'lite' = vitals+position (cheap),
    'full' = +inventory+equipped+tribe+progression, 'inventory_only' = inventory. Getters run
    sequentially — the mod serializes them on the game thread and they share the QueryAccum
    accumulator, so concurrent dispatch is deliberately avoided.

    Args:
        profile: lite | full | inventory_only (defaults to lite on an unknown value).
        ctx: Injected context with the game handler.
    """
    fns = {"vitals": get_vitals, "position": get_position, "inventory": get_inventory,
           "equipped": get_equipped, "tribe": get_tribe, "progression": get_progression}
    profile = profile if profile in _SNAPSHOT_PROFILES else "lite"
    snap: dict[str, Any] = {}
    for name in _SNAPSHOT_PROFILES[profile]:
        snap[name] = await fns[name](ctx=ctx)
    return {"success": True, "profile": profile, "snapshot": snap}


def _parse_delimited(payload: str, fields: list[str]) -> list[dict[str, Any]]:
    """Parse a cooked world-read payload `a|b|c;a|b|c;...` into dicts keyed by `fields`.
    Tolerant: blank records skipped, short records fill only present fields. Shared by the
    player/dino/structure enum getters (they differ only in `fields`)."""
    out: list[dict[str, Any]] = []
    for rec in (payload or "").split(";"):
        rec = rec.strip()
        if not rec:
            continue
        vals = rec.split("|")
        row = {f: vals[i].strip() for i, f in enumerate(fields)
               if i < len(vals) and vals[i].strip()}
        if row:
            out.append(row)
    return out


@tool(tier="player", description="List the survivors currently online (name + id)")
async def get_players(ctx: dict | None = None) -> dict[str, Any]:
    """Query the online-player roster from the cooked players getter (server enumerates all
    ShooterPlayerControllers). Returns name + EOS id per connected survivor.

    Args:
        ctx: Injected context with the game handler.
    """
    game_handler = ctx.get("game_handler") if ctx else None
    if game_handler:
        r = await game_handler({"action": "query", "request": "get_players", "reply": "players", "eos": (ctx or {}).get("eos_id")})
        if r.get("success"):
            players = _parse_delimited((r.get("data") or {}).get("list", ""), ["name", "eos"])
            return {"success": True, "players": players, "count": len(players)}
        return r
    return {"success": True, "mock": True, "count": 1,
            "players": [{"name": "Mock Survivor", "eos": "0002abc"}],
            "message": "[MOCK] Would list online players from the server."}


@tool(tier="player",
      description="List the requesting survivor's TRIBE/personal dinos (species, tamed, location, level, hp) — bounded, safe")
async def get_dinos(ctx: dict | None = None) -> dict[str, Any]:
    """Query the requester's TRIBE dinos from the cooked getter (V13: enumerates via
    GetAllActorsOfClassInTribe, bounded to the tribe — NOT the whole map, so it no longer
    freezes the server). Returns species + tamed flag + raw location + level + current hp per
    dino. For map-wide questions ("highest-level X anywhere"), this tool does NOT cover them —
    that needs the separate bounded world-scan getter (C10 sphere+cache design, not yet built).

    Args:
        ctx: Injected context with the game handler.
    """
    game_handler = ctx.get("game_handler") if ctx else None
    if game_handler:
        r = await game_handler({"action": "query", "request": "get_dinos", "reply": "dinos", "eos": (ctx or {}).get("eos_id")})
        if r.get("success"):
            dinos = _parse_delimited((r.get("data") or {}).get("list", ""),
                                     ["species", "tamed", "loc", "level", "hp"])
            for d in dinos:
                if "tamed" in d:
                    d["tamed"] = d["tamed"].lower() in ("true", "1", "yes")
                if "level" in d:
                    try:
                        d["level"] = int(float(d["level"]))
                    except ValueError:
                        pass  # leave the raw string if the getter sent something odd
                if "hp" in d:
                    try:
                        d["hp"] = round(float(d["hp"]), 1)
                    except ValueError:
                        pass
            return {"success": True, "dinos": dinos, "count": len(dinos)}
        return r
    return {"success": True, "mock": True, "count": 1,
            "dinos": [{"species": "Raptor", "tamed": True, "loc": "X=1 Y=2 Z=3", "level": 150, "hp": 1850.0}],
            "message": "[MOCK] Would list dinos from the server."}


@tool(tier="player",
      description="Search the MAP-WIDE dino census (the continuous background world scan of all tamed + wild dinos) by species and/or minimum level, highest level first. Answers map-wide questions get_dinos can't — 'highest-level Rex anywhere', 'where are the gigas', 'how many wild Argents'. Each result has location, level, gender, tribe, and a full stat block. May be empty or stale if the census hasn't swept recently — check census_age_seconds.")
async def find_dinos(species: str = "", min_level: int = 0, limit: int = 20,
                     ctx: dict | None = None) -> dict[str, Any]:
    """Query the persistent dino_index (populated by the continuous octree census), NOT a
    live getter — so it spans the whole map, including stasised/far dinos. species is a
    case-insensitive substring match.

    Args:
        species: filter by species name substring (e.g. "Rex", "Giga"); empty = any.
        min_level: only dinos at or above this level; 0 = any.
        limit: max rows returned, highest level first.
        ctx: injected context with the telemetry store.
    """
    telemetry = ctx.get("telemetry") if ctx else None
    if telemetry is None:
        return {"success": True, "mock": True, "count": 1,
                "dinos": [{"species": "Giganotosaurus", "name": "", "level": 295, "gender": "F",
                           "tribe": "0", "loc": "X=12000 Y=-8000", "stats": {"hp_m": 80000.0}}],
                "message": "[MOCK] Would search the map-wide dino census."}
    rows = await telemetry.search_dinos(species=species or None, min_level=min_level or 0, limit=limit)
    fresh = await telemetry.newest_capture()
    age = round(time.time() - fresh) if fresh else None
    mapgps = gps.from_server_state(await telemetry.get_server_state())  # None until the mod publishes constants
    dinos = []
    for r in rows:
        try:
            stats = json.loads(r["base_stats"] or "{}")
        except (ValueError, TypeError):
            stats = {}
        d = {
            "species": r["species"], "name": r["name"], "level": r["level"],
            "gender": r["gender"], "tribe": r["tribe"], "stats": stats,
            "loc": None,
        }
        if r["x"] is not None and r["y"] is not None:
            d["loc"] = f"X={r['x']:.0f} Y={r['y']:.0f}" + (f" Z={r['z']:.0f}" if r["z"] is not None else "")
            if mapgps is not None:
                d["gps"] = gps.fmt_gps(*mapgps.convert(r["x"], r["y"]))
        dinos.append(d)
    return {"success": True, "dinos": dinos, "count": len(dinos), "census_age_seconds": age}


@tool(tier="player",
      description="Read the requesting survivor's active buffs/debuffs (poisoned, well-fed, mate-boost, cryo-sick, etc.) — live")
async def get_buffs(ctx: dict | None = None) -> dict[str, Any]:
    """Query the requester's active buffs from the cooked getter (V13: GetAllBuffs on the
    requester character → each buff's display name). Per-survivor read — cheap + safe (no
    world scan). Returns a list of buff display names; use it to ground a reaction in the
    survivor's current status effects (well-fed, hypothermia, poisoned, mate-boosted, …).

    Args:
        ctx: Injected context with the game handler.
    """
    game_handler = ctx.get("game_handler") if ctx else None
    if game_handler:
        r = await game_handler({"action": "query", "request": "get_buffs", "reply": "buffs", "eos": (ctx or {}).get("eos_id")})
        if r.get("success"):
            buffs = [b["name"] for b in
                     _parse_delimited((r.get("data") or {}).get("list", ""), ["name"]) if "name" in b]
            return {"success": True, "buffs": buffs, "count": len(buffs)}
        return r
    return {"success": True, "mock": True, "count": 2, "buffs": ["Well Fed", "Mate Boosted"],
            "message": "[MOCK] Would read the survivor's active buffs from the server."}


# EngramGroup byte -> label (from the live PrimalEngramEntry.EngramGroup enum). 8 = Tek.
_ENGRAM_GROUPS = {"2": "prime", "4": "scorched_earth", "8": "tek"}


def _safe_int(s):
    try:
        return int(str(s).strip())
    except Exception:
        return None


@tool(tier="player",
      description="Read the requesting survivor's engrams — which they've LEARNED, which they have NOT, available engram points, and (V18) per-engram required level/points + Tek flag (live)")
async def get_engrams(ctx: dict | None = None) -> dict[str, Any]:
    """Query the survivor's FULL engram standing from the cooked getter: every engram in the
    catalog (incl. mod engrams) tagged learned/unlearned, plus available (free) engram points, plus
    (V18 cook) per-engram REQUIREMENTS — required character level, required engram points, and the
    EngramGroup (Tek flag). The mod streams `list` as `Name|learned|reqLevel|reqPoints|groupByte`
    entries joined by ';' (pre-V18 cook: just `Name|learned`); we split into learned[] + unlearned[]
    and a requirements{} lookup. Per-survivor read — cheap + safe (the catalog is a bounded in-memory
    array, not a world scan). Use for "what engrams have I learned / what can I still learn / what
    level + points do I need to unlock X / how many engram points do I have".

    Args:
        ctx: Injected context with the game handler.
    """
    game_handler = ctx.get("game_handler") if ctx else None
    if game_handler:
        r = await game_handler({"action": "query", "request": "get_engrams", "reply": "engrams", "eos": (ctx or {}).get("eos_id")})
        if r.get("success"):
            data = r.get("data") or {}
            learned, unlearned, requirements = [], [], {}
            from sheldon_bridge.engram_catalog import clean_engram_name
            for tok in (data.get("list") or "").split(";"):
                tok = tok.strip()
                if not tok:
                    continue
                # V18: Name|learned|reqLevel|reqPoints|groupByte. rsplit from the RIGHT so a name
                # containing '|' survives; pre-V18 cook emits Name|learned -> requirements omitted.
                parts = tok.rsplit("|", 4)
                name = parts[0]
                flag = parts[1] if len(parts) >= 2 else ""
                clean = clean_engram_name(name or tok)  # mod emits the class key; map -> display name
                (learned if flag.strip().lower() in ("1", "true") else unlearned).append(clean)
                if len(parts) >= 5:
                    grp = parts[4].strip()
                    requirements[clean] = {"required_level": _safe_int(parts[2]),
                                           "required_points": _safe_int(parts[3]),
                                           "tek": grp == "8",
                                           "group": _ENGRAM_GROUPS.get(grp, "base")}
            return {"success": True, "free_engram_points": data.get("free"),
                    "learned": learned, "learned_count": len(learned),
                    "unlearned": unlearned, "unlearned_count": len(unlearned),
                    "requirements": requirements}
        return r
    return {"success": True, "mock": True, "free_engram_points": 12,
            "learned": ["Campfire", "Spear"], "learned_count": 2,
            "unlearned": ["Forge", "Smithy"], "unlearned_count": 2,
            "message": "[MOCK] Would read the survivor's learned + unlearned engrams from the server."}


@tool(tier="player", description="Read where the requesting survivor is looking — eye point + aim (live)")
async def get_look(ctx: dict | None = None) -> dict[str, Any]:
    """Query the survivor's server-side eye view point + aim rotation from the cooked look getter.
    Returns raw `eye` (location) + `aim` (rotation) strings; the bridge does the view-cone math
    against the dino/player lists to infer what they're looking at.

    Args:
        ctx: Injected context with the game handler.
    """
    game_handler = ctx.get("game_handler") if ctx else None
    if game_handler:
        r = await game_handler({"action": "query", "request": "get_look", "reply": "look", "eos": (ctx or {}).get("eos_id")})
        if r.get("success"):
            d = r.get("data") or {}
            return {"success": True, "eye": d.get("eye"), "aim": d.get("aim")}
        return r
    return {"success": True, "mock": True, "eye": "X=0 Y=0 Z=0", "aim": "P=0 Y=90 R=0",
            "message": "[MOCK] Would read the survivor's eye view point + aim from the server."}


@tool(tier="player",
      description="Recall recent game events (death, damage) the survivor experienced — answers 'what just killed me?'")
async def get_recent_events(limit: int = 5, ctx: dict | None = None) -> dict[str, Any]:
    """Return the most recent game events pushed by the mod's universal event buff, newest
    first. These are unsolicited bursts (the survivor died, took notable damage) the bridge
    recorded as they happened. Use this to react to or recall a recent death/damage (e.g.
    "what killed me?", "why am I hurt?"). Each event has 'event' (death_event / damage_event),
    'at' (epoch seconds), 'data' (raw fields the mod sent — death: victim/killer/cause; damage:
    amount/cause), and 'summary' (a ready one-line gloss, e.g. "Killed by Rex (combat)"). Empty
    when nothing has happened recently.

    Args:
        limit: Max events to return, newest first (default 5).
        ctx: Injected context (carries the bridge's recent-events ring).
    """
    raw = list(ctx.get("recent_events") or []) if ctx else []
    events = list(reversed(raw))[:max(1, limit)]
    return {"success": True, "events": events, "count": len(events)}


@tool(tier="player",
      description="Recall recent dino tame/death events for the survivor's OWN tribe — answers 'did any of my dinos die?' / 'what did I tame?'")
async def get_recent_dino_events(limit: int = 8, ctx: dict | None = None) -> dict[str, Any]:
    """Return recent dino events (tames + deaths) belonging to the asking survivor's TRIBE,
    newest first — privacy-scoped, so it never shows another tribe's dinos. Each has 'event'
    (dino_tamed / dino_death), 'at', 'summary' (e.g. "Your tribe's Rex was killed by a Giga"),
    'dino', and 'killer'. Use for "did any of my dinos die?", "what have I tamed lately?". The
    dino events are team/tribe-scoped (no per-player EOS) — a tame names the tribe, a death names
    the team which is mapped to a tribe via the most recent tame for that team. Empty if nothing
    recent or the survivor's tribe can't be resolved.

    Args:
        limit: Max events to return, newest first (default 8).
        ctx: Injected context (the dino-event ring, team->tribe map, game handler).
    """
    if not ctx:
        return {"success": True, "events": [], "count": 0}
    raw = list(ctx.get("dino_events") or [])
    # BUFFS-07: team_tribe is the asker's OWN-CLUSTER team->tribe map (server.py scopes it per
    # cluster_id before injecting), so a same-named tribe on a different cluster can't bleed in
    # through the death->team->tribe resolution.
    team_tribe = ctx.get("team_tribe") or {}
    mine = await get_tribe(ctx)
    my_tribe = mine.get("tribe")
    my_team = mine.get("team")  # numeric team id, only when the getter reports it (future cook)
    if not my_tribe and not my_team:
        return {"success": True, "events": [], "count": 0,
                "note": "couldn't resolve your tribe; can't scope dino events to you yet"}
    out = []
    for e in reversed(raw):
        d = e.get("data", {})
        ev_team = str(d.get("team", "")).strip()
        if my_team:
            # Exact NUMERIC team-id match — collision-free, no tribe-name string equality. A tame
            # carries team; a death carries team. (Falls through to name only if a frame lacks team.)
            match = bool(ev_team) and ev_team == my_team
            if not match and not ev_team:  # frame without a team id → last-resort name match
                match = bool(d.get("tribe")) and d.get("tribe") == my_tribe
        else:
            # Back-compat fallback (cooked V18 getter reports no team): match by tribe NAME, resolving
            # a death's team via the asker's OWN-CLUSTER team->tribe map only.
            ev_tribe = d.get("tribe") or team_tribe.get(ev_team)
            match = bool(ev_tribe) and ev_tribe == my_tribe
        if match:
            out.append({"event": e.get("event"), "at": e.get("at"),
                        "summary": e.get("summary"), "dino": d.get("dino"), "killer": d.get("killer")})
        if len(out) >= max(1, limit):
            break
    return {"success": True, "events": out, "count": len(out)}


@tool(tier="player",
      description="Drop a labeled marker on the survivor's map at a world location (a base, a resource, a rendezvous). Returns a pin id for later removal.")
async def add_pin(label: str, x: float, y: float, z: float, ctx: dict | None = None) -> dict[str, Any]:
    """Add a labeled minimap marker on the REQUESTING survivor's map at a world-coordinate
    location (the same X/Y/Z space get_position and get_dinos report). Use coordinates you got
    from a getter (e.g. pin the survivor's own position, or a dino's location) — don't invent
    them. Returns the pin id so you can remove_pin it later.

    Args:
        label: Short marker label shown on the map (e.g. "Metal node", "Home").
        x, y, z: World location in cm (as reported by get_position / get_dinos).
        ctx: Injected context with the game handler.
    """
    game_handler = ctx.get("game_handler") if ctx else None
    if game_handler:
        return await game_handler({"action": "map_pin", "op": "add", "label": label,
                                   "x": x, "y": y, "z": z, "eos": ctx.get("eos_id")})
    return {"success": True, "mock": True, "pin_id": "sheldon_p1", "label": label,
            "message": f"[MOCK] Would pin '{label}' at X={x} Y={y} Z={z}."}


@tool(tier="player", description="Remove a Sheldon-placed map marker by its pin id")
async def remove_pin(pin_id: str, ctx: dict | None = None) -> dict[str, Any]:
    """Remove a single Sheldon-placed minimap marker by the id returned from add_pin.

    Args:
        pin_id: The id returned by add_pin (e.g. "sheldon_p1").
        ctx: Injected context with the game handler.
    """
    game_handler = ctx.get("game_handler") if ctx else None
    if game_handler:
        return await game_handler({"action": "map_pin", "op": "remove", "id": pin_id,
                                   "eos": ctx.get("eos_id")})
    return {"success": True, "mock": True, "removed": pin_id,
            "message": f"[MOCK] Would remove pin {pin_id}."}


@tool(tier="player", description="Clear all Sheldon-placed map markers for the requesting survivor")
async def clear_pins(ctx: dict | None = None) -> dict[str, Any]:
    """Remove every minimap marker Sheldon has placed for the requesting survivor.

    Args:
        ctx: Injected context with the game handler.
    """
    game_handler = ctx.get("game_handler") if ctx else None
    if game_handler:
        return await game_handler({"action": "map_pin", "op": "clear", "eos": ctx.get("eos_id")})
    return {"success": True, "mock": True, "cleared": 0,
            "message": "[MOCK] Would clear all Sheldon pins."}


@tool(tier="admin", description="Send a broadcast message to all online players")
async def broadcast(message: str, ctx: dict | None = None) -> dict[str, Any]:
    """Send a server-wide broadcast message visible to all players.

    Args:
        message: The message text to broadcast.
        ctx: Injected context with game handler.
    """
    command = {
        "action": "console_command",
        "command": f'broadcast {message}',
        "eos": (ctx or {}).get("eos_id"),   # V21-1: run on the requesting admin's client (broadcast is server-wide regardless)
    }

    game_handler = ctx.get("game_handler") if ctx else None
    if game_handler:
        return await game_handler(command)

    return {
        "success": True,
        "mock": True,
        "message": f"[MOCK] Would broadcast: {message}",
    }
