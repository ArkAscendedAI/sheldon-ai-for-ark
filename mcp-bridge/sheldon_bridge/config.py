"""Configuration loading and validation for the Sheldon Bridge."""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass, field
from pathlib import Path

from sheldon_bridge.auth import TokenAuthenticator
from sheldon_bridge.providers.llm import LLMConfig

logger = logging.getLogger(__name__)

DEFAULT_CONFIG_PATH = "config.json"


@dataclass
class BridgeConfig:
    """Full configuration for the Sheldon Bridge."""

    # LLM settings
    llm: LLMConfig

    # Server settings
    websocket_host: str = "0.0.0.0"
    websocket_port: int = 8443
    ssl_cert: str | None = None
    ssl_key: str | None = None

    # Auth
    shared_secret: str = ""

    # Tier config
    tiers: dict = field(default_factory=dict)

    # Personality
    personality_name: str = "Sheldon"
    personality_prompt_file: str | None = None
    server_context_dir: str | None = None

    # Data
    data_dirs: list[str] = field(default_factory=lambda: ["./data/vanilla", "./data/custom"])

    # Logging
    audit_file: str = "./logs/audit.jsonl"
    log_level: str = "INFO"

    # Agent
    max_tool_iterations: int = 25

    def get_personality_prompt(self) -> str:
        """Load the personality prompt from file or return a default."""
        if self.personality_prompt_file and Path(self.personality_prompt_file).exists():
            return Path(self.personality_prompt_file).read_text().strip()

        return (
            f"You are {self.personality_name}, a helpful AI assistant embedded in an "
            f"ARK: Survival Ascended server.\n\n"
            f"You are knowledgeable about ARK game mechanics, dinosaurs, crafting, "
            f"taming, breeding, and survival strategies. When players ask questions, "
            f"give practical, actionable answers.\n\n"
            f"When you execute commands (spawning dinos, giving items, changing time), "
            f"confirm what you did in a friendly, conversational way.\n\n"
            f"If a player asks for something you can't do with your available tools, "
            f"explain what you can help with instead.\n\n"
            f"USE YOUR TOOLS for any factual lookups. Do not guess blueprint paths, "
            f"coordinates, or recipes from memory — look them up."
        )

    def get_server_context(self) -> str:
        """Load all markdown files from the server context directory."""
        if not self.server_context_dir:
            return ""

        context_dir = Path(self.server_context_dir)
        if not context_dir.exists():
            return ""

        parts = []
        for md_file in sorted(context_dir.glob("*.md")):
            parts.append(f"## {md_file.stem}\n\n{md_file.read_text().strip()}")

        if parts:
            return "\n\n---\n\n".join(parts)
        return ""

    def build_system_prompt(self, player_name: str, tier: str, tribe: str = "") -> str:
        """Assemble the full system prompt for a player session."""
        prompt_parts = [self.get_personality_prompt()]

        server_ctx = self.get_server_context()
        if server_ctx:
            prompt_parts.append(f"## Server Information\n\n{server_ctx}")

        player_info = f"## Current Player\n\nYou are talking to {player_name} ({tier} tier)."
        if tribe:
            player_info += f" They are in the tribe '{tribe}'."
        prompt_parts.append(player_info)

        if tier == "player":
            prompt_parts.append(
                "This player has standard permissions. You can help them with "
                "information, lookups, and calculations. You CANNOT execute admin "
                "commands (spawning, giving items, teleporting, etc.) for them."
            )
        elif tier in ("admin", "superadmin"):
            prompt_parts.append(
                "This player has admin permissions. You can execute commands on "
                "their behalf including spawning dinos, giving items, teleporting, "
                "and server management."
            )

        return "\n\n".join(prompt_parts)


def _resolve_env_vars(value: str) -> str:
    """Resolve ${ENV_VAR} patterns in config values."""
    if isinstance(value, str) and value.startswith("${") and value.endswith("}"):
        env_name = value[2:-1]
        env_value = os.environ.get(env_name, "")
        if not env_value:
            logger.warning(f"Environment variable {env_name} not set")
        return env_value
    return value


