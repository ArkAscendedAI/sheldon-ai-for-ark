"""Interactive mock client that simulates the game mod.

Use this to test the bridge without the actual ARK mod. Connects via
WebSocket, authenticates, and lets you type messages as a player.

Usage:
    python -m sheldon_bridge.mock_client --tier admin --name "TestPlayer"
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys

import websockets


async def run_client(
    url: str,
    token: str,
    player_name: str,
    tier: str,
    player_id: str = "MOCK_EOS_001",
):
    """Connect to the bridge and run an interactive chat session."""
    print(f"Connecting to {url}...")

    try:
        async with websockets.connect(url) as ws:
            # Authenticate
            auth_msg = {
                "type": "auth",
                "token": token,
                "player": {
                    "player_id": player_id,
                    "display_name": player_name,
                    "tier": tier,
                    "tribe_id": "MockTribe",
                    "position": {"x": 234567.0, "y": 345678.0, "z": 12000.0},
                    "facing_yaw": 135.0,
                },
            }
            await ws.send(json.dumps(auth_msg))

            # Wait for auth response
            raw = await asyncio.wait_for(ws.recv(), timeout=10.0)
            response = json.loads(raw)

            if response.get("type") == "auth_success":
                print(f"Authenticated as {player_name} (tier: {tier})")
                print(f"Tools available: {response.get('tools_available', '?')}")
                print("-" * 60)
                print("Type messages to Sheldon. Press Ctrl+C to quit.\n")
            else:
                print(f"Auth failed: {response}")
                return

            # Start receiver task
            receiver_task = asyncio.create_task(_receive_messages(ws))

            # Interactive input loop
            try:
                while True:
                    line = await asyncio.to_thread(input, "You: ")
                    if not line.strip():
                        continue

                    msg = {
                        "type": "player_message",
                        "message": line.strip(),
                        "position": {"x": 234567.0, "y": 345678.0, "z": 12000.0},
                        "facing_yaw": 135.0,
                    }
                    await ws.send(json.dumps(msg))

            except (KeyboardInterrupt, EOFError):
                print("\nDisconnecting...")
                receiver_task.cancel()

    except ConnectionRefusedError:
        print(f"Could not connect to {url}. Is the bridge running?")
    except Exception as e:
        print(f"Error: {e}")


async def _receive_messages(ws):
    """Background task to print incoming messages."""
    try:
        async for raw in ws:
            msg = json.loads(raw)
            msg_type = msg.get("type")

            if msg_type == "thinking":
                print("Sheldon is thinking...", flush=True)

            elif msg_type == "reply":
                text = msg.get("message", "")
                stats = msg.get("stats", {})
                print(f"\nSheldon: {text}")
                print(
                    f"  [{stats.get('iterations', 0)} iter, "
                    f"{stats.get('tool_calls', 0)} tools, "
                    f"${stats.get('cost', 0):.4f}, "
                    f"{stats.get('duration_ms', 0):.0f}ms]"
                )
                print()

            elif msg_type == "error":
                print(f"\n[ERROR] {msg.get('message', 'Unknown error')}\n")

            elif msg_type == "pong":
                pass  # Heartbeat response

            else:
                print(f"\n[{msg_type}] {json.dumps(msg, indent=2)}\n")

    except websockets.exceptions.ConnectionClosed:
        print("\nConnection closed by server.")
    except asyncio.CancelledError:
        pass


def main():
    parser = argparse.ArgumentParser(description="Mock game client for Sheldon Bridge")
    parser.add_argument("--url", default="ws://localhost:8443", help="Bridge WebSocket URL")
    parser.add_argument("--token", required=True, help="Shared auth token")
    parser.add_argument("--name", default="MockPlayer", help="Player display name")
    parser.add_argument("--tier", default="admin", choices=["player", "admin", "superadmin"])
    parser.add_argument("--player-id", default="MOCK_EOS_001", help="Player EOS ID")
    args = parser.parse_args()

    asyncio.run(run_client(
        url=args.url,
        token=args.token,
        player_name=args.name,
        tier=args.tier,
        player_id=args.player_id,
    ))


if __name__ == "__main__":
    main()
