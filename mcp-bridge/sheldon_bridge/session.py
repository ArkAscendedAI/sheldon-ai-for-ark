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
    scope: object = None  # db.manager.Scope (cluster_id, server_id); set on connect
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

    def set_context(self, system_prompt: str, history: list[dict]) -> None:
        """Rebuild this turn's working conversation from the DB-backed assembler:
        [system] + prior history. The agent then appends the new user message.
        The DB — not this in-memory list — is the durable source of truth."""
        self.system_prompt = system_prompt
        self.conversation = [{"role": "system", "content": system_prompt}, *history]

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

    def _evict_if_full(self) -> None:
        """Enforce the session cap: while at/over capacity, evict the least-recently-active
        (largest idle_seconds) session. Per-requester sessions are created on the shared server
        link and only reclaimed after the 1h idle timeout, so under heavy player churn they would
        otherwise grow unbounded between cleanup passes (M2). The DB is the durable source of
        truth, so an evicted player just rebuilds in-memory scratch on their next message. Call
        BEFORE inserting a new id (the caller's own id must not be present, or it could self-evict)."""
        while len(self._sessions) >= self._max_sessions:
            # LEAK-01/SESSION-02: never evict a session with an IN-FLIGHT turn (its lock is held) —
            # that would let a concurrent get_or_create mint a SECOND session for the same player
            # (split-brain → two locks → lost per-player serialization). Evict the LRU among IDLE
            # (unlocked) sessions only; if every session is mid-turn, defer (temporarily over cap).
            candidates = [pid for pid, s in self._sessions.items() if not s.lock.locked()]
            if not candidates:
                logger.warning(
                    f"[session-cap] at {self._max_sessions} but every session is mid-turn; "
                    f"deferring eviction (temporarily over cap) to avoid a split-brain session"
                )
                break
            lru_pid = max(candidates, key=lambda pid: self._sessions[pid].idle_seconds)
            logger.info(
                f"[session-cap] at max_sessions={self._max_sessions}; evicting LRU "
                f"{lru_pid[:8]}... (idle {self._sessions[lru_pid].idle_seconds:.0f}s)"
            )
            self.remove(lru_pid)

    def create(self, player: PlayerContext, system_prompt: str = "") -> Session:
        """Create a new session for a player."""
        # Remove existing session for this player if any
        self._sessions.pop(player.player_id, None)
        self._evict_if_full()  # M2: cap total sessions (LRU) — our own id was just popped

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

    def get_or_create(self, player: PlayerContext, scope: object = None) -> Session:
        """Return the existing session for player.player_id, or create one without
        evicting any other. Used for per-requester sessions multiplexed over a single
        server link (unlike create(), which evicts an existing same-id session)."""
        session = self._sessions.get(player.player_id)
        if session is None:
            self._evict_if_full()  # M2: cap total sessions (LRU) before adding a new requester
            session = Session(player=player)
            if scope is not None:
                session.scope = scope
            self._sessions[player.player_id] = session
            logger.info(
                f"Session created (per-requester) for {player.display_name} "
                f"({player.player_id[:8]}...) tier={player.tier}"
            )
        return session

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