def load_config(path: str = DEFAULT_CONFIG_PATH) -> BridgeConfig:
    """Load configuration from a JSON file.

    Environment variables in the format ${VAR_NAME} are resolved at load time.
    """
    config_path = Path(path)
    if not config_path.exists():
        raise FileNotFoundError(
            f"Config file not found: {path}\n"
            f"Run 'sheldon-bridge init' to create one, or copy from examples/config.example.json"
        )

    raw = json.loads(config_path.read_text())

    # Resolve env vars in LLM config
    llm_raw = raw.get("llm", {})
    provider = llm_raw.get("provider", "openrouter")

    # Default model per provider
    default_models = {
        "openrouter": "openrouter/anthropic/claude-sonnet-4-20250514",
        "anthropic": "anthropic/claude-sonnet-4-20250514",
        "openai": "openai/gpt-4o",
        "gemini": "gemini/gemini-2.0-flash",
    }

    llm_config = LLMConfig(
        provider=provider,
        model=llm_raw.get("model", default_models.get(provider, "openrouter/anthropic/claude-sonnet-4-20250514")),
        api_key=_resolve_env_vars(llm_raw.get("api_key", "")),
        max_tokens=llm_raw.get("max_tokens", 4096),
        temperature=llm_raw.get("temperature", 0.7),
        timeout=llm_raw.get("timeout", 60),
        num_retries=llm_raw.get("num_retries", 2),
    )

    # Resolve env vars in auth
    auth_raw = raw.get("auth", {})
    shared_secret = _resolve_env_vars(auth_raw.get("shared_secret", ""))

    # Server config
    server_raw = raw.get("server", {})

    # Personality
    personality_raw = raw.get("personality", {})

    # Data dirs
    data_raw = raw.get("data", {})
    data_dirs = data_raw.get("dino_data_dirs", ["./data/vanilla", "./data/custom"])

    # Logging
    logging_raw = raw.get("logging", {})

    return BridgeConfig(
        llm=llm_config,
        websocket_host=server_raw.get("websocket_host", "0.0.0.0"),
        websocket_port=server_raw.get("websocket_port", 8443),
        ssl_cert=server_raw.get("websocket_ssl_cert"),
        ssl_key=server_raw.get("websocket_ssl_key"),
        shared_secret=shared_secret,
        tiers=raw.get("tiers", {}),
        personality_name=personality_raw.get("name", "Sheldon"),
        personality_prompt_file=personality_raw.get("prompt_file"),
        server_context_dir=personality_raw.get("server_context_dir"),
        data_dirs=data_dirs,
        audit_file=logging_raw.get("audit_file", "./logs/audit.jsonl"),
        log_level=logging_raw.get("level", "INFO"),
        max_tool_iterations=llm_raw.get("max_tool_iterations", 25),
    )


def initialize_config(path: str = DEFAULT_CONFIG_PATH) -> None:
    """Interactive setup — generates a minimal config with sensible defaults.

    The only things a user MUST provide:
      1. LLM API key
      2. LLM provider choice

    Everything else has working defaults.
    """
    config_path = Path(path)
    if config_path.exists():
        print(f"Config file already exists at {path}")
        print(f"Delete it first if you want to re-initialize.")
        return

    print("=" * 60)
    print("  Sheldon AI for ARK — Bridge Setup")
    print("=" * 60)
    print()

    # --- LLM Provider ---
    print("Which LLM provider do you want to use?")
    print()
    print("  1. OpenRouter  (recommended — 200+ models, one API key)")
    print("  2. Anthropic   (direct)")
    print("  3. OpenAI      (direct)")
    print("  4. Google      (Gemini)")
    print()

    provider_map = {
        "1": ("openrouter", "OPENROUTER_API_KEY", "openrouter/anthropic/claude-sonnet-4-20250514"),
        "2": ("anthropic", "ANTHROPIC_API_KEY", "anthropic/claude-sonnet-4-20250514"),
        "3": ("openai", "OPENAI_API_KEY", "openai/gpt-4o"),
        "4": ("gemini", "GOOGLE_API_KEY", "gemini/gemini-2.0-flash"),
    }

    choice = input("Choose [1-4, default=1]: ").strip() or "1"
    if choice not in provider_map:
        print(f"Invalid choice '{choice}', defaulting to OpenRouter.")
        choice = "1"

    provider, env_var, default_model = provider_map[choice]
    print(f"  → Provider: {provider}")
    print()

    # --- API Key ---
    print(f"Enter your API key (or press Enter to use ${{{env_var}}} env var):")
    api_key_input = input("API key: ").strip()

    if api_key_input:
        api_key = api_key_input
    else:
        api_key = f"${{{env_var}}}"
        print(f"  → Will read from {env_var} environment variable at runtime")
    print()

    # --- Generate shared secret ---
    secret = TokenAuthenticator.generate_secret()
    print(f"Generated shared secret for mod authentication.")
    print(f"You will need to add this to your ARK server's GameUserSettings.ini:")
    print()
    print(f"  [SheldonAI]")
    print(f"  WebSocketURL=wss://YOUR-BRIDGE-HOST:8443/sheldon")
    print(f"  AuthSecret={secret}")
    print()

    # --- Build config (everything else is defaults) ---
    config = {
        "llm": {
            "provider": provider,
            "model": default_model,
            "api_key": api_key,
        },
        "auth": {
            "shared_secret": secret,
        },
    }

    # Write config
    config_path.write_text(json.dumps(config, indent=2) + "\n")

    # Create directories
    Path("./data/custom").mkdir(parents=True, exist_ok=True)
    Path("./logs").mkdir(parents=True, exist_ok=True)

    print("=" * 60)
    print(f"  Config saved to: {config_path}")
    print()
    print("  To start the bridge:")
    print(f"    sheldon-bridge run")
    print()
    print("  All other settings use sensible defaults.")
    print("  See examples/config.example.json for advanced options.")
    print("=" * 60)
