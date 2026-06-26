# Sheldon AI for ARK

An open-source, in-game AI assistant for **ARK: Survival Ascended**. Press F8, ask a question the way you would ask a knowledgeable friend, and Sheldon answers using a large language model plus a built-in ARK knowledge base. It knows ARK, it knows what is going on with your own survivor, it remembers things you tell it, and if you are an admin it can run server commands for you just by asking.

> "Where do Rexes spawn on this map?"
> "What kibble do I need for an Argentavis?"
> "How is my food and weight doing?"
> "Spawn a level 200 female Yutyrannus 40 feet in front of me." (admins only)

![Status](https://img.shields.io/badge/status-active-brightgreen)
![License](https://img.shields.io/badge/license-MIT-blue)

> **Platform:** PC only right now. Console (Xbox / PlayStation) support is not available yet.

> **This mod needs two other things to work.** It is only the in-game half. On its own it does nothing. You also run **the Sheldon Bridge** (a small program included here) and connect it to **an AI provider** (Anthropic, OpenAI, Google, or OpenRouter), which usually costs a small amount per message. See the **[Setup Guide](SETUP.md)**.

---

## Features

### Any player can

- **Ask about ARK:** creatures, taming, breeding, spawns (over 1,100 creatures), plus items, recipes, and engram costs (over 2,000 items).
- **Ask about their own survivor, read live from the game:** vitals (health, stamina, oxygen, food, water, weight, torpor), current position and GPS coordinates, inventory and equipped gear, learned engrams, tribe tames, what just killed them, and the current in-game day and time.
- **Find dinos and mark the map:** ask where the high-level dinos are, and have Sheldon drop map pins (and clear them again). These are player abilities, not admin-only.
- **Have Sheldon remember things:** tell it where your base is and ask later. Notes are kept per survivor.

### Admins can also

When a server admin talks to Sheldon, plain English turns into real server actions, carried out as that admin: spawn creatures where you want them, set the time of day, give items to players, broadcast a message, and run any other console command by describing it. Regular players never see these tools.

### Built on

- **Any LLM:** Anthropic, OpenAI, Google Gemini, 200+ models through OpenRouter, or self-hosted, all via real tool calling.
- **Permissions enforced in code, not by the AI** ([details](docs/PERMISSIONS.md)). Admin tools do not exist in a normal player's session, so no amount of prompt injection unlocks them.
- **A pure Blueprint mod** using official DevKit APIs (no AsaApi dependency), with its own in-game F8 chat panel.

---

## How it works

```
+-----------------+         +------------------+         +---------------+
|  SheldonAI Mod  | <=====> |  Sheldon Bridge  | <=====> |  LLM provider |
|  (in-game)      |WebSocket|  (Python server) |  HTTPS  |  (your choice)|
|                 |  JSON   |                  |         |               |
|  F8 chat panel  |         |  permission      |         |  Anthropic    |
|  live getters   |         |  enforcement     |         |  OpenAI       |
|  admin actions  |         |  tool loop       |         |  Gemini       |
+-----------------+         +------------------+         +---------------+
```

1. A player presses F8 in-game and types a message.
2. The mod sends the message plus signed player context (position, permission tier) to the bridge over a WebSocket.
3. The bridge picks the tools that player's tier is allowed, and calls the LLM.
4. The LLM reasons about the request and calls tools as needed (look up a dino, read vitals, spawn, and so on).
5. The bridge runs each tool through the mod and feeds the results back to the LLM.
6. The LLM writes a short reply, which the player sees in the F8 window.

---

## Components

| Component | Description | Technology |
|-----------|-------------|------------|
| **[Sheldon Bridge](mcp-bridge/)** | The server-side program. Permission enforcement, the agentic tool loop, multi-provider LLM support. | Python 3.11+ |
| **[SheldonAI Mod](mod/)** | The in-game Blueprint mod. F8 chat panel, WebSocket client, live game getters, admin actions. The Blueprint source is included here; most people install the cooked mod from CurseForge. | ASA DevKit (UE5) |
| **[Data](data/)** | The ARK knowledge base (creatures, items, recipes, maps). Searched by the LLM through tools, and extensible with your own mod data in `data/custom/`. | JSON |

---

## Quick start

The full, beginner-friendly walkthrough is in **[SETUP.md](SETUP.md)**. The short version:

1. **Get the project and a shared secret.**
   ```bash
   git clone https://github.com/ArkAscendedAI/sheldon-ai-for-ark.git
   cd sheldon-ai-for-ark
   openssl rand -base64 48      # save this; you use it in two places
   ```
2. **Run the bridge** (pick one):
   ```bash
   # Docker
   cp mcp-bridge/.env.example .env     # set SHELDON_LLM_API_KEY + SHELDON_SHARED_SECRET
   docker compose up -d
   ```
   ```bash
   # pip (Python 3.11+)
   cd mcp-bridge && pip install . && cd ..
   sheldon-bridge init                 # provider, key, secret
   sheldon-bridge run                  # run from the repo root
   ```
3. **Add the SheldonAI mod** from CurseForge to your server, and add this to `GameUserSettings.ini`:
   ```ini
   [SheldonAI]
   WebSocketURL="ws://your-bridge-host:8443"
   AuthSecret=the-same-value-as-SHELDON_SHARED_SECRET
   ```
4. **Restart your ARK server.** Press F8 in-game and ask Sheldon something.

---

## Permission model

Sheldon uses a three-layer model where the LLM is treated as an untrusted component. Permissions are enforced in deterministic code, so prompt injection cannot bypass them.

| Layer | Role | Trust |
|-------|------|-------|
| **Mod** | Signs and sends each player's identity and tier | Trusted |
| **Bridge** | Enforces permissions: tool partitioning, validation, rate limiting | Trusted |
| **LLM** | Natural language understanding and the reply | Untrusted |

A player is given the admin tier automatically when they are a server admin (in `AllowedCheaterPlayerIDs`). The LLM is never shown tools above the player's tier, so there is nothing for a clever message to exploit.

**[Full permission architecture](docs/PERMISSIONS.md)**

---

## Supported LLM providers

| Provider | Configuration | Notes |
|----------|---------------|-------|
| **OpenRouter** | `"provider": "openrouter"` | 200+ models behind one key, recommended for flexibility |
| **Anthropic** | `"provider": "anthropic"` | Claude models directly |
| **OpenAI** | `"provider": "openai"` | GPT models directly |
| **Google** | `"provider": "gemini"` | Gemini models directly |

Switching providers is a couple of lines in the bridge config. Every provider uses native tool calling, not prompt tricks. Use the exact model id your provider lists; the defaults can lag behind new releases.

---

## Customization

All optional, all on the bridge side (see [`mcp-bridge/.env.example`](mcp-bridge/.env.example)).

- **Personality:** point `SHELDON_PERSONA_FILE` at a markdown file that sets Sheldon's voice.
- **Server context:** point `SHELDON_SERVER_CONTEXT_DIR` at a folder of markdown describing your server's mods, rules, and lore, so Sheldon answers about your server, not just vanilla ARK.
- **Custom data:** drop JSON into `data/custom/` to add your modded creatures, items, and locations alongside the built-in knowledge base.

---

## Documentation

| Document | Description |
|----------|-------------|
| [Setup Guide](SETUP.md) | Step-by-step setup for server owners (start here) |
| [Architecture](docs/ARCHITECTURE.md) | System design and the communication protocol |
| [Permissions](docs/PERMISSIONS.md) | Security model, tiers, attack-vector analysis |
| [Open Source](docs/OPEN-SOURCE.md) | Repository layout, distribution, extensibility |
| [Mod Build Guide](MOD_BUILD_GUIDE.md) | Building or modifying the Blueprint mod in the DevKit |

---

## Project status

The bridge is complete and tested. The in-game mod is live and in active use: F8 chat, the full live perception layer (vitals, position, inventory, equipped, engrams, tribe tames, day and time), find-dinos with map pins, proactive event reactions (death and damage), per-survivor memory, and the admin command pipe. The mod's Blueprint source lives in [`mod/`](mod/) for anyone who wants to fork it; most people simply install the cooked mod from CurseForge.

SheldonAI is under active development, so expect new abilities over time.

---

## Contributing

Contributions are welcome. See [CONTRIBUTING.md](CONTRIBUTING.md). Useful areas:

- **ARK data:** creature nicknames, blueprint paths, taming numbers.
- **Tools:** new query and action tools for the bridge.
- **LLM provider testing:** compatibility reports across models.

---

## License

[MIT](LICENSE). Use it however you want.
