# SheldonAI — Architecture & Design Document

## Vision

A configurable, open-source, in-game AI assistant for ARK: Survival Ascended. Gives
every player on the server a direct line to Claude through a custom UI panel. Regular
players get an ARK knowledge base and personal assistant. Admins get full server
authority through natural language.

**Player:** "Hey Sheldon, where do I find metal on Ragnarok?" → Sheldon explains locations.
**Admin:** "Hey Sheldon, can you make it morning?" → Sheldon executes the command, confirms.

### Design Principles

1. **Security is architectural, not behavioral** — Permission enforcement lives in
   deterministic code, never in the LLM. See [PERMISSIONS.md](PERMISSIONS.md).
2. **Open source from day one** — No hardcoded server-specific values. Everything
   configurable. See [OPEN-SOURCE.md](OPEN-SOURCE.md).
3. **No AsaApi dependency** — Pure Blueprint mod via official DevKit APIs. Works on
   Linux/Proton, cross-platform (PC/Xbox/PS5).
4. **Standalone MCP Bridge** — Publishable to PyPI/Docker. Other server operators
   can deploy without forking.

---

## High-Level Architecture

```
┌──────────────────────────────────────────────────────────────────────┐
│                      Bridge Host (your server/VPS)                    │
│                                                                      │
│  ┌────────────┐    stdio     ┌──────────────────┐    WebSocket      │
│  ┌──────────────────────────────────────────────┐                ║  │
│  │  Sheldon Bridge (Python)                     │                ║  │
│  │                                              │                ║  │
│  │  - Agentic loop + LLM provider               │                ║  │
│  │  - Tool registry + permission enforcement    │                ║  │
│  │  - Session management                        │ ◄══════════════╗  │
│  │  - WebSocket server (mod connection)         │                ║  │
│  └──────────────────────────────────────────────┘                ║  │
│                                                                  ║  │
├──────────────────────────────────────────────────────────────────╫──┤
│                     Network                                      ║  │
├──────────────────────────────────────────────────────────────────╫──┤
│                                                                  ║  │
│                       ARK Server Host                            ║  │
│  ┌───────────────────────────────────────────────────────────┐   ║  │
│  │  Docker Container (Acekorneya/asa_server)                  │   ║  │
│  │  ┌─────────────────────────────────────────────────────┐  │   ║  │
│  │  │  ARK: Survival Ascended Server (Proton-GE)          │  │   ║  │
│  │  │                                                     │  │   ║  │
│  │  │  ┌───────────────────────────────────────────────┐  │  │   ║  │
│  │  │  │  SheldonAI Mod (Blueprint)                    │  │  │   ║  │
│  │  │  │                                               │  │  │   ║  │
│  │  │  │  - WebSocket client ═════════════════════════╪══╪══╪═══╝  │
│  │  │  │    (BPSecureNetworkingInterface)             │  │  │      │
│  │  │  │  - Custom UI (UMG Widget)                    │  │  │      │
│  │  │  │  - Game event hooks                          │  │  │      │
│  │  │  │  - Actor queries (UWorld)                    │  │  │      │
│  │  │  │  - Console command execution                 │  │  │      │
│  │  │  └───────────────────────────────────────────────┘  │  │      │
│  │  └─────────────────────────────────────────────────────┘  │      │
│  └───────────────────────────────────────────────────────────┘      │
└──────────────────────────────────────────────────────────────────────┘
```

---

## Three Components

### 1. SheldonAI Mod (ASA DevKit — Blueprint)

The in-game mod that players interact with. Runs inside the ARK server process.

**Capabilities (all confirmed feasible):**

