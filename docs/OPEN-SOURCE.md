# SheldonAI — Open Source & Distribution Strategy

## Project Identity

- **Name:** Sheldon AI for ARK
- **Tagline:** An in-game AI assistant for ARK: Survival Ascended
- **License:** MIT (matches ASA-Plugins ecosystem, maximally permissive)
- **Repository:** github.com/[TBD]/sheldon-ai-for-ark (monorepo)

---

## Repository Structure

```
sheldon-ai-for-ark/
├── README.md                       # Project overview, screenshots, quick start
├── LICENSE                         # MIT
├── CONTRIBUTING.md                 # How to contribute
├── CHANGELOG.md                    # Version history
│
├── mod/                            # ASA DevKit Blueprint mod
│   ├── README.md                   # Mod-specific docs, CurseForge link
│   ├── SheldonAI.uplugin           # UE5 plugin descriptor
│   └── Content/                    # Blueprint assets (binary, not human-editable)
│       ├── SheldonAI_GameMode.uasset
│       ├── SheldonAI_CCA.uasset    # Custom Cosmetics Actor (server-side logic)
│       ├── SheldonAI_Buff.uasset   # Player buff (client-side logic)
│       ├── Widgets/                # UMG Widget Blueprints
│       │   ├── W_SheldonChat.uasset
│       │   ├── W_SheldonChatEntry.uasset
│       │   └── W_SheldonNotification.uasset
│       ├── Sounds/                 # Notification sounds
│       └── Textures/               # Icons, UI assets
│
├── mcp-bridge/                     # MCP Bridge server (standalone, pip-installable)
│   ├── README.md                   # Bridge-specific docs
│   ├── pyproject.toml              # Python package metadata (pip installable)
│   ├── config.example.json         # Example config (NO secrets)
│   ├── sheldon_bridge/
│   │   ├── __init__.py
│   │   ├── __main__.py             # Entry point
│   │   ├── server.py               # MCP server + WebSocket handler
│   │   ├── permissions.py          # Tier definitions, tool registry
│   │   ├── session.py              # Session isolation, HMAC validation
│   │   ├── tools/                  # Tool implementations
│   │   │   ├── __init__.py
│   │   │   ├── communication.py    # reply, broadcast, direct_message
│   │   │   ├── queries.py          # census, player info, world state
│   │   │   ├── actions.py          # spawn, give, teleport, set_time
│   │   │   ├── knowledge.py        # dino lookup, recipes, map info
│   │   │   └── admin.py            # server ops, config, permissions
│   │   ├── protocol.py             # WebSocket message protocol
│   │   ├── rate_limiter.py         # Per-tier, per-tool rate limiting
│   │   └── audit.py                # Structured audit logging
│   └── tests/
│       ├── test_permissions.py     # Permission enforcement tests
│       ├── test_hmac.py            # Signature validation tests
│       ├── test_session.py         # Session isolation tests
│       ├── test_rate_limiter.py    # Rate limiting tests
│       └── test_tools.py           # Tool validation tests
│
├── docs/
│   ├── ARCHITECTURE.md             # System architecture overview
│   ├── PERMISSIONS.md              # Permission model deep-dive
│   ├── OPEN-SOURCE.md              # This file
│   ├── SETUP-GUIDE.md              # Step-by-step deployment guide
│   ├── CONFIGURATION.md            # All config options documented
│   ├── TOOL-REFERENCE.md           # Every tool, parameters, tiers
│   ├── PROTOCOL.md                 # WebSocket message format spec
│   └── MODDING-GUIDE.md            # How to extend/customize
│
└── examples/
    ├── gameusersettings.example.ini # Example mod INI config
    └── personality-prompts/        # Example Sheldon personality configs
        ├── default.md              # Friendly, knowledgeable assistant
        ├── sarcastic.md            # Sarcastic Sheldon (Big Bang Theory style)
        └── roleplay-npc.md         # Immersive NPC character
```

---

## What's Configurable (No Hardcoding)

### Mod Configuration (GameUserSettings.ini)

Everything server-specific lives in INI, not in Blueprint code:

```ini
[SheldonAI]
# Connection
WebSocketURL=wss://your-devops-server:8443/sheldon
AuthSecret=<your-generated-hmac-secret>
ServerName=MyServer

# UI
EnableUI=true
UIKeybind=F8
UITitle=Sheldon AI
UISubtitle=Your AI Assistant
# UIColor=0.2,0.6,1.0,1.0    # Optional: custom accent color (RGBA 0-1)

# Chat fallback (optional, requires admin for ScriptCommand)
EnableChatCommands=false
ChatPrefix=/sheldon

# Permissions
DefaultTier=player
AdminTierFromGameAdmins=true

# Feature toggles
EnableEventNotifications=true
EnablePlayerWelcome=true
WelcomeMessage=Welcome to the server! Press F8 to talk to Sheldon.

[SheldonAI.Permissions]
# EOS ID → tier overrides
# 00012345abcdef=superadmin
# 00067890fedcba=admin
```

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

- Published to CurseForge as a cross-platform ASA mod
- Cloud-cooked for PC, Xbox, PS5
- Players install from in-game mod browser
- The mod alone does nothing — it needs an MCP bridge to connect to
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
  ghcr.io/[org]/sheldon-bridge:latest
```

**From source:**
```bash
git clone https://github.com/[org]/sheldon-ai.git
cd sheldon-ai/mcp-bridge
pip install -e .
sheldon-bridge init
sheldon-bridge run
```

### MCP Registry

Publish to the official MCP registry for discoverability:
- Namespace: `io.github.[org]/sheldon-bridge`
- Listed alongside other MCP servers
- Searchable by developers

---

## What Ships vs What Doesn't

### Ships with the project (committed to git):
- All Blueprint assets (binary .uasset files)
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
"player/admin/superadmin" — those are just the defaults.

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
