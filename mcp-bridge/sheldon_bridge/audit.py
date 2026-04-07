"""
Structured audit logging for security-critical events.

Every tool call (allowed and denied), auth attempt, and rate limit hit
is logged as a JSON line to an append-only audit file. This creates an
accountability trail for security review.

Log format: one JSON object per line (JSONL), each with:
  - timestamp (ISO 8601)
  - event type
  - player context (id, name, tier)
  - action details
  - outcome (allowed/denied + reason)
"""

from __future__ import annotations

import json
import logging
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


class AuditLogger:
    """Append-only JSONL audit log for security events."""

    def __init__(self, log_file: str = "./logs/audit.jsonl"):
        self._log_path = Path(log_file)
        self._log_path.parent.mkdir(parents=True, exist_ok=True)
        self._file = None
        self._open()

    def _open(self):
        """Open the log file for appending."""
        try:
            self._file = open(self._log_path, "a", buffering=1)  # line-buffered
            logger.info(f"Audit log opened: {self._log_path}")
        except Exception as e:
            logger.error(f"Failed to open audit log {self._log_path}: {e}")
            self._file = None

    def _write(self, entry: dict[str, Any]) -> None:
        """Write a single audit entry as a JSON line."""
        entry["timestamp"] = datetime.now(timezone.utc).isoformat()
        try:
            if self._file:
                self._file.write(json.dumps(entry, default=str) + "\n")
        except Exception as e:
            logger.error(f"Failed to write audit entry: {e}")

    def log_auth_attempt(
        self,
        remote_address: str,
        success: bool,
        player_id: str = "",
        display_name: str = "",
        tier: str = "",
    ) -> None:
        """Log an authentication attempt."""
        self._write({
            "event": "auth",
            "remote_address": remote_address,
            "success": success,
            "player_id": player_id,
            "display_name": display_name,
            "tier": tier,
        })

    def log_tool_call(
        self,
        player_id: str,
        display_name: str,
        tier: str,
        tool_name: str,
        arguments: dict[str, Any],
        allowed: bool,
        reason: str = "",
        result_summary: str = "",
    ) -> None:
        """Log a tool call attempt (allowed or denied)."""
        self._write({
            "event": "tool_call",
            "player_id": player_id,
            "display_name": display_name,
            "tier": tier,
            "tool": tool_name,
            "arguments": _sanitize_arguments(arguments),
            "allowed": allowed,
            "reason": reason,
            "result_summary": result_summary[:500] if result_summary else "",
        })

    def log_rate_limit(
        self,
        player_id: str,
        display_name: str,
        tier: str,
        action: str,
        reason: str,
    ) -> None:
        """Log a rate limit hit."""
        self._write({
            "event": "rate_limit",
            "player_id": player_id,
            "display_name": display_name,
            "tier": tier,
            "action": action,
            "reason": reason,
        })

    def log_session_event(
        self,
        event_type: str,
        player_id: str,
        display_name: str,
        tier: str,
        details: dict[str, Any] | None = None,
    ) -> None:
        """Log a session lifecycle event (connect, disconnect, etc.)."""
        entry = {
            "event": f"session_{event_type}",
            "player_id": player_id,
            "display_name": display_name,
            "tier": tier,
        }
        if details:
            entry["details"] = details
        self._write(entry)

    def log_player_message(
        self,
        player_id: str,
        display_name: str,
        tier: str,
        message: str,
        response_summary: str = "",
        tool_calls: int = 0,
        cost: float = 0.0,
        duration_ms: float = 0.0,
    ) -> None:
        """Log a player message and the resulting response."""
        self._write({
            "event": "player_message",
            "player_id": player_id,
            "display_name": display_name,
            "tier": tier,
            "message": message[:500],
            "response_summary": response_summary[:200],
            "tool_calls": tool_calls,
            "cost": round(cost, 6),
            "duration_ms": round(duration_ms, 1),
        })

    def close(self) -> None:
        """Close the audit log file."""
        if self._file:
            self._file.close()
            self._file = None


def _sanitize_arguments(args: dict[str, Any]) -> dict[str, Any]:
    """Sanitize tool call arguments for logging (truncate large values)."""
    sanitized = {}
    for key, value in args.items():
        if isinstance(value, str) and len(value) > 200:
            sanitized[key] = value[:200] + "..."
        elif isinstance(value, (list, dict)):
            s = json.dumps(value, default=str)
            if len(s) > 200:
                sanitized[key] = s[:200] + "..."
            else:
                sanitized[key] = value
        else:
            sanitized[key] = value
    return sanitized