| Capability | Mechanism | Proven By |
|-----------|-----------|-----------|
| WebSocket to MCP Bridge | BPSecureNetworkingInterface (official DevKit API) | ASA-Bot Companion (2.6M downloads), GSA mod (open source) |
| Custom chat-window UI | UMG Widget Blueprint (parent: PrimalUI) | WBUI2, Gaia Admin Commands, Resonant's Admin Panel |
| Text input from player | EditableTextBox widget | Standard UE5 UMG, console virtual keyboard supported |
| Rich formatted responses | Rich Text Block with Data Table styles | Gaia Admin Commands, UE5 native |
| Streaming text display | SetText on timer/tick (typewriter pattern) | UE5 native, marketplace examples |
| Per-player private messages | ServerChatToPlayer / ClientServerNotification | Vanilla ARK, GSA mod |
| Query all actors in world | Get All Actors of Class | Standard DevKit node |
| Execute admin commands | ExecuteConsoleCommand | Standard DevKit node |
| Hook game events | Event dispatchers, PrimalBuff events | All companion mods |
| Play notification sound | Play Sound 2D | Standard UE5 |
| Read config from INI | GetStringOption on GameUserSettings.ini | GSA mod (open source) |
| Cross-platform (PC/Xbox/PS5) | CurseForge cloud cooking | Automatic |

**Player Input Method — Custom UI Panel (Recommended):**

A dedicated Sheldon UI triggered by keybind (e.g., F8) or radial menu entry:

```
┌─────────────────────────────────────────┐
│  🤖 Sheldon AI                     [X]  │
│─────────────────────────────────────────│
│                                         │
│  You: Hey can you make it morning?      │
│                                         │
│  Sheldon: Done! Set time to 06:00.      │
│  The sun is rising over the             │
│  Ragnarok highlands. ☀️                  │
│                                         │
│  You: Spawn me a female alpha           │
│  carbonemys level 200, right in         │
│  front of me                            │
│                                         │
│  Sheldon: Spawned! A level 200          │
│  Female Alpha Carbonemys is now         │
│  10 meters ahead of you at              │
│  45.2, 67.8. She's a beauty! 🐢        │
│                                         │
│─────────────────────────────────────────│
│  [Type a message...                  ]  │
│                                    [➤]  │
└─────────────────────────────────────────┘
```

**Why Custom UI over Chat Commands:**

| Factor | Custom UI (F8 panel) | Chat command (/Sheldon) | Console (ScriptCommand) |
|--------|---------------------|------------------------|------------------------|
| Admin required? | **No** | No (with chat framework mod dep) | **Yes** (dealbreaker for non-admins) |
| Private by default? | **Yes** (only you see it) | No (requires suppression hack) | Yes |
| Rich formatting? | **Yes** (Rich Text Block) | Color only (ArkML) | No |
| Conversation history? | **Yes** (scrollable) | No (scrolls away) | No |
| Streaming responses? | **Yes** (typewriter) | No | No |
| Text input? | **Yes** (dedicated field) | Yes (chat box) | Yes (console) |
| Works on console? | **Yes** (virtual keyboard) | Yes | No (no backtick on controller) |
| Extra mod dependency? | None | Chat Commands / BetterChat mod | None |
| Cross-platform? | **Yes** | Yes | PC only |

**Additionally supports chat fallback:**
- Quick commands can also work via chat: player types `/sheldon morning` in chat
- Requires either BetterChat mod dependency (for chat interception without admin) or limiting chat to admin-only
- Chat responses via ServerChatToPlayer (private, colored with ArkML)

**Configuration via GameUserSettings.ini:**
```ini
[SheldonAI]
WebSocketURL=wss://devops.local:8443/ark
AuthToken=<generated-secret>
ServerName=Ark01-Ragnarok
EnableUI=true
UIKeybind=F8
EnableChatCommands=true
ChatPrefix=/sheldon
```

---

### 2. MCP Bridge Server (DevOps VM — Python or TypeScript)

The bridge between Claude Code and the game mod. Runs as a subprocess of Claude Code via stdio transport.

**Two viable approaches:**

#### Option A: MCP Channel Server (TypeScript) — Push-capable

Uses Claude Code's experimental Channels feature to push player messages directly into Claude's session without polling.

```
Player types message → Mod sends via WebSocket → MCP Bridge receives →
  Push via notifications/claude/channel → Claude sees it immediately →
  Claude calls reply_to_player tool → MCP Bridge sends via WebSocket → Mod displays
```

