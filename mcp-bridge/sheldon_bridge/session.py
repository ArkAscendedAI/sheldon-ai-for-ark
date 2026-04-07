"""Per-player session management with conversation history and token budgeting.

Each player connected to the bridge gets an isolated session containing their
conversation history, player context, and token usage tracking. Sessions are
created on WebSocket connect and destroyed on disconnect.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field

from sheldon_bridge.auth import PlayerContext

logger = logging.getLogger(__name__)


@dataclass
class Session:
    """A single player's conversation session."""

    player: PlayerContext
    conversation: list[dict[str, str]] = field(default_factory=list)
    system_prompt: str = ""
    total_input_tokens: int = 0
    total_output_tokens: int = 0
    total_cost: float = 0.0
    created_at: float = field(default_factory=time.time)
    last_active: float = field(default_factory=time.time)
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    _tool_call_count: int = 0

    def add_system_prompt(self, prompt: str) -> None:
        """Set the system prompt (first message in the conversation)."""
        self.system_prompt = prompt
        if self.conversation and self.conversation[0]["role"] == "system":
            self.conversation[0]["content"] = prompt
        else:
            self.conversation.insert(0, {"role": "system", "content": prompt})

    def add_user_message(self, content: str) -> None:
        """Add a user message to the conversation history."""
        self.conversation.append({"role": "user", "content": content})
        self.last_active = time.time()

    def add_assistant_message(self, message: dict) -> None:
        """Add an assistant message (may contain tool_calls)."""
        self.conversation.append(message)
        self.last_active = time.time()

    def add_tool_result(self, tool_call_id: str, name: str, content: str) -> None:
        """Add a tool execution result to the conversation."""
        self.conversation.append({
            "tool_call_id": tool_call_id,
            "role": "tool",
            "name": name,
            "content": content,
        })
        self._tool_call_count += 1
        self.last_active = time.time()

    def get_messages(self) -> list[dict]:
        """Get the full conversation history for an LLM call."""
        return list(self.conversation)

    def track_usage(self, input_tokens: int, output_tokens: int, cost: float) -> None:
        """Track token usage and cost for this session."""
        self.total_input_tokens += input_tokens
        self.total_output_tokens += output_tokens
        self.total_cost += cost

    def truncate_to_budget(self, max_tokens: int, reserve: int = 4096) -> None:
        """Truncate conversation history to fit within a token budget.

        Keeps the system prompt and most recent messages, dropping oldest
        exchanges first. Uses a rough estimate of 4 chars per token.
        """
        target = max_tokens - reserve

        # Always keep system prompt
        system_msg = None
        other_msgs = []
        for msg in self.conversation:
            if msg["role"] == "system":
                system_msg = msg
            else:
                other_msgs.append(msg)

        # Estimate tokens (rough: 1 token ≈ 4 chars)
        def estimate_tokens(msg: dict) -> int:
            content = msg.get("content", "")
            if isinstance(content, str):
                return len(content) // 4
            return 50  # tool_calls and other structures

        system_tokens = estimate_tokens(system_msg) if system_msg else 0
        budget = target - system_tokens

        # Keep messages from the end until budget is exceeded
        kept = []
        running = 0
        for msg in reversed(other_msgs):
            msg_tokens = estimate_tokens(msg)
            if running + msg_tokens > budget:
                break
            kept.insert(0, msg)
            running += msg_tokens

        self.conversation = ([system_msg] if system_msg else []) + kept
        logger.debug(
            f"Truncated session for {self.player.player_id}: "
            f"kept {len(kept)} messages, ~{running + system_tokens} tokens"
        )

    @property
    def tool_call_count(self) -> int:
        return self._tool_call_count

    @property
    def age_seconds(self) -> float:
        return time.time() - self.created_at

    @property
    def idle_seconds(self) -> float:
        return time.time() - self.last_active


class SessionManager:
    """Manages all active player sessions.

    Sessions are created on WebSocket connect and destroyed on disconnect.
    A background cleanup task removes sessions that have been idle too long.
    """

    def __init__(self, session_timeout: int = 3600, max_sessions: int = 100):
        self._sessions: dict[str, Session] = {}
        self._session_timeout = session_timeout
        self._max_sessions = max_sessions

    def create(self, player: PlayerContext, system_prompt: str = "") -> Session:
        """Create a new session for a player."""
        # Remove existing session for this player if any
        self._sessions.pop(player.player_id, None)

        session = Session(player=player)
        if system_prompt:
            session.add_system_prompt(system_prompt)

        self._sessions[player.player_id] = session
        logger.info(
            f"Session created for {player.display_name} "
            f"({player.player_id[:8]}...) tier={player.tier}"
        )
        return session

    def get(self, player_id: str) -> Session | None:
        """Get an existing session by player ID."""
        return self._sessions.get(player_id)

    def remove(self, player_id: str) -> None:
        """Remove a session."""
        session = self._sessions.pop(player_id, None)
        if session:
            logger.info(
                f"Session removed for {session.player.display_name} "
                f"({player_id[:8]}...) "
                f"duration={session.age_seconds:.0f}s "
                f"cost=${session.total_cost:.4f}"
            )

    def cleanup_expired(self) -> int:
        """Remove sessions that have been idle too long. Returns count removed."""
        expired = [
            pid
            for pid, session in self._sessions.items()
            if session.idle_seconds > self._session_timeout
        ]
        for pid in expired:
            self.remove(pid)
        return len(expired)

    @property
    def active_count(self) -> int:
        return len(self._sessions)

    @property
    def all_sessions(self) -> dict[str, Session]:
        return dict(self._sessions)
