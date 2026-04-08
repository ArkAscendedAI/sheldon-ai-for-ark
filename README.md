# Sheldon AI for ARK

An open-source, in-game AI assistant for **ARK: Survival Ascended**. Give every player on your server access to an intelligent assistant that answers questions, provides ARK knowledge, and — for admins — executes server commands through natural language.

> **"Hey Sheldon, where do Rexes spawn on Ragnarok?"**
> **"Spawn a level 200 female Yutyrannus 40 feet in front of me."**
> **"What kibble do I need for an Argentavis?"**

![Status](https://img.shields.io/badge/status-in%20development-orange)
![License](https://img.shields.io/badge/license-MIT-blue)

---

## Features

### For Players (No Admin Required)
- **ARK Encyclopedia** — dino stats, spawn locations, taming strategies, breeding info
- **Taming & Breeding Calculators** — "How much mutton for a level 150 Rex?"
- **Crafting Recipes** — ingredients, workstations, engram requirements
- **Map Navigation** — "Where's the nearest metal-rich area?"
- **Personal Tame Tracking** — "Where's my Argentavis?"
- **Server Info** — rates, rules, mods, online players

### For Admins (Tier-Based Permissions)
- **Natural Language Server Control** — "Make it morning", "Spawn me a Rex"
- **World Queries** — "How many wild Rexes on the map?"
- **Player Management** — teleport, give items, kick, ban
- **Dino Spawning** — species, level, gender, position — all from plain English
- **Broadcasts** — "Tell everyone the server restarts in 15 minutes"

### Architecture Highlights
- **Multi-LLM Support** — Anthropic, OpenAI, Google Gemini, or any model via OpenRouter
- **Inviolable Permission System** — enforced in deterministic code, not by the LLM ([details](docs/PERMISSIONS.md))
- **No AsaApi Dependency** — pure Blueprint mod using official DevKit APIs
- **Cross-Platform** — PC, Xbox, PS5 via CurseForge cloud cooking
- **Custom UI** — dedicated in-game chat panel (F8), no admin required

---

## How It Works

```
┌─────────────────┐         ┌──────────────────┐         ┌───────────────┐
│  SheldonAI Mod  │◄═══════►│  Sheldon Bridge  │◄═══════►│  LLM Provider │
│  (in-game)      │WebSocket│  (Python server) │  HTTPS  │  (your choice)│
│                 │  JSON   │                  │         │               │
│  Custom UI      │         │  Permission      │         │  Any LLM      │
│  Game queries   │         │  enforcement     │         │  GPT-4o       │
│  Event hooks    │         │  Agentic loop    │         │  Gemini       │
│  Admin commands │         │  Tool registry   │         │  OpenRouter   │
└─────────────────┘         └──────────────────┘         └───────────────┘
```

1. Player presses **F8** in-game, types a message
2. Mod sends the message + player context (position, permissions) to the Bridge via WebSocket
3. Bridge verifies permissions, selects tier-appropriate tools, sends to LLM
4. LLM reasons about the request, calls tools as needed (lookup dinos, spawn, teleport, etc.)
5. Bridge executes tools via the mod, feeds results back to the LLM
6. LLM generates a natural language response
7. Player sees the response in-game with rich formatting

---

## Components

| Component | Description | Technology |
|-----------|-------------|------------|
| **[Sheldon Bridge](mcp-bridge/)** | Standalone AI agent server. Permission enforcement, agentic loop, multi-provider LLM support. | Python 3.12+ |
| **[SheldonAI Mod](mod/)** | In-game Blueprint mod. Custom UI, WebSocket client, game queries, command execution. | ASA DevKit (UE5) |
| **[Data](data/)** | ARK knowledge base — dinos, items, recipes, maps. Queryable by the LLM via tools. | JSON |

---

## Quick Start

### 1. Install the Bridge

```bash
pip install sheldon-bridge
```

Or with Docker:

```bash
docker pull ghcr.io/arkascendedai/sheldon-ai-for-ark:latest
```

### 2. Configure

```bash
sheldon-bridge init
```

Interactive setup asks only two things — your LLM provider and API key. Everything else has sensible defaults. The generated `config.json` is minimal:

```json
{
  "llm": {
    "provider": "openrouter",
    "api_key": "your-api-key-here"
  },
  "auth": {
    "shared_secret": "auto-generated-during-init"
  }
}
```

See `examples/config.advanced.json` for all available options.

### 3. Install the Mod

Subscribe to **SheldonAI** on [CurseForge](#) and add it to your server's mod list.

Add to your `GameUserSettings.ini`:

```ini
[SheldonAI]
WebSocketURL=wss://your-server:8443/sheldon
AuthSecret=your-generated-secret
```

### 4. Run

```bash
sheldon-bridge run
```

---

## Permission System

Sheldon uses a **three-layer permission model** where the LLM is treated as an untrusted component. Permissions are enforced in deterministic code — no amount of prompt injection or social engineering can bypass them.

| Layer | Role | Trust Level |
|-------|------|-------------|
| **Mod** | Identity attestation (HMAC-signed player context) | Trusted |
| **Bridge** | Permission enforcement (tool partitioning, validation, rate limiting) | Trusted |
| **LLM** | Natural language understanding and UX | Untrusted |

The LLM never sees tools above the player's permission tier. Admin tools don't exist in a regular player's session — there's nothing to exploit.

**[Full Permission Architecture →](docs/PERMISSIONS.md)**

---

## Supported LLM Providers

| Provider | Configuration | Notes |
|----------|--------------|-------|
| **OpenRouter** | `"provider": "openrouter"` | 200+ models, pay-per-token, recommended for flexibility |
| **Anthropic** | `"provider": "anthropic"` | Anthropic models directly |
| **OpenAI** | `"provider": "openai"` | GPT-4o, GPT-4 Turbo |
| **Google** | `"provider": "gemini"` | Gemini 2.0 Flash/Pro |

Swap providers by changing two fields in your config. All providers use native tool/function calling — no prompt-based hacks.

---

## Customization

### Personality

Create a `personality.md` file with your assistant's character:

```markdown
You are a helpful, knowledgeable ARK assistant. You speak with
enthusiasm about dinosaurs and prehistoric survival.
```

Or go full character:

```markdown
You are Sheldon, a sardonic AI who judges players for their
questionable taming decisions but always helps them anyway.
```

### Server Context

Drop markdown files into your `server-context/` directory to give the AI knowledge about your specific server — mods, rules, custom configurations, lore. The bridge loads these at startup.

### Custom Data

Add JSON files to `data/custom/` for mod-specific dinos, items, or locations. The lookup tools automatically search custom data alongside vanilla.

---

## Documentation

| Document | Description |
|----------|-------------|
| [Architecture](docs/ARCHITECTURE.md) | System design, communication protocol, component overview |
| [Permissions](docs/PERMISSIONS.md) | Security model, tier enforcement, attack vector analysis |
| [Open Source](docs/OPEN-SOURCE.md) | Repository structure, distribution, extensibility |
| [Configuration](docs/CONFIGURATION.md) | All configuration options (coming soon) |
| [Tool Reference](docs/TOOL-REFERENCE.md) | Every tool, parameters, tiers (coming soon) |
| [Setup Guide](docs/SETUP-GUIDE.md) | Step-by-step deployment (coming soon) |

---

## Project Status

This project is in **active development**. The bridge server is functional and tested end-to-end. The in-game mod is next.

### Bridge Server
- [x] Architecture design and documentation
- [x] Permission model (tier-based tool partitioning, 55 tests passing)
- [x] Token authentication and rate limiting
- [x] Tool registry with wildcard patterns and inheritance
- [x] Agentic loop (LLM → tool calls → execute → respond)
- [x] WebSocket server with session management
- [x] LLM provider abstraction (Anthropic, OpenAI, Google, OpenRouter via LiteLLM)
- [x] Knowledge base: 1,119 dinos (686 vanilla + 433 Primal Nemesis), 2,090 items, 618 engrams, 7 spawn maps
- [x] Fuzzy search with nickname/alias support (rapidfuzz)
- [x] Semantic cache with local embeddings (zero-cost, 5ms lookups)
- [x] Audit logging (JSONL)
- [x] Cache warm-up with common ARK Q&A
- [x] End-to-end integration tests (4/4 passing with real LLM)
- [x] Docker packaging
- [x] GitHub Actions CI

### In-Game Mod
- [ ] DevKit project setup
- [ ] WebSocket client (BPSecureNetworking)
- [ ] Custom UI (UMG Widget — chat panel)
- [ ] Game queries (player position, dino census, admin status)
- [ ] Event hooks (player join/leave, tame, death)
- [ ] CurseForge upload and cloud cooking

### Distribution
- [ ] PyPI package
- [ ] Docker Hub / GHCR image
- [ ] CurseForge mod listing

---

## Contributing

Contributions are welcome! See [CONTRIBUTING.md](CONTRIBUTING.md) for guidelines.

Priority areas:
- **ARK data** — dino nicknames, blueprint paths, taming data
- **Tool implementations** — new query and action tools
- **LLM provider testing** — compatibility reports across models
- **Cross-platform testing** — Xbox/PS5 mod behavior

---

## License

[MIT](LICENSE) — use it however you want.