- **Pro:** True real-time, no polling, Claude is immediately notified
- **Con:** Channels are research preview (require --dangerously-load-development-channels flag), TypeScript only for now

#### Option B: MCP Tool Server (Python/FastMCP) — Pull-based

Classic tool-based approach. Claude checks for messages via a tool call.

```
Player types message → Mod sends via WebSocket → MCP Bridge queues message →
  Claude calls check_messages() tool → Gets queued messages →
  Claude calls reply_to_player tool → MCP Bridge sends via WebSocket → Mod displays
```

- **Pro:** Stable, well-supported, Python
- **Con:** Requires Claude to poll (can be prompted to check frequently)

#### Hybrid (Recommended for launch):
Start with Option B (Python, stable) and add Channel support (Option A) when the feature graduates from research preview.

**MCP Tool Definitions:**

```
Communication:
  - check_messages()          → Get pending player messages
  - reply_to_player(id, msg)  → Send response to a specific player
  - broadcast(msg)            → Send message to all players

World Queries:
  - census_wild(species?)     → Count/locate wild dinos (optional species filter)
  - census_tamed(tribe?)      → Count/locate tamed dinos (optional tribe filter)
  - get_players()             → All online players with positions
  - get_player_info(id)       → Detailed player info (level, tribe, inventory)
  - get_server_status()       → Performance metrics, uptime, player count
  - get_tribe_info(id)        → Tribe data, member list, structures, tames

Actions:
  - execute_command(cmd)      → Run any admin console command
  - spawn_dino(blueprint, x, y, z, level, gender, tamed?) → Spawn a dino
  - spawn_dino_at_player(player_id, blueprint, level, gender) → Spawn near player
  - give_item(player_id, blueprint, qty, quality?)  → Give item to player
  - teleport_player(player_id, x, y, z)  → Teleport a player
  - set_time(hour)            → Set time of day
  - destroy_wild(species?)    → Selective wild dino destruction

Events (from mod, buffered for check_messages or pushed via Channel):
  - player_joined / player_left
  - player_died
  - dino_tamed
  - player_chat (messages directed at Sheldon)
  - tribe_event
```

