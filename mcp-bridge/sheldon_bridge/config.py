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

    # Persistence (telemetry + campaign DBs)
    data_root: str = "./data/db"
    default_cluster_id: str = "default"
    default_server_id: str = "default"
    server_ip_map: dict = field(default_factory=dict)  # remote IP -> {"server_id","cluster_id"}
    context_token_budget: int = 14000

    # Dino census: continuous background octree sweep keeping telemetry.db dino_index
    # fresh. Off by default — cook-gated (the mod's get_dino_scan branch must be live).
    census_enabled: bool = False

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
            f"To actually DO anything on the server (spawn a dino, give an item, change the "
            f"time, teleport, broadcast, run a console command) you MUST call the matching tool — "
            f"that is the ONLY thing that performs the action. NEVER claim or imply you did "
            f"something unless you actually called the tool this turn and it returned success; "
            f"saying you did an action you did not perform is a serious error. Confirm what you "
            f"did, conversationally, only AFTER the tool succeeds.\n\n"
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

    def build_base_system(self) -> str:
        """The static system-prompt base shared by every turn: persona + server
        context + the plain-text response-format rule. The per-survivor block
        (requester line, live telemetry, lorebook) is added by the assembler."""
        parts = [self.get_personality_prompt()]

        server_ctx = self.get_server_context()
        if server_ctx:
            parts.append(f"## Server Information\n\n{server_ctx}")

        parts.append(
            "## Response format\n\n"
            "You are replying inside a small in-game chat window with a tiny font and "
            "NO rich-text rendering. Reply in PLAIN TEXT only: no markdown, no '#' headers, "
            "no '*' or '**' bold/italic, no bullet or numbered lists, no code blocks, no "
            "tables, and no emoji or special unicode symbols. If you need to list steps, "
            "write them as a short flowing sentence or separate them with periods. Keep "
            "replies short — ideally 1-3 sentences — since the window is small."
        )
        return "\n\n".join(parts)

    def build_system_prompt(self, player_name: str, tier: str, tribe: str = "") -> str:
        """Full one-shot prompt (base + per-player block). Retained for the mock
        client and tests; the live path uses build_base_system + the assembler."""
        prompt_parts = [self.build_base_system()]

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


def _coerce_int(value, default: int, label: str) -> int:
    """Parse an int from env-or-config, WARN+fall back to default on a bad value
    (matches the bridge's warn-don't-crash-on-bad-config pattern)."""
    if value is None or value == "":
        return default
    try:
        return int(value)
    except (TypeError, ValueError):
        logger.warning(f"Invalid integer for {label}: {value!r} — using default {default}")
        return default


def _coerce_float(value, default: float, label: str) -> float:
    """Parse a float from env-or-config, WARN+fall back to default on a bad value."""
    if value is None or value == "":
        return default
    try:
        return float(value)
    except (TypeError, ValueError):
        logger.warning(f"Invalid number for {label}: {value!r} — using default {default}")
        return default


