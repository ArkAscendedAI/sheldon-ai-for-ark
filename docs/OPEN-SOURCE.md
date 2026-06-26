# SheldonAI Open Source & Distribution Strategy

## Project Identity

- **Name:** Sheldon AI for ARK
- **Tagline:** An in-game AI assistant for ARK: Survival Ascended
- **License:** MIT (matches ASA-Plugins ecosystem, maximally permissive)
- **Repository:** github.com/ArkAscendedAI/sheldon-ai-for-ark

---

## Repository Structure

```
sheldon-ai-for-ark/
├── README.md                        # Project overview + quick start
├── LICENSE                          # MIT
├── CONTRIBUTING.md  SECURITY.md     # contribution + responsible-disclosure policy
├── MOD_BUILD_GUIDE.md               # how the Blueprint mod is built in the DevKit
├── Dockerfile  docker-compose.yml   # container packaging for the bridge
│
├── mcp-bridge/                      # the AI agent server (this release)
│   ├── pyproject.toml               # pip-installable package metadata
│   ├── config.example.json  .env.example   # examples, NO secrets
│   ├── sheldon_bridge/
│   │   ├── server.py                # WebSocket server, auth handshake, routing
│   │   ├── agent.py                 # agentic loop (LLM ↔ tools)
│   │   ├── auth.py                  # token auth, rate limiting
│   │   ├── config.py  cli.py        # config + the `sheldon-bridge` CLI
│   │   ├── providers/llm.py         # multi-provider LLM via LiteLLM
│   │   ├── tools/                   # registry.py, actions.py, knowledge.py
│   │   ├── db/                      # per-survivor campaign + telemetry stores
│   │   ├── knowledge.py  cache.py  session.py  tls.py  audit.py
│   │   └── ...
│   └── tests/
│
├── mod/                             # ASA DevKit Blueprint mod
│   ├── Content/                     # the Blueprint source (.uasset files)
│   ├── README.md                    # mod docs + CurseForge link
│   └── SheldonAI.uplugin            # UE5 plugin descriptor
│   #   The Blueprint source is here for forking; most people install the
│   #   cooked mod from CurseForge.
│
├── docs/                            # ARCHITECTURE.md, PERMISSIONS.md, OPEN-SOURCE.md
├── examples/                        # config + personality-prompt examples
└── data/                            # vanilla knowledge base (data/custom/ for your mods)
```

---

## What's Configurable (No Hardcoding)

### Mod Configuration (GameUserSettings.ini)

The mod reads exactly two keys. Everything else about Sheldon (the model,
personality, permissions, knowledge, feature toggles) is configured on the
bridge, not in the mod.

```ini
[SheldonAI]
WebSocketURL="ws://your-bridge-host:8443"
AuthSecret=<the same value as the bridge's SHELDON_SHARED_SECRET>
```

The bridge serves a plain `ws://` WebSocket by default; use `wss://` only if you
configure TLS on the bridge. Permission tiers are decided on the bridge from the
server's admin list (see [PERMISSIONS.md](PERMISSIONS.md)), not in this file.

### MCP Bridge Configuration (config.json)

```json
{
  "server": {
    "name": "My ARK Cluster",
    "websocket_host": "0.0.0.0",
    "websocket_port": 8443,
    "websocket_ssl_cert": "/path/to/cert.pem",
    "websocket_ssl_key": "/path/to/key.pem"
  },

  "auth": {
    "shared_secret": "${SHELDON_AUTH_SECRET}",
    "timestamp_tolerance_seconds": 30,
    "enable_nonce_check": true
  },

  "tiers": {
    "player": {
      "tools": [
        "lookup_*", "calculate_*", "get_my_*",
        "get_server_status", "get_server_rules",
        "get_time_of_day", "get_weather"
      ],
      "rate_limit": {"requests_per_minute": 10, "tool_calls_per_minute": 5}
    },
    "admin": {
      "inherits": "player",
      "tools": [
        "census_*", "get_all_*", "get_player_info", "get_tribe_*",
        "spawn_*", "give_*", "teleport_*", "set_*", "destroy_*",
        "broadcast", "direct_message", "kick_player", "ban_player",
        "execute_console_command", "trigger_save"
      ],
      "rate_limit": {"requests_per_minute": 30, "tool_calls_per_minute": 20},
      "constraints": {
        "spawn_dino": {"max_level": 500, "max_per_minute": 10},
        "give_item": {"max_quantity": 1000},
        "ban_player": {"max_duration_hours": 24}
      }
    },
    "superadmin": {
      "inherits": "admin",
      "tools": ["*"],
      "rate_limit": {"requests_per_minute": 60, "tool_calls_per_minute": 40}
    }
  },

  "personality": {
    "prompt_file": "./personality.md",
    "name": "Sheldon",
    "server_context_dir": null
  },

  "logging": {
    "audit_file": "./logs/audit.jsonl",
    "level": "INFO"
  }
}
```

