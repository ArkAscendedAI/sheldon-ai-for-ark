"""Admin action tools — commands that interact with the game server.

These tools require the game mod to be connected via WebSocket. In v0.1
(mock client mode), they simulate game interactions. When the real mod
is connected, they send commands over the WebSocket and await responses.

Available to admin and superadmin tiers only.
"""

from __future__ import annotations

import math
from typing import Any

from sheldon_bridge.tools.registry import tool


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


@tool(tier="admin", description="Spawn a dino near a player at a specified distance in front of them")
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
    }

    game_handler = ctx.get("game_handler") if ctx else None
    if game_handler:
        return await game_handler(command)

    return {
        "success": True,
        "mock": True,
        "message": f"[MOCK] Would set time to {hour:02d}:{minute:02d}",
    }


@tool(tier="admin", description="Give an item to a player by blueprint path")
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
    }

    game_handler = ctx.get("game_handler") if ctx else None
    if game_handler:
        return await game_handler(game_command)

    return {
        "success": True,
        "mock": True,
        "message": f"[MOCK] Would execute: {command}",
    }


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
    }

    game_handler = ctx.get("game_handler") if ctx else None
    if game_handler:
        return await game_handler(command)

    return {
        "success": True,
        "mock": True,
        "message": f"[MOCK] Would broadcast: {message}",
    }