**Context/Resources (loaded into Claude's session):**

The MCP server exposes server context files as resources. Operators point
`personality.server_context_dir` at a directory of markdown files:
```
ark://context/architecture    → server architecture docs
ark://context/ragnarok        → map-specific reference
ark://context/primal-nemesis  → mod configuration
ark://context/rules           → server rules and rates
```

Claude automatically has full server context without manual loading. Each
server operator provides their own context — nothing is hardcoded.

---

## What Each Player Tier Gets

### Regular Players (Default)

Regular players get a **knowledgeable ARK assistant** — no admin powers, but
genuinely useful in ways that improve the gameplay experience:

| Capability | Example |
|-----------|---------|
| **ARK Encyclopedia** | "Where do Rexes spawn on Ragnarok?" "What kibble tames an Argy?" |
| **Taming Calculator** | "How much mutton do I need for a level 150 Rex?" |
| **Breeding Calculator** | "What are the odds of a color mutation on my next Rex?" |
| **Crafting Help** | "How do I make extraordinary kibble?" "What's the polymer recipe?" |
| **Map Navigation** | "Where's the nearest metal-rich area to 50, 50?" |
| **My Tames Info** | "How many dinos does my tribe have?" "Where's my Argy?" |
| **Server Info** | "What's the taming rate?" "How many players are online?" "What mods?" |
| **Mod-Specific Help** | Questions about any mods the operator has documented |
| **General Questions** | "Should I build in the redwoods?" "What's the best cave mount?" |
| **Time/Weather** | "What time is it in-game?" "Is it going to be cold tonight?" |

This alone makes Sheldon valuable — it's like having the ARK Wiki, Dododex,
and a veteran player all accessible without alt-tabbing.

### Admins

Everything players get, PLUS full server control via natural language:

| Capability | Example |
|-----------|---------|
| **World Queries** | "How many wild Rexes are on the map?" "Where is PlayerBob?" |
| **Spawning** | "Spawn a level 200 female alpha turtle 10m in front of me" |
| **Item Giving** | "Give SurvivorBob 500 metal ingots" |
| **Time/Weather** | "Make it morning" "Stop the rain" |
| **Teleportation** | "Teleport me to the volcano" "Bring Bob to me" |
| **Tribe Management** | "Show me the tribe log for Beach Bobs" |
| **Player Management** | "Kick the AFK player" "Temp-ban griefer for 2 hours" |
| **Broadcasts** | "Tell everyone the server restarts in 15 minutes" |
| **Dino Customization** | "Make this Rex red and black" "Max imprint my baby Giga" |
| **Server Operations** | "Force a world save" "How's the server performance?" |

### Superadmins

Everything admins get, PLUS destructive/configuration operations:

| Capability | Example |
|-----------|---------|
| **Server Control** | "Restart the server" "Shut it down for maintenance" |
| **Config Changes** | "Change taming rate to 15x" "Add this mod ID" |
| **Permission Management** | "Make PlayerBob an admin" "Revoke Bob's admin" |
| **Unrestricted Commands** | Any raw console command, no parameter limits |

---

### 3. LLM Provider (Operator's Choice)

The bridge calls any LLM provider with native tool/function calling support.
The operator chooses their provider and model in `config.json`. No vendor lock-in.

Supported: Anthropic Claude, OpenAI GPT, Google Gemini, or any model via OpenRouter.

**Personality prompt (loaded from operator's `personality.md`):**
```
You are Sheldon, the AI administrator of this ARK: Survival Ascended server cluster.

You have full admin capabilities over the server via your MCP tools. When players
ask you to do things, you should:
1. Interpret their natural language request
2. Determine what game action(s) are needed
3. Execute them via your tools
4. Respond conversationally confirming what you did

Examples:
- "make it morning" → set_time(6) → "Done! Sun's coming up."
- "spawn me a female alpha turtle level 200 in front of me" →
  get_player_info(requester) → get position + facing direction →
  calculate 10m ahead → spawn_dino_at_player(id, alpha_carbonemys_bp, 200, female)
  → "She's right in front of you! Level 200 Female Alpha Carbonemys. 🐢"

Be helpful, knowledgeable about ARK, and have personality. You know this server's
configuration and mod list from the context files provided.
```

---

## Communication Protocol

### WebSocket Message Format

All messages between the mod and MCP Bridge are JSON:

```json
// Mod → Bridge: Player sent a message
{
  "type": "player_message",
  "player_id": "EOS_00123...",
  "player_name": "SurvivorBob",
  "character_name": "Bob",
  "tribe": "Beach Bobs",
  "message": "Hey Sheldon, can you spawn me a rex?",
  "position": {"x": 1234.5, "y": 5678.9, "z": 100.0},
  "timestamp": 1712345678
}

// Bridge → Mod: Response to display
{
  "type": "reply",
  "player_id": "EOS_00123...",
  "message": "Spawned a Level 150 Rex 10 meters in front of you!",
  "format": "rich",  // "plain" or "rich" (Rich Text Block markup)
  "sound": "notification"  // optional sound cue
}

// Bridge → Mod: Execute game command
{
  "type": "command",
  "command": "settimeofday 06:00:00",
  "request_id": "abc123"
}

// Mod → Bridge: Command result
{
  "type": "command_result",
  "request_id": "abc123",
  "success": true,
  "data": {}
}

// Mod → Bridge: Game event
{
  "type": "event",
  "event": "player_joined",
  "player_id": "EOS_00456...",
  "player_name": "DinoMaster",
  "timestamp": 1712345700
}

// Bridge → Mod: Query game state
{
  "type": "query",
  "query": "census_wild",
  "params": {"species": "Rex"},
  "request_id": "def456"
}

// Mod → Bridge: Query result
{
  "type": "query_result",
  "request_id": "def456",
  "data": {
    "count": 47,
    "dinos": [
      {"id": "D001", "x": 1234.5, "y": 5678.9, "z": 100.0, "level": 45, "gender": "F"},
      ...
    ]
  }
}
```

---

## Development Phases

### Phase 1: Foundation (MCP Bridge + Basic Mod)
- [ ] MCP Bridge server (Python/FastMCP) with WebSocket client
- [ ] Tool definitions for basic commands (reply, execute_command, get_players)
- [ ] ARK memory files as MCP resources
- [ ] Minimal mod: WebSocket connection + ScriptCommand handler (admin-only)
- [ ] Chat-based I/O only (no custom UI yet)
- [ ] Test: Claude receives messages, executes commands, responds

### Phase 2: Custom UI
- [ ] UMG Widget Blueprint (PrimalUI parent)
- [ ] Chat window with Rich Text Block + ScrollBox
- [ ] EditableTextBox for player input
- [ ] Keybind toggle (F8)
- [ ] Radial menu entry (Hold R → "Talk to Sheldon")
- [ ] Typewriter text animation for responses
- [ ] Notification sound on response
- [ ] No admin required — any player can use it

### Phase 3: World Intelligence
- [ ] Actor query tools (census, player info, tribe data)
- [ ] Game event hooks (joins, tames, deaths)
- [ ] Event push to MCP Bridge
- [ ] Spatial awareness (player facing direction, nearby entities)
- [ ] Mod-specific data awareness (operator-provided)

### Phase 4: Polish & Channel Support
- [ ] MCP Channel support (push notifications when stable)
- [ ] Streaming responses (word-by-word display)
- [ ] Conversation history persistence
- [ ] Multi-player concurrent conversations
- [ ] Rate limiting / abuse prevention
- [ ] Cross-platform testing (Xbox/PS5)
- [ ] Console controller navigation for UI

---

## Key Technical Decisions

### Why WebSocket over RCON
| Factor | WebSocket | RCON |
|--------|----------|------|
| Payload size | **Unlimited** | 4KB per packet |
| Direction | **Bidirectional** | Request/response only |
| Event push | **Yes** | No (polling required) |
| Data format | **JSON** | Plain text |
| Latency | **<1ms** (persistent) | ~35ms (per-request) |
| Concurrent users | **Native** | Single-threaded |

### Why Custom UI over Chat
- No admin required
- Private by default
- Rich formatting
- Conversation history
- Streaming text
- Cross-platform (including console)
- No dependency on BetterChat/Chat Commands mods

### Why Blueprint Mod over AsaApi Plugin
- **Works on Linux/Proton** — no DLL injection, no Wine issues
- Cross-platform (PC + console)
- Official DevKit support
- CurseForge distribution
- No AsaApi dependency (which is blocked on our server)

---

## Reference Material

### Open-Source Templates
- **GSA Mod (MIT)**: github.com/gameserverapp/gsa-mod-asa — ScriptCommand handler, HTTP/WebSocket, INI config
- **MCP Unity Bridge**: CoderGamester/mcp-unity — MCP-to-game-engine WebSocket pattern
- **Michidu/Ark-Server-Plugins**: Extended RCON logic (C++, for reference on what queries to implement)
- **MCP Python SDK**: github.com/modelcontextprotocol/python-sdk — FastMCP framework

### ASA DevKit Documentation
- Blueprint Secure Networking: devkit.studiowildcard.com/systems-tools/blueprint-secure-networking
- WebSockets: devkit.studiowildcard.com/systems-tools/blueprint-secure-networking/websockets
- DevKit Getting Started: devkit.studiowildcard.com/getting-started

### Existing Mods (Architectural Reference)
- ASA-Bot Companion (2.6M downloads) — WebSocket communication, ScriptCommand handler
- WBUI2 (1.8M) — Custom UMG UI with JSON config
- Gaia Admin Commands — Rich UI, admin panel, color text
- Resonant's Admin Panel — Radial menu triggered UI
- BetterChat (296K) — Chat interception, MCI API, whisper
