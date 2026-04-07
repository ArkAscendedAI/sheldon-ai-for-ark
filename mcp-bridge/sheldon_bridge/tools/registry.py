"""Tool registry with tier-based access control.

Tools are Python functions decorated with @tool(). Each tool declares which
permission tier(s) can access it. The registry generates LLM-compatible
JSON schemas from type hints and docstrings.

Usage:
    from sheldon_bridge.tools.registry import tool, ToolRegistry

    @tool(tier="admin", description="Spawn a dino near a player")
    async def spawn_dino(blueprint: str, level: int, gender: str = "random") -> dict:
        ...

    registry = ToolRegistry()
    registry.discover()  # Auto-discovers all @tool decorated functions

    # Get tools for a specific tier
    player_tools = registry.get_tools_for_tier("player")
    admin_tools = registry.get_tools_for_tier("admin")
"""

from __future__ import annotations

import asyncio
import inspect
import json
import logging
from dataclasses import dataclass, field
from enum import Enum
from fnmatch import fnmatch
from typing import Any, Callable, get_type_hints

logger = logging.getLogger(__name__)

# Global list of registered tools (populated by @tool decorator)
_registered_tools: list[ToolDefinition] = []


@dataclass
class ToolDefinition:
    """A registered tool with metadata and access control."""

    name: str
    description: str
    function: Callable
    tier: str  # minimum tier required (e.g., "player", "admin", "superadmin")
    parameters: dict[str, Any]  # JSON Schema for parameters
    constraints: dict[str, Any] = field(default_factory=dict)


def tool(
    tier: str = "player",
    description: str | None = None,
    constraints: dict[str, Any] | None = None,
):
    """Decorator to register a function as a tool.

    Args:
        tier: Minimum permission tier required to use this tool.
        description: Tool description for the LLM. Defaults to the function's docstring.
        constraints: Optional parameter constraints (e.g., {"max_level": 500}).
    """

    def decorator(func: Callable) -> Callable:
        func_description = description or (func.__doc__ or "").strip().split("\n")[0]
        params_schema = _build_params_schema(func)

        tool_def = ToolDefinition(
            name=func.__name__,
            description=func_description,
            function=func,
            tier=tier,
            parameters=params_schema,
            constraints=constraints or {},
        )
        _registered_tools.append(tool_def)

        # Attach metadata to the function for introspection
        func._tool_definition = tool_def
        return func

    return decorator


def _python_type_to_json_type(python_type: type) -> str:
    """Convert a Python type hint to a JSON Schema type string."""
    type_map = {
        str: "string",
        int: "integer",
        float: "number",
        bool: "boolean",
        list: "array",
        dict: "object",
    }
    return type_map.get(python_type, "string")


def _build_params_schema(func: Callable) -> dict[str, Any]:
    """Build a JSON Schema object from a function's type hints and defaults."""
    sig = inspect.signature(func)
    hints = get_type_hints(func)

    properties = {}
    required = []

    for param_name, param in sig.parameters.items():
        # Skip 'self', 'ctx', and internal parameters
        if param_name in ("self", "ctx", "context", "session", "websocket"):
            continue

        param_type = hints.get(param_name, str)
        json_type = _python_type_to_json_type(param_type)

        prop: dict[str, Any] = {"type": json_type}

        # Extract description from docstring Args section if available
        doc = func.__doc__ or ""
        if f"{param_name}:" in doc:
            # Simple extraction: find "param_name: description" in docstring
            for line in doc.split("\n"):
                stripped = line.strip()
                if stripped.startswith(f"{param_name}:"):
                    prop["description"] = stripped.split(":", 1)[1].strip()
                    break

        # Handle enum types
        if hasattr(param_type, "__args__"):
            # This is a Literal type or similar
            pass

        # Check for defaults
        if param.default is not inspect.Parameter.empty:
            prop["default"] = param.default
        else:
            required.append(param_name)

        properties[param_name] = prop

    schema: dict[str, Any] = {
        "type": "object",
        "properties": properties,
    }
    if required:
        schema["required"] = required

    return schema


