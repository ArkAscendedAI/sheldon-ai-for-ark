"""Audit-fix regression tests for the REAL game tools (actions.py).

Covers two confirmed audit findings:

  REG-02 — admin-tool magnitude constraints were docstring-only, and the max_*
           check did a raw '>' that crashed on a string arg. The real admin
           tools now declare constraints=, and the check is type-safe.

  ADD-04 — malformed tool-call args reached func(**call_args) unguarded: an
           unexpected kwarg raised TypeError, and bad values could raise inside
           the tool. execute() now filters unknown kwargs and returns a clean
           error dict on TypeError/ValueError.

Unlike test_permissions.py (which registers mock tools in conftest), these tests
exercise the actual @tool-decorated functions in sheldon_bridge.tools.actions and
their real constraints — that's the surface the audit flagged.
"""

import asyncio

import pytest

from sheldon_bridge.tools import actions  # noqa: F401  (import = self-registers tools)
from sheldon_bridge.tools.registry import ToolRegistry, _registered_tools, tool


@pytest.fixture
def real_registry():
    """A registry populated with the REAL action tools.

    conftest's clear_tool_registry empties the global list; re-import the
    actions module's decorated functions so this test is order-independent.
    """
    if not any(t.name == "spawn_dino_at_player" for t in _registered_tools):
        import importlib
        importlib.reload(actions)
    reg = ToolRegistry()
    reg.discover()
    return reg


# --- REG-02: constraints are enforced + type-safe -------------------------------

class TestRealToolConstraints:
    def test_spawn_dino_declares_level_cap(self, real_registry):
        """The real spawn tool must actually carry the 500 cap (not just a docstring)."""
        td = real_registry.get_tool("spawn_dino_at_player")
        assert td is not None
        assert td.constraints.get("max_level") == 500

    def test_spawn_dino_level_99999_is_rejected(self, real_registry):
        """An admin spawning level 99999 MUST be rejected by the constraint check."""
        allowed, reason = real_registry.validate_tool_call(
            "spawn_dino_at_player", {"blueprint": "Rex_BP", "level": 99999}, "admin"
        )
        assert not allowed
        assert "exceeds maximum" in reason.lower() or "500" in reason

    def test_spawn_dino_at_boundary_is_allowed(self, real_registry):
        allowed, _ = real_registry.validate_tool_call(
            "spawn_dino_at_player", {"blueprint": "Rex_BP", "level": 500}, "admin"
        )
        assert allowed

    def test_spawn_dino_one_above_boundary_is_rejected(self, real_registry):
        allowed, _ = real_registry.validate_tool_call(
            "spawn_dino_at_player", {"blueprint": "Rex_BP", "level": 501}, "admin"
        )
        assert not allowed

    def test_spawn_dino_string_level_does_not_crash(self, real_registry):
        """level='500' must NOT raise TypeError — string is coerced and compared."""
        allowed, _ = real_registry.validate_tool_call(
            "spawn_dino_at_player", {"blueprint": "Rex_BP", "level": "500"}, "admin"
        )
        assert allowed  # "500" -> 500.0, within cap

        allowed, reason = real_registry.validate_tool_call(
            "spawn_dino_at_player", {"blueprint": "Rex_BP", "level": "99999"}, "admin"
        )
        assert not allowed
        assert "exceeds maximum" in reason.lower() or "500" in reason

    def test_spawn_dino_nonnumeric_level_is_clean_reject(self, real_registry):
        """A non-numeric level is a clean validation failure, never a TypeError."""
        allowed, reason = real_registry.validate_tool_call(
            "spawn_dino_at_player", {"blueprint": "Rex_BP", "level": "lots"}, "admin"
        )
        assert not allowed
        assert "number" in reason.lower()

    def test_give_item_quantity_caps(self, real_registry):
        """give_item enforces 1..1000."""
        ok, _ = real_registry.validate_tool_call(
            "give_item", {"player_name": "Bob", "blueprint": "Wood", "quantity": 1000}, "admin"
        )
        assert ok
        over, reason = real_registry.validate_tool_call(
            "give_item", {"player_name": "Bob", "blueprint": "Wood", "quantity": 1_000_000}, "admin"
        )
        assert not over
        assert "exceeds maximum" in reason.lower() or "1000" in reason
        under, reason = real_registry.validate_tool_call(
            "give_item", {"player_name": "Bob", "blueprint": "Wood", "quantity": 0}, "admin"
        )
        assert not under
        assert "below minimum" in reason.lower() or "1" in reason

    def test_tier_acl_still_holds(self, real_registry):
        """Sanity: the constraint work did NOT weaken tier gating."""
        allowed, _ = real_registry.validate_tool_call(
            "spawn_dino_at_player", {"blueprint": "Rex_BP", "level": 100}, "player"
        )
        assert not allowed  # player can't spawn at all


# --- ADD-04: execute() guards malformed args ------------------------------------

class TestExecuteArgGuarding:
    def test_unexpected_kwarg_does_not_raise(self, real_registry):
        """An LLM-hallucinated kwarg must be dropped, not crash func(**call_args)."""
        result = asyncio.run(
            real_registry.execute(
                "broadcast",
                {"message": "hello", "bogus_arg": "boom", "another": 123},
                context={},  # no game_handler -> mock branch
            )
        )
        assert result.get("success") is True
        assert result.get("mock") is True

    def test_bad_value_returns_clean_error_dict(self, real_registry):
        """A ValueError/TypeError raised inside the tool from a bad value type
        becomes a clean {success: False, error: ...} dict, not a propagated exception."""
        # set_time formats hour with :02d — passing a non-int hour raises ValueError
        # inside the tool body; execute() must catch it.
        result = asyncio.run(
            real_registry.execute(
                "set_time", {"hour": "noon"}, context={}
            )
        )
        assert isinstance(result, dict)
        assert result.get("success") is False
        assert "error" in result

    def test_valid_call_still_works(self, real_registry):
        """A well-formed call is unaffected by the new filtering/guard."""
        result = asyncio.run(
            real_registry.execute("set_time", {"hour": 12, "minute": 30}, context={})
        )
        assert result.get("success") is True
        assert "12:30" in result.get("message", "")

    def test_ctx_injection_still_works(self, real_registry):
        """ctx must still be injected (and not be treated as an unknown kwarg)."""
        captured = {}

        async def fake_handler(cmd):
            captured.update(cmd)
            return {"success": True, "echoed": True}

        result = asyncio.run(
            real_registry.execute(
                "broadcast", {"message": "ping"}, context={"game_handler": fake_handler}
            )
        )
        assert result.get("echoed") is True
        assert captured.get("action") == "console_command"

    def test_var_kwargs_tool_is_not_filtered(self):
        """A tool that declares **kwargs should receive everything (no over-filtering)."""
        seen = {}

        @tool(tier="player", description="kwargs sink")
        def sink(**kwargs) -> dict:
            seen.update(kwargs)
            return {"success": True}

        reg = ToolRegistry()
        reg.discover()
        asyncio.run(reg.execute("sink", {"a": 1, "b": 2}, context={}))
        assert seen == {"a": 1, "b": 2}