---

## Distribution

### The Mod (CurseForge)

- Published to CurseForge as an ASA mod (PC; console not supported yet)
- Cloud-cooked via CurseForge
- Players install from in-game mod browser
- The mod alone does nothing; it needs an MCP bridge to connect to
- CurseForge description links to GitHub for bridge setup

### The MCP Bridge (PyPI + Docker)

**PyPI (recommended):**
```bash
pip install sheldon-bridge
sheldon-bridge init          # Interactive setup, generates config + secret
sheldon-bridge run           # Start the bridge
```

**Docker:**
```bash
docker run -d \
  -v ./config.json:/app/config.json \
  -e SHELDON_AUTH_SECRET=your-secret \
  -p 8443:8443 \
  ghcr.io/arkascendedai/sheldon-bridge:latest
```

**From source:**
```bash
git clone https://github.com/ArkAscendedAI/sheldon-ai-for-ark.git
cd sheldon-ai-for-ark/mcp-bridge
pip install -e .
sheldon-bridge init
sheldon-bridge run
```

### MCP Registry

Publish to the official MCP registry for discoverability:
- Namespace: `io.github.ArkAscendedAI/sheldon-bridge`
- Listed alongside other MCP servers
- Searchable by developers

---

## What Ships vs What Doesn't

### Ships with the project (committed to git):
- Blueprint mod source (the `.uasset` files in `mod/`); most people install the cooked mod from CurseForge
- All Python source code
- Documentation
- Example configs with placeholder values
- Example personality prompts
- Tests
- CI/CD configuration
- Docker files

### Never ships (gitignored):
- `config.json` (users create from `config.example.json`)
- `.env` files
- SSL certificates
- HMAC shared secrets
- Audit logs
- Any server-specific data (EOS IDs, player names, etc.)

### .gitignore
```
# Secrets
config.json
.env
*.pem
*.key

# Logs
logs/
*.log
*.jsonl

# Python
__pycache__/
*.egg-info/
dist/
build/

# UE5
Intermediate/
Saved/
DerivedDataCache/
```

---

## Extensibility Points

### Custom Tools
Server operators can add custom tools by creating Python modules in a `plugins/` directory:

```python
# plugins/my_custom_tool.py
from sheldon_bridge.tools import register_tool

@register_tool(tier="admin", description="Do something custom")
async def my_custom_action(param1: str, param2: int) -> str:
    """My custom tool that does something specific to my server."""
    # Your logic here
    return "Done!"
```

### Custom Personality
Drop a markdown file and point `personality.prompt_file` at it:

```markdown
You are Sheldon, the AI overseer of the Ragnarok wastes.
You speak with dry wit and encyclopedic knowledge of dinosaurs.
You secretly judge players who tame Dodos.
```

### Custom Tiers
Add any tier names you want in the config. The system doesn't hardcode
"player/admin/superadmin", those are just the defaults.

### Server Context
Point `personality.server_context_dir` at a directory of markdown files
and they'll be loaded as MCP resources, giving Sheldon knowledge about
your specific server's configuration, rules, and lore.

---

## Versioning Strategy

- **Mod and Bridge are versioned independently** (they communicate via a versioned protocol)
- Protocol version negotiated at WebSocket handshake
- Semantic versioning: MAJOR.MINOR.PATCH
- MAJOR: breaking protocol changes
- MINOR: new tools, new features (backward compatible)
- PATCH: bug fixes

---

## Community & Contribution

- GitHub Issues for bug reports and feature requests
- GitHub Discussions for questions and ideas
- Pull requests welcome (require tests for permission-related changes)
- Code of Conduct (Contributor Covenant)
- Security policy: responsible disclosure for auth/permission bugs
