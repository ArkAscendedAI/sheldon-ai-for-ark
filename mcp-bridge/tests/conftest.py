"""Shared test fixtures for the Sheldon Bridge test suite."""

import sys
from pathlib import Path

import pytest

# Ensure the package is importable
sys.path.insert(0, str(Path(__file__).parent.parent))

from sheldon_bridge.auth import PlayerContext, TokenAuthenticator, RateLimiter
from sheldon_bridge.session import Session, SessionManager
from sheldon_bridge.tools.registry import (
    ToolDefinition,
    ToolRegistry,
    tool,
    _registered_tools,
)


@pytest.fixture(autouse=True)
def clear_tool_registry():
    """Clear the global tool registry between tests."""
    _registered_tools.clear()
    yield
    _registered_tools.clear()


@pytest.fixture
def shared_secret():
    return "test-secret-that-is-long-enough-for-validation-purposes"


@pytest.fixture
def authenticator(shared_secret):
    return TokenAuthenticator(shared_secret)


@pytest.fixture
def rate_limiter():
    return RateLimiter()


@pytest.fixture
def player_context():
    return PlayerContext(
        player_id="EOS_PLAYER_001",
        display_name="SurvivorBob",
        tier="player",
        tribe_id="tribe_42",
        position={"x": 1000.0, "y": 2000.0, "z": 100.0},
        facing_yaw=90.0,
    )


@pytest.fixture
def admin_context():
    return PlayerContext(
        player_id="EOS_ADMIN_001",
        display_name="AdminTracy",
        tier="admin",
        tribe_id="tribe_01",
        position={"x": 5000.0, "y": 6000.0, "z": 200.0},
        facing_yaw=180.0,
    )


@pytest.fixture
def superadmin_context():
    return PlayerContext(
        player_id="EOS_SUPERADMIN_001",
        display_name="SuperAdminRoot",
        tier="superadmin",
        tribe_id="tribe_01",
    )


def _register_test_tools():
    """Register a comprehensive set of test tools across all tiers."""

    @tool(tier="player", description="Look up dino information")
    def lookup_dino(query: str) -> dict:
        return {"name": "Rex", "diet": "Carnivore"}

    @tool(tier="player", description="Calculate taming requirements")
    def calculate_taming(species: str, level: int = 150) -> dict:
        return {"species": species, "food": "Raw Mutton", "time": "30 min"}

    @tool(tier="player", description="Get server status")
    def get_server_status() -> dict:
        return {"players": 5, "uptime": "12h"}

    @tool(tier="player", description="Get my tames")
    def get_my_tames(player_id: str) -> list:
        return [{"name": "Rex", "level": 200}]

    @tool(tier="player", description="Get current time of day")
    def get_time_of_day() -> str:
        return "14:30"

    @tool(tier="admin", description="Spawn a dino at coordinates")
    def spawn_dino(blueprint: str, level: int, x: float, y: float, z: float) -> dict:
        return {"spawned": True, "blueprint": blueprint, "level": level}

    @tool(tier="admin", description="Give an item to a player")
    def give_item(player_id: str, blueprint: str, quantity: int = 1) -> dict:
        return {"given": True, "qty": quantity}

    @tool(tier="admin", description="Teleport a player")
    def teleport_player(player_id: str, x: float, y: float, z: float) -> dict:
        return {"teleported": True}

    @tool(tier="admin", description="Kick a player from the server")
    def kick_player(player_id: str, reason: str = "") -> dict:
        return {"kicked": True}

    @tool(tier="admin", description="Get all players")
    def get_all_players() -> list:
        return [{"name": "SurvivorBob", "level": 50}]

    @tool(tier="admin", description="Census of wild dinos")
    def census_wild(species: str = "") -> dict:
        return {"count": 47, "species": species}

    @tool(tier="admin", description="Execute a console command")
    def execute_console_command(command: str) -> dict:
        return {"executed": True, "command": command}

    @tool(tier="admin", description="Broadcast a message")
    def broadcast(message: str) -> dict:
        return {"broadcast": True}

    @tool(tier="admin", description="Destroy wild dinos")
    def destroy_wild_dinos(species: str = "") -> dict:
        return {"destroyed": True}

    @tool(tier="admin", description="Set time of day")
    def set_time(hour: int) -> dict:
        return {"set": True, "hour": hour}

    @tool(tier="superadmin", description="Shut down the server")
    def shutdown_server(reason: str = "") -> dict:
        return {"shutdown": True}

    @tool(tier="superadmin", description="Modify server config")
    def modify_server_config(key: str, value: str) -> dict:
        return {"modified": True}

    @tool(tier="superadmin", description="Manage permissions")
    def manage_permissions(player_id: str, new_tier: str) -> dict:
        return {"updated": True}

    return {
        "lookup_dino": lookup_dino,
        "calculate_taming": calculate_taming,
        "get_server_status": get_server_status,
        "get_my_tames": get_my_tames,
        "get_time_of_day": get_time_of_day,
        "spawn_dino": spawn_dino,
        "give_item": give_item,
        "teleport_player": teleport_player,
        "kick_player": kick_player,
        "get_all_players": get_all_players,
        "census_wild": census_wild,
        "execute_console_command": execute_console_command,
        "broadcast": broadcast,
        "destroy_wild_dinos": destroy_wild_dinos,
        "set_time": set_time,
        "shutdown_server": shutdown_server,
        "modify_server_config": modify_server_config,
        "manage_permissions": manage_permissions,
    }


@pytest.fixture
def test_tools():
    return _register_test_tools()


@pytest.fixture
def registry(test_tools):
    """A fully populated tool registry with test tools."""
    reg = ToolRegistry()
    reg.discover()
    return reg


@pytest.fixture
def constrained_registry(test_tools):
    """A registry with parameter constraints for admin tier."""
    tier_config = {
        "player": {
            "tools": ["lookup_*", "calculate_*", "get_my_*", "get_server_*", "get_time_*"],
        },
        "admin": {
            "inherits": "player",
            "tools": [
                "spawn_*", "give_*", "teleport_*", "kick_*",
                "get_all_*", "census_*", "execute_*", "broadcast",
                "destroy_*", "set_*",
            ],
            "constraints": {
                "spawn_dino": {"max_level": 500},
                "give_item": {"max_quantity": 1000},
            },
        },
        "superadmin": {
            "inherits": "admin",
            "tools": ["*"],
        },
    }
    reg = ToolRegistry(tier_config=tier_config)
    reg.discover()
    return reg
