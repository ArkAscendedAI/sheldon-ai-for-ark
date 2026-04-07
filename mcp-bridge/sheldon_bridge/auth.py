"""Authentication and authorization for the Sheldon Bridge.

V1 uses token-based auth on WebSocket connect. The mod sends a shared secret
on connection; the bridge validates it. All subsequent messages on that
connection are trusted with the tier established at connect time.

Future versions may add per-message HMAC signing for defense-in-depth.
"""

from __future__ import annotations

import hmac
import logging
import secrets
import time
from collections import OrderedDict
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)


@dataclass
class PlayerContext:
    """Verified player identity attached to a WebSocket session.

    This context is established at connection time based on data from the mod.
    The bridge trusts it because the connection was authenticated with the
    shared secret — only the mod knows the secret.
    """

    player_id: str  # EOS ID
    display_name: str
    tier: str  # "player", "admin", "superadmin", or custom
    tribe_id: str = ""
    position: dict[str, float] = field(default_factory=dict)
    facing_yaw: float = 0.0

    def update_position(self, position: dict[str, float], facing_yaw: float = 0.0) -> None:
        """Update player position (sent with each message from the mod)."""
        self.position = position
        self.facing_yaw = facing_yaw


class TokenAuthenticator:
    """Validates connections using a shared secret token.

    The mod sends the token as the first message on WebSocket connect.
    If it matches, the connection is authenticated and a session is created.
    """

    def __init__(self, shared_secret: str):
        if not shared_secret or len(shared_secret) < 16:
            raise ValueError("Shared secret must be at least 16 characters")
        self._secret = shared_secret

    def validate_token(self, token: str) -> bool:
        """Validate a token using constant-time comparison."""
        return hmac.compare_digest(self._secret, token)

    @staticmethod
    def generate_secret(length: int = 48) -> str:
        """Generate a cryptographically secure shared secret."""
        return secrets.token_urlsafe(length)


class RateLimiter:
    """Per-player, per-tier rate limiting.

    Tracks request counts in sliding time windows. Returns whether a
    request should be allowed or rejected.
    """

    def __init__(self, tier_limits: dict[str, dict[str, int]] | None = None):
        self._limits = tier_limits or {
            "player": {"requests_per_minute": 10, "tool_calls_per_minute": 5},
            "admin": {"requests_per_minute": 30, "tool_calls_per_minute": 20},
            "superadmin": {"requests_per_minute": 60, "tool_calls_per_minute": 40},
        }
        # Track timestamps: {player_id: {"requests": [timestamps], "tool_calls": [timestamps]}}
        self._windows: dict[str, dict[str, list[float]]] = {}

    def check(self, player_id: str, tier: str, action: str = "requests") -> tuple[bool, str]:
        """Check if a request is within rate limits.

        Args:
            player_id: The player's ID.
            tier: The player's permission tier.
            action: "requests" or "tool_calls".

        Returns:
            (allowed, reason) tuple.
        """
        limits = self._limits.get(tier, self._limits.get("player", {}))
        limit_key = f"{action}_per_minute"
        max_count = limits.get(limit_key, 10)

        now = time.time()
        window_start = now - 60.0

        # Initialize tracking for this player
        if player_id not in self._windows:
            self._windows[player_id] = {"requests": [], "tool_calls": []}

        # Clean old entries
        timestamps = self._windows[player_id].setdefault(action, [])
        timestamps[:] = [t for t in timestamps if t > window_start]

        if len(timestamps) >= max_count:
            return False, f"Rate limit exceeded: {len(timestamps)}/{max_count} {action} per minute"

        timestamps.append(now)
        return True, "ok"

    def cleanup(self, max_age: float = 120.0) -> None:
        """Remove stale tracking data."""
        now = time.time()
        cutoff = now - max_age
        stale = [
            pid
            for pid, windows in self._windows.items()
            if all(
                not timestamps or timestamps[-1] < cutoff
                for timestamps in windows.values()
            )
        ]
        for pid in stale:
            del self._windows[pid]