class ToolRegistry:
    """Manages tool registration, tier-based access, and schema generation.

    The registry loads tier configuration that defines which tools each tier
    can access, supporting wildcard patterns and inheritance.
    """

    def __init__(self, tier_config: dict[str, Any] | None = None):
        self._tools: dict[str, ToolDefinition] = {}
        self._tier_config = tier_config or self._default_tier_config()
        self._resolved_tiers: dict[str, set[str]] = {}

    def _default_tier_config(self) -> dict[str, Any]:
        """Default tier configuration if none provided."""
        return {
            "player": {
                "tools": ["lookup_*", "calculate_*", "get_my_*", "get_server_*", "get_time_*"],
            },
            "admin": {
                "inherits": "player",
                "tools": [
                    "census_*",
                    "get_all_*",
                    "get_player_*",
                    "get_tribe_*",
                    "spawn_*",
                    "give_*",
                    "teleport_*",
                    "set_*",
                    "destroy_*",
                    "broadcast",
                    "direct_message",
                    "kick_player",
                    "ban_player",
                    "execute_console_command",
                    "trigger_save",
                ],
            },
            "superadmin": {
                "inherits": "admin",
                "tools": ["*"],
            },
        }

    def discover(self) -> None:
        """Load all registered tools from the global registry."""
        for tool_def in _registered_tools:
            self._tools[tool_def.name] = tool_def
        self._resolve_tiers()
        logger.info(f"Discovered {len(self._tools)} tools")

    def register(self, tool_def: ToolDefinition) -> None:
        """Manually register a tool."""
        self._tools[tool_def.name] = tool_def
        self._resolve_tiers()

    def _resolve_tiers(self) -> None:
        """Resolve tier inheritance and wildcard patterns into concrete tool sets."""
        self._resolved_tiers = {}
        all_tool_names = set(self._tools.keys())

        for tier_name, tier_cfg in self._tier_config.items():
            allowed = set()

            # Inherit from parent tier
            parent = tier_cfg.get("inherits")
            if parent and parent in self._resolved_tiers:
                allowed |= self._resolved_tiers[parent]

            # Resolve wildcard patterns
            patterns = tier_cfg.get("tools", [])
            for pattern in patterns:
                for tool_name in all_tool_names:
                    if fnmatch(tool_name, pattern):
                        allowed.add(tool_name)

            self._resolved_tiers[tier_name] = allowed

    def get_tools_for_tier(self, tier: str) -> list[ToolDefinition]:
        """Get all tools accessible to a specific permission tier."""
        allowed_names = self._resolved_tiers.get(tier, set())
        return [self._tools[name] for name in allowed_names if name in self._tools]

    def get_tool(self, name: str) -> ToolDefinition | None:
        """Get a single tool by name."""
        return self._tools.get(name)

    def is_allowed(self, tool_name: str, tier: str) -> bool:
        """Check if a tool is allowed for a given tier (defense in depth)."""
        return tool_name in self._resolved_tiers.get(tier, set())

    def to_llm_format(self, tier: str) -> list[dict[str, Any]]:
        """Generate the OpenAI-compatible tool definitions for a tier.

        This is what gets sent to the LLM via LiteLLM. Each tool is formatted as:
        {"type": "function", "function": {"name": ..., "description": ..., "parameters": ...}}
        """
        tools = self.get_tools_for_tier(tier)
        return [
            {
                "type": "function",
                "function": {
                    "name": t.name,
                    "description": t.description,
                    "parameters": t.parameters,
                },
            }
            for t in tools
        ]

    def validate_tool_call(
        self, tool_name: str, arguments: dict[str, Any], tier: str
    ) -> tuple[bool, str]:
        """Validate a tool call against tier permissions and parameter constraints.

        Returns (allowed, reason). This is the defense-in-depth check that runs
        AFTER the LLM has already been given only tier-appropriate tools.
        """
        # Check tier access
        if not self.is_allowed(tool_name, tier):
            return False, f"Tool '{tool_name}' is not available for tier '{tier}'"

        tool_def = self._tools.get(tool_name)
        if not tool_def:
            return False, f"Unknown tool: '{tool_name}'"

        # Check parameter constraints from tier config
        tier_cfg = self._tier_config.get(tier, {})
        tier_constraints = tier_cfg.get("constraints", {}).get(tool_name, {})

        # Merge tool-level constraints with tier-level constraints
        all_constraints = {**tool_def.constraints, **tier_constraints}

        for constraint_key, constraint_value in all_constraints.items():
            # Handle max_* constraints
            if constraint_key.startswith("max_"):
                param_name = constraint_key[4:]  # strip "max_"
                if param_name in arguments:
                    if arguments[param_name] > constraint_value:
                        return (
                            False,
                            f"Parameter '{param_name}' value {arguments[param_name]} "
                            f"exceeds maximum {constraint_value} for tier '{tier}'",
                        )

        return True, "ok"

    async def execute(
        self,
        tool_name: str,
        arguments: dict[str, Any],
        context: dict[str, Any] | None = None,
    ) -> Any:
        """Execute a tool by name with the given arguments.

        Args:
            tool_name: The tool function name.
            arguments: Parsed arguments from the LLM.
            context: Optional context dict (player info, websocket, etc.)
                     injected into functions that accept a 'ctx' parameter.
        """
        tool_def = self._tools.get(tool_name)
        if not tool_def:
            raise ValueError(f"Unknown tool: {tool_name}")

        func = tool_def.function

        # Inject context if the function accepts it
        sig = inspect.signature(func)
        call_args = dict(arguments)
        if "ctx" in sig.parameters and context is not None:
            call_args["ctx"] = context

        # Call the function (async or sync)
        if asyncio.iscoroutinefunction(func):
            return await func(**call_args)
        else:
            return func(**call_args)

    @property
    def all_tools(self) -> dict[str, ToolDefinition]:
        return dict(self._tools)

    @property
    def tier_names(self) -> list[str]:
        return list(self._tier_config.keys())
