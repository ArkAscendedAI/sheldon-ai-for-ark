# SheldonAI Mod — Source Blueprints

This directory contains the source Blueprint assets for the SheldonAI in-game mod.

## For Players

Subscribe to **SheldonAI** on [CurseForge](#) to install the mod on your server. You don't need these source files.

## For Modders — Opening in the DevKit

To open, modify, or fork the mod:

1. Install the [ASA DevKit](https://store.epicgames.com/en-US/p/ark-survival-ascended-devkit) from the Epic Games Store
2. Copy this entire `mod/` directory into your DevKit's mod folder:
   ```
   <DevKit Install>/Projects/ShooterGame/Mods/SheldonAI/
   ```
   So the files end up at:
   ```
   .../Mods/SheldonAI/SheldonAI.uplugin
   .../Mods/SheldonAI/Content/Blueprints/BP_SheldonWebSocket.uasset
   .../Mods/SheldonAI/Content/Blueprints/BP_SheldonMapExtension.uasset
   .../Mods/SheldonAI/Content/Widgets/WBP_SheldonChat.uasset
   .../Mods/SheldonAI/Content/ModDataAsset_SheldonAI.uasset
   .../Mods/SheldonAI/Content/PrimalGameData_BP_SheldonAI.uasset
   .../Mods/SheldonAI/Resources/Icon128.png
   ```
3. Open the DevKit. The mod content appears under **All > Plugins > SheldonAI Content**

## Blueprint Overview

| Asset | Type | Purpose |
|-------|------|---------|
| `BP_SheldonWebSocket` | ActorComponent | WebSocket client — connects to the Sheldon Bridge server |
| `WBP_SheldonChat` | Widget (PrimalUI) | In-game chat panel UI — text input, message display, F8 toggle |
| `BP_SheldonMapExtension` | SaveGameActor | World singleton — manages the WebSocket lifecycle, F8 keybind, client↔server NetExec routing |
| `ModDataAsset_SheldonAI` | ModDataAsset | Mod registration — tells ARK to spawn the MapExtension |
| `PrimalGameData_BP_SheldonAI` | PrimalGameData | Required for ARK to load the ModDataAsset |

## Architecture

The mod uses a **server-authoritative** design:

- The **server** (Authority) owns the WebSocket connection to the Bridge
- **Clients** (Remote) get the chat UI and communicate with the server via ARK's built-in NetExec RPC system
- Player messages flow: Client → NetExec → Server → WebSocket → Bridge → LLM → Bridge → WebSocket → Server → NetExec → Client

This means only the server needs network access to the Bridge — players don't need any special connectivity.