def load_config(path: str = DEFAULT_CONFIG_PATH) -> BridgeConfig:
    """Load configuration from a JSON file.

    Environment variables in the format ${VAR_NAME} are resolved at load time.
    """
    config_path = Path(path)
    # config.json is OPTIONAL — a deployment can be configured entirely via
    # SHELDON_* env vars (the Docker default). Env > config.json > defaults.
    raw = json.loads(config_path.read_text()) if config_path.exists() else {}

    def _env(name: str) -> str | None:
        v = os.environ.get(name)
        return v if v not in (None, "") else None

    llm_raw = raw.get("llm", {})
    auth_raw = raw.get("auth", {})
    server_raw = raw.get("server", {})
    personality_raw = raw.get("personality", {})
    data_raw = raw.get("data", {})
    logging_raw = raw.get("logging", {})
    persistence_raw = raw.get("persistence", {})

    # Default model per provider (override with SHELDON_LLM_MODEL — these may lag)
    default_models = {
        "openrouter": "openrouter/anthropic/claude-sonnet-4-20250514",
        "anthropic": "anthropic/claude-sonnet-4-20250514",
        "openai": "openai/gpt-4o",
        "gemini": "gemini/gemini-2.0-flash",
    }
    provider = _env("SHELDON_LLM_PROVIDER") or llm_raw.get("provider", "openrouter")

    llm_config = LLMConfig(
        provider=provider,
        model=_env("SHELDON_LLM_MODEL") or llm_raw.get("model") or default_models.get(provider, default_models["openrouter"]),
        api_key=_env("SHELDON_LLM_API_KEY") or _resolve_env_vars(llm_raw.get("api_key", "")),
        max_tokens=_coerce_int(_env("SHELDON_MAX_TOKENS") or llm_raw.get("max_tokens"), 4096, "SHELDON_MAX_TOKENS"),
        temperature=_coerce_float(_env("SHELDON_TEMPERATURE") or llm_raw.get("temperature"), 0.7, "SHELDON_TEMPERATURE"),
        timeout=_coerce_int(_env("SHELDON_LLM_TIMEOUT") or llm_raw.get("timeout"), 60, "SHELDON_LLM_TIMEOUT"),
        num_retries=_coerce_int(_env("SHELDON_LLM_RETRIES") or llm_raw.get("num_retries"), 2, "SHELDON_LLM_RETRIES"),
        # V18 Path A — cross-model failover chain (LiteLLM model strings, comma-sep env or a
        # config.json list). Tried in order after the primary exhausts its backoff.
        fallback_models=[m.strip() for m in (_env("SHELDON_LLM_FALLBACK_MODELS") or "").split(",") if m.strip()]
        or [str(m).strip() for m in (llm_raw.get("fallback_models") or []) if str(m).strip()],
    )

    shared_secret = _env("SHELDON_SHARED_SECRET") or _resolve_env_vars(auth_raw.get("shared_secret", ""))
    ip_map_env = _env("SHELDON_SERVER_IP_MAP")
    if ip_map_env:
        try:
            server_ip_map = json.loads(ip_map_env)
        except (ValueError, TypeError) as e:
            logger.warning(f"Invalid JSON in SHELDON_SERVER_IP_MAP — ignoring it: {e}")
            server_ip_map = persistence_raw.get("server_ip_map", {})
    else:
        server_ip_map = persistence_raw.get("server_ip_map", {})

    if not shared_secret:
        logger.warning("No shared secret (SHELDON_SHARED_SECRET / auth.shared_secret) — the mod cannot authenticate.")
    if not llm_config.api_key:
        logger.warning("No LLM API key (SHELDON_LLM_API_KEY / llm.api_key) — LLM calls will fail.")

    return BridgeConfig(
        llm=llm_config,
        websocket_host=_env("SHELDON_WS_HOST") or server_raw.get("websocket_host", "0.0.0.0"),
        websocket_port=_coerce_int(_env("SHELDON_WS_PORT") or server_raw.get("websocket_port"), 8443, "SHELDON_WS_PORT"),
        ssl_cert=_env("SHELDON_SSL_CERT") or server_raw.get("websocket_ssl_cert"),
        ssl_key=_env("SHELDON_SSL_KEY") or server_raw.get("websocket_ssl_key"),
        shared_secret=shared_secret,
        tiers=raw.get("tiers", {}),
        personality_name=_env("SHELDON_PERSONA_NAME") or personality_raw.get("name", "Sheldon"),
        personality_prompt_file=_env("SHELDON_PERSONA_FILE") or personality_raw.get("prompt_file"),
        server_context_dir=_env("SHELDON_SERVER_CONTEXT_DIR") or personality_raw.get("server_context_dir"),
        data_dirs=data_raw.get("dino_data_dirs", ["./data/vanilla", "./data/custom"]),
        audit_file=_env("SHELDON_AUDIT_FILE") or logging_raw.get("audit_file", "./logs/audit.jsonl"),
        log_level=_env("SHELDON_LOG_LEVEL") or logging_raw.get("level", "INFO"),
        max_tool_iterations=_coerce_int(_env("SHELDON_MAX_TOOL_ITERATIONS") or llm_raw.get("max_tool_iterations"), 25, "SHELDON_MAX_TOOL_ITERATIONS"),
        data_root=_env("SHELDON_DATA_ROOT") or persistence_raw.get("data_root", "./data/db"),
        default_cluster_id=_env("SHELDON_DEFAULT_CLUSTER_ID") or persistence_raw.get("default_cluster_id", "default"),
        default_server_id=_env("SHELDON_DEFAULT_SERVER_ID") or persistence_raw.get("default_server_id", "default"),
        server_ip_map=server_ip_map,
        context_token_budget=_coerce_int(_env("SHELDON_TOKEN_BUDGET") or persistence_raw.get("context_token_budget"), 14000, "SHELDON_TOKEN_BUDGET"),
        census_enabled=str(_env("SHELDON_CENSUS_ENABLED") or persistence_raw.get("census_enabled", "")).lower() in ("1", "true", "yes", "on"),
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
    print(f'  WebSocketURL="ws://YOUR-BRIDGE-HOST:8443"')
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
