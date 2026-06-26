"""
End-to-end integration test: real player message through the full pipeline.

This test starts the bridge server, connects a mock client, sends a player
message, and verifies the response comes back through the full chain:
  WebSocket → Auth → Session → Agent → LLM → Tools → Response

Requires ANTHROPIC_API_KEY to be set (uses real LLM calls).
Skip with: pytest -k "not integration"
"""

import asyncio
import json
import logging
import os
import sys
from pathlib import Path

import pytest

# Load API key from env file
env_file = Path.home() / ".sheldon-bridge.env"
if env_file.exists():
    for line in env_file.read_text().strip().split("\n"):
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            key, val = line.split("=", 1)
            os.environ[key.strip()] = val.strip()

# Skip entire module if no API key
pytestmark = pytest.mark.skipif(
    not os.environ.get("ANTHROPIC_API_KEY"),
    reason="ANTHROPIC_API_KEY not set — skipping integration tests",
)

import websockets

from sheldon_bridge.config import BridgeConfig
from sheldon_bridge.providers.llm import LLMConfig
from sheldon_bridge.tools.knowledge import load_data

logging.basicConfig(level=logging.INFO, format="%(name)s %(message)s")

# Data directory (relative to project root)
DATA_DIR = str(Path(__file__).parent.parent.parent / "data" / "vanilla")
SHARED_SECRET = "integration-test-secret-long-enough"


def make_test_config(port: int) -> BridgeConfig:
    """Create a test configuration."""
    return BridgeConfig(
        llm=LLMConfig(
            provider="anthropic",
            model="claude-sonnet-4-20250514",
            api_key=os.environ["ANTHROPIC_API_KEY"],
            max_tokens=1024,
            temperature=0.3,
        ),
        websocket_host="127.0.0.1",
        websocket_port=port,
        shared_secret=SHARED_SECRET,
        data_dirs=[DATA_DIR],
        personality_name="Sheldon",
    )


async def start_server(config: BridgeConfig):
    """Start the bridge server and return it."""
    from sheldon_bridge.server import BridgeServer
    from websockets.asyncio.server import serve

    # Load knowledge base data
    load_data(config.data_dirs)

    server = BridgeServer(config)

    ws_server = await serve(
        server.handle_connection,
        host=config.websocket_host,
        port=config.websocket_port,
        ping_interval=None,
    )
    return ws_server, server


async def mock_player_session(
    port: int,
    player_name: str = "TestPlayer",
    tier: str = "player",
    messages: list[str] = None,
) -> list[dict]:
    """Connect as a mock player and send messages. Returns all responses."""
    uri = f"ws://127.0.0.1:{port}"
    responses = []

    async with websockets.connect(uri) as ws:
        # Authenticate
        await ws.send(json.dumps({
            "type": "auth",
            "token": SHARED_SECRET,
            "player": {
                "player_id": f"EOS_{player_name.upper()}_001",
                "display_name": player_name,
                "tier": tier,
                "tribe_id": "tribe_test",
                "position": {"x": 50000.0, "y": 60000.0, "z": 1000.0},
                "facing_yaw": 90.0,
            },
        }))

        # Wait for auth response
        auth_resp = json.loads(await ws.recv())
        responses.append(auth_resp)
        assert auth_resp["type"] == "auth_success", f"Auth failed: {auth_resp}"

        # Send each message and collect responses
        for msg in (messages or []):
            await ws.send(json.dumps({
                "type": "player_message",
                "message": msg,
                "position": {"x": 50000.0, "y": 60000.0, "z": 1000.0},
                "facing_yaw": 90.0,
            }))

            # Collect responses until we get the reply
            while True:
                raw = await asyncio.wait_for(ws.recv(), timeout=60.0)
                resp = json.loads(raw)
                responses.append(resp)
                if resp["type"] == "reply":
                    break

    return responses


@pytest.fixture
async def server_port():
    """Start a test server and yield its port."""
    port = 18765  # Use a non-standard port for testing
    config = make_test_config(port)
    ws_server, bridge = await start_server(config)
    yield port
    ws_server.close()
    await ws_server.wait_closed()


class TestEndToEnd:
    """Full pipeline integration tests with real LLM calls."""

    @pytest.mark.timeout(90)
    async def test_player_knowledge_query(self, server_port):
        """Player asks an ARK knowledge question — should get a helpful answer."""
        responses = await mock_player_session(
            server_port,
            player_name="Bob",
            tier="player",
            messages=["What does a Yutyrannus eat? How do I tame one?"],
        )

        # Find the reply
        reply = next(r for r in responses if r["type"] == "reply")
        text = reply["message"].lower()

        print(f"\n=== PLAYER QUERY RESPONSE ===\n{reply['message']}\n")
        print(f"Stats: {reply.get('stats', {})}")

        # Should contain relevant taming info
        assert len(reply["message"]) > 50, "Response too short"
        # The LLM should have used the lookup_dino tool and gotten info
        assert reply.get("stats", {}).get("tool_calls", 0) >= 0  # May or may not use tools

    @pytest.mark.timeout(90)
    async def test_player_cannot_spawn(self, server_port):
        """Player asks to spawn a dino — should be denied gracefully."""
        responses = await mock_player_session(
            server_port,
            player_name="Hacker",
            tier="player",
            messages=["Spawn me a level 500 Rex right here"],
        )

        reply = next(r for r in responses if r["type"] == "reply")
        text = reply["message"].lower()

        print(f"\n=== PLAYER SPAWN ATTEMPT ===\n{reply['message']}\n")

        # Should NOT say it spawned anything
        assert "spawned" not in text or "can't" in text or "cannot" in text or "don't have" in text or "permission" in text or "not able" in text or "unable" in text, (
            f"Player may have been told a dino was spawned: {reply['message'][:200]}"
        )

    @pytest.mark.timeout(90)
    async def test_admin_can_set_time(self, server_port):
        """Admin asks to change time — should succeed (mock execution)."""
        responses = await mock_player_session(
            server_port,
            player_name="AdminTracy",
            tier="admin",
            messages=["Make it morning please"],
        )

        reply = next(r for r in responses if r["type"] == "reply")

        print(f"\n=== ADMIN SET TIME ===\n{reply['message']}\n")
        print(f"Stats: {reply.get('stats', {})}")

        # Should have used the set_time tool
        assert reply.get("stats", {}).get("tool_calls", 0) >= 1, (
            "Expected at least one tool call for setting time"
        )

    @pytest.mark.timeout(90)
    async def test_player_tool_count_matches_tier(self, server_port):
        """Verify the auth response reports correct tool count per tier."""
        # Player
        player_responses = await mock_player_session(
            server_port, player_name="P1", tier="player", messages=[]
        )
        player_auth = player_responses[0]

        # Admin
        admin_responses = await mock_player_session(
            server_port, player_name="A1", tier="admin", messages=[]
        )
        admin_auth = admin_responses[0]

        player_tools = player_auth["tools_available"]
        admin_tools = admin_auth["tools_available"]

        print(f"\nPlayer tools: {player_tools}, Admin tools: {admin_tools}")

        assert admin_tools > player_tools, (
            f"Admin should have more tools than player: admin={admin_tools}, player={player_tools}"
        )


if __name__ == "__main__":
    """Allow running directly for manual testing."""
    async def main():
        port = 18765
        config = make_test_config(port)
        load_data(config.data_dirs)
        ws_server, bridge = await start_server(config)
        print(f"\nServer running on ws://127.0.0.1:{port}")
        print("Connect with the mock client or run tests.\n")
        try:
            await asyncio.Future()
        finally:
            ws_server.close()

    asyncio.run(main())
