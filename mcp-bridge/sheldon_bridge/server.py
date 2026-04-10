"""WebSocket server — accepts connections from the game mod and mock clients.

This is the main entry point for the bridge. It:
1. Accepts WebSocket connections
2. Authenticates via shared token
3. Creates a session per player
4. Routes messages to the agent
5. Sends responses back
"""

from __future__ import annotations

import asyncio
import json
import logging
import signal

import websockets
from websockets.asyncio.server import serve, ServerConnection

from sheldon_bridge.agent import Agent
from sheldon_bridge.auth import PlayerContext, RateLimiter, TokenAuthenticator
from sheldon_bridge.config import BridgeConfig
from sheldon_bridge.providers.llm import LLMProvider
from sheldon_bridge.session import SessionManager
from sheldon_bridge.tools.registry import ToolRegistry

# Import tool modules to trigger @tool registration
import sheldon_bridge.tools.knowledge  # noqa: F401
import sheldon_bridge.tools.actions  # noqa: F401

logger = logging.getLogger(__name__)


class BridgeServer:
    """The main Sheldon Bridge server."""

    def __init__(self, config: BridgeConfig):
        self.config = config

        # Auth
        self.authenticator = TokenAuthenticator(config.shared_secret)

        # LLM
        self.llm = LLMProvider(config.llm)

        # Tools
        self.registry = ToolRegistry(tier_config=config.tiers or None)
        self.registry.discover()

        # Sessions & rate limiting
        self.sessions = SessionManager()
        self.rate_limiter = RateLimiter()

        # Agent
        self.agent = Agent(
            llm=self.llm,
            registry=self.registry,
            rate_limiter=self.rate_limiter,
        )

        # Track connected clients
        self._connections: dict[str, ServerConnection] = {}

        logger.info(
            f"Bridge initialized: {len(self.registry.all_tools)} tools, "
            f"tiers={self.registry.tier_names}, "
            f"model={config.llm.litellm_model}"
        )

    async def handle_connection(self, websocket: ServerConnection) -> None:
        """Handle a single WebSocket connection from the game mod or mock client."""
        player_id = None
        try:
            # Step 1: Authenticate
            raw = await asyncio.wait_for(websocket.recv(), timeout=10.0)
            auth_msg = json.loads(raw)

            if auth_msg.get("type") != "auth":
                await websocket.close(4001, "First message must be auth")
                return

            token = auth_msg.get("token", "")
            if not self.authenticator.validate_token(token):
                logger.warning(f"Auth failed from {websocket.remote_address}")
                await websocket.close(4001, "Authentication failed")
                return

            # Step 2: Create session
            player_data = auth_msg.get("player", {})
            player = PlayerContext(
                player_id=player_data.get("player_id", "unknown"),
                display_name=player_data.get("display_name", "Unknown"),
                tier=player_data.get("tier", "player"),
                tribe_id=player_data.get("tribe_id", ""),
                position=player_data.get("position", {}),
                facing_yaw=player_data.get("facing_yaw", 0.0),
            )
            player_id = player.player_id

            # Build system prompt
            system_prompt = self.config.build_system_prompt(
                player_name=player.display_name,
                tier=player.tier,
                tribe=player.tribe_id,
            )

            session = self.sessions.create(player, system_prompt)
            self._connections[player_id] = websocket

            # Send auth success
            await websocket.send(json.dumps({
                "type": "auth_success",
                "player_id": player_id,
                "tier": player.tier,
                "tools_available": len(self.registry.get_tools_for_tier(player.tier)),
            }))

            logger.info(
                f"Player connected: {player.display_name} ({player_id[:8]}...) "
                f"tier={player.tier}"
            )

            # Step 3: Message loop
            async for raw_message in websocket:
                try:
                    msg = json.loads(raw_message)
                    await self._handle_message(msg, session, websocket)
                except json.JSONDecodeError:
                    await websocket.send(json.dumps({
                        "type": "error",
                        "message": "Invalid JSON",
                    }))
                except Exception as e:
                    logger.error(f"Error handling message for {player_id}: {e}", exc_info=True)
                    await websocket.send(json.dumps({
                        "type": "error",
                        "message": "Internal error processing your request",
                    }))

        except asyncio.TimeoutError:
            logger.warning(f"Auth timeout from {websocket.remote_address}")
            await websocket.close(4002, "Auth timeout")
        except websockets.exceptions.ConnectionClosed:
            pass
        except Exception as e:
            logger.error(f"Connection error: {e}", exc_info=True)
        finally:
            if player_id:
                self._connections.pop(player_id, None)
                self.sessions.remove(player_id)

    async def _handle_message(
        self, msg: dict, session, websocket: ServerConnection
    ) -> None:
        """Route an incoming message to the appropriate handler."""
        msg_type = msg.get("type")

        if msg_type == "player_message":
            await self._handle_player_message(msg, session, websocket)

        elif msg_type == "position_update":
            # Update player position (sent periodically by the mod)
            pos = msg.get("position", {})
            session.player.update_position(pos, msg.get("facing_yaw", 0.0))

        elif msg_type == "tool_response":
            # Game mod responding to a tool request (future use)
            pass

        elif msg_type == "ping":
            await websocket.send(json.dumps({"type": "pong"}))

        else:
            logger.warning(f"Unknown message type: {msg_type}")

    async def _handle_player_message(
        self, msg: dict, session, websocket: ServerConnection
    ) -> None:
        """Handle a player chat message — run the agentic loop."""
        text = msg.get("message", "").strip()
        if not text:
            return

        # Update position if included in the message
        pos = msg.get("position")
        if pos:
            session.player.update_position(pos, msg.get("facing_yaw", 0.0))

        # Echo request_id from client (for server-side request tracking)
        request_id = msg.get("request_id")

        # Send "thinking" indicator
        thinking_msg = {"type": "thinking"}
        if request_id is not None:
            thinking_msg["request_id"] = request_id
        await websocket.send(json.dumps(thinking_msg))

        # Run the agentic loop
        async with session.lock:
            result = await self.agent.run(session, text)

        # Send response (echo request_id for server-side routing)
        reply_msg = {
            "type": "reply",
            "message": result.response_text,
            "stats": {
                "tool_calls": result.tool_calls_made,
                "iterations": result.iterations,
                "input_tokens": result.total_input_tokens,
                "output_tokens": result.total_output_tokens,
                "cost": round(result.total_cost, 6),
                "duration_ms": round(result.duration_ms, 1),
            },
        }
        if request_id is not None:
            reply_msg["request_id"] = request_id
        await websocket.send(json.dumps(reply_msg))

        logger.info(
            f"[{session.player.display_name}] "
            f"'{text[:50]}...' → "
            f"{result.iterations} iters, "
            f"{result.tool_calls_made} tools, "
            f"${result.total_cost:.4f}, "
            f"{result.duration_ms:.0f}ms"
        )

    async def _cleanup_loop(self) -> None:
        """Periodically clean up expired sessions and rate limiter data."""
        while True:
            await asyncio.sleep(60)
            expired = self.sessions.cleanup_expired()
            self.rate_limiter.cleanup()
            if expired:
                logger.info(f"Cleaned up {expired} expired sessions")


async def run_server(config: BridgeConfig) -> None:
    """Start the bridge server."""
    server = BridgeServer(config)

    # Set up graceful shutdown
    stop = asyncio.get_event_loop().create_future()

    def handle_signal():
        if not stop.done():
            stop.set_result(None)

    loop = asyncio.get_event_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, handle_signal)

    # Start cleanup task
    cleanup_task = asyncio.create_task(server._cleanup_loop())

    logger.info(
        f"Sheldon Bridge starting on "
        f"ws://{config.websocket_host}:{config.websocket_port}"
    )

    async with serve(
        server.handle_connection,
        host=config.websocket_host,
        port=config.websocket_port,
        ping_interval=20,
        ping_timeout=20,
        close_timeout=10,
        max_size=2**20,  # 1MB max message
    ) as ws_server:
        logger.info("Sheldon Bridge is running. Press Ctrl+C to stop.")
        await stop

    cleanup_task.cancel()
    logger.info("Sheldon Bridge stopped.")
