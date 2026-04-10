---
name: SheldonAI Mod Build Guide — Automated Pipeline
description: Complete guide for building the SheldonAI Blueprint mod using the DevKit Bridge automated pipeline. Updated 2026-04-08 with T3D clipboard paste automation.
type: guide
updated: 2026-04-08
devkit_version: "5.5.4 (Build 84.11.266.801108, Branch UE5, Build Date Mar 10 2026)"
---

# SheldonAI Mod Build Guide

## IMPORTANT: Two Build Approaches

### Approach A: Automated via DevKit Bridge (RECOMMENDED)
Use the ARK DevKit Bridge to programmatically create Blueprints, variables, functions,
and nodes. Nodes with full pin wiring are pasted via the T3D clipboard format.
This is DRAMATICALLY faster than manual Blueprint editing.

**Requires:** DevKit editor running on your workstation + the DevKit Bridge TCP server.
**See:** The DevKit Bridge repository for setup and reference.

### Approach B: Manual Blueprint editing (FALLBACK)
If the bridge is unavailable, follow the step-by-step manual instructions below.
This is slower but works without the bridge.

---

## AUTOMATED PIPELINE (Approach A)

### How It Works

```
Build Script (your dev machine)
    |
    | 1. Runs Python script via the DevKit Bridge CLI
    v
ARK DevKit Bridge (TCP connection to editor)
    |
    | 2. Creates Blueprint assets, variables, function graphs
    | 3. Generates T3D text with nodes + pin connections
    | 4. Sets Windows clipboard via PowerShell
    v
DevKit Editor (your workstation)
    |
    | 5. Click in graph, press Ctrl+V
    | 6. Fully wired nodes appear instantly
    | 7. Script compiles and saves
    v
Done! Complete Blueprint mod.
```

### What the Script Does Programmatically

| Step | API | Status |
|------|-----|--------|
| Create Blueprint asset | `BlueprintEditorLibrary.create_blueprint_asset_with_parent()` | Proven |
| Add variables (Bool, String, Int) | `BlueprintEditorLibrary.add_member_variable()` | Proven |
| Create function graphs | `BlueprintEditorLibrary.add_function_graph()` | Proven |
| Add BPSecureNetworkingInterface | Script creates + configures | Proven |
| Create nodes with function refs | `new_object()` + `FunctionReference.import_text()` | Proven |
| Add nodes to graphs | `graph.wc_get_property_value('Nodes').append()` | Proven |
| **Paste fully-wired nodes** | **T3D text → clipboard → Ctrl+V** | **Proven** |
| Compile | `BlueprintEditorLibrary.compile_blueprint()` | Proven |
| Save | `EditorAssetLibrary.save_asset()` | Proven |

### Running the Build Script

```bash
# 1. Verify bridge is up
~/bin/ark-devkit status

# 2. Run the mod generation script
ark-devkit -f scripts/build_sheldon_mod.py

# 3. Script will prompt you to paste into each graph tab:
#    "Clipboard loaded with EventGraph nodes. Click in EventGraph and press Ctrl+V"
#    "Clipboard loaded with ReadConfig nodes. Click in ReadConfig tab and press Ctrl+V"
#    etc.

# 4. Script compiles and saves when done
```

### T3D Format Reference

The T3D (copy-paste) format for Blueprint nodes:

```
Begin Object Class=/Script/BlueprintGraph.K2Node_CallFunction Name="K2Node_CallFunction_0" ExportPath="..."
   FunctionReference=(MemberParent=/Script/Engine.KismetSystemLibrary,MemberName="PrintString")
   NodePosX=400
   NodePosY=300
   NodeGuid=XXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXX
   CustomProperties Pin (PinId=GUID1,PinName="execute",Direction="EGPD_Input",PinType.PinCategory="exec",LinkedTo=(OtherNode PinGUID,),...)
   CustomProperties Pin (PinId=GUID2,PinName="then",Direction="EGPD_Output",PinType.PinCategory="exec",...)
End Object
```

**Key fields:**
- `LinkedTo=(NodeName PinId,)` — defines wire connections (must be on BOTH sides)
- `PinType.PinCategory` — "exec" (white arrows), "string" (pink), "bool" (red), "int" (green), "real" (green)
- `Direction` — "EGPD_Input" or "EGPD_Output"
- `DefaultValue` — default value for the pin
- `NodeGuid` — unique GUID per node (generate fresh ones)
- `PinId` — unique GUID per pin (generate fresh ones)

### Template Capture

A T3D capture of the BP_SheldonWebSocket EventGraph (25 nodes with all connections)
can be obtained by selecting all nodes in the DevKit graph and pressing Ctrl+C.
This serves as the reference template for generating T3D text.

---

## WHAT THE MOD NEEDS (both approaches)

### Blueprint 1: BP_SheldonWebSocket (Actor Component)

**Parent:** ActorComponent
**Interface:** BPSecureNetworkingInterface (all audit functions return True)

**Variables:**
| Name | Type | Default |
|------|------|---------|
| SocketID | Integer | -1 |
| IsConnected | Boolean | false |
| BridgeURL | String | (empty) |
| AuthSecret | String | (empty) |
| PlayerID | String | (empty) |
| PlayerName | String | (empty) |
| PlayerTier | String | "player" |
| IsAuthenticated | Boolean | false |

**Functions:**
- ReadConfig: Get Game Mode → Cast To ShooterGameMode → GetStringOption (SheldonAI/WebSocketURL) → SET BridgeURL → GetStringOption (SheldonAI/AuthSecret) → SET AuthSecret
- ConnectToBridge: ReadConfig → Create WebSocket (BridgeURL) → SET SocketID → Branch (>=0) → Connect WebSocket
- SendAuthMessage: Append 9-pin JSON → Send Message to WebSocket
- SendPlayerMessage(Message): Branch IsAuthenticated → Build JSON with position → Send Message to WebSocket
- Disconnect: Branch IsConnected → Close WebSocket → Reset state

**EventGraph Events:**
- On WebSocket Connected → SET IsConnected true → SendAuthMessage
- On Websocket Connection Error → SET IsConnected false → Print → Delay 5s → ConnectToBridge (retry)
- On WebSocket Closed → SET IsConnected false → SET IsAuthenticated false → Print
- On WebSocket Message Recieved → Parse type → Branch auth_success/reply/thinking/error

### Blueprint 2: WBP_SheldonChat (Widget, parent: PrimalUI)

**UI Layout:** Dark panel (500x600), header ("Sheldon AI" + close button), ScrollBox with ChatHistory VerticalBox, input area (EditableTextBox + Send button)

**Logic:** Send button → get text → add to chat → call SendPlayerMessage → clear input. Close button → Remove From Parent. Enter key → same as send. DisplayResponse/ShowThinkingIndicator/DisplayError functions for WebSocket manager callbacks.

### Blueprint 3: BP_SheldonMapExtension (Map Extension)

**Startup:** BeginPlay → Switch Has Authority → Authority: ConnectToBridge (server-only WebSocket) | Remote: EnableInput → CreateWidget → Set MapExtensionRef → Bind F8 key
**F8 Toggle:** Toggle chat widget visibility, set input mode
**NetExec:** Client↔server messaging via BPServerHandleNetExecCommand / BPClientHandleNetExecCommand overrides

---

## BRIDGE PROTOCOL (what the mod sends/receives)

### Auth (first message, within 10 seconds)
```json
{"type":"auth","token":"SECRET","player":{"player_id":"EOS_ID","display_name":"NAME","tier":"player","tribe_id":"","position":{"x":0,"y":0,"z":0},"facing_yaw":0.0}}
```

### Player Message
```json
{"type":"player_message","message":"text","position":{"x":0,"y":0,"z":0},"facing_yaw":0.0}
```

### Bridge Responses
- `{"type":"auth_success","player_id":"...","tier":"...","tools_available":12}`
- `{"type":"thinking"}`
- `{"type":"reply","message":"...","stats":{...}}`
- `{"type":"error","message":"..."}`
- Auth failure: WebSocket close code 4001

### Bridge Defaults
Port 8443, auth timeout 10s, ping interval 20s, max message 1MB.

---

## KEY DEVKIT FACTS

- BPSecureNetworkingInterface provides WebSocket (Create, Connect, Send, Receive, Close)
- Audit functions MUST be implemented or WebSocket operations silently fail
- Widget parent MUST be PrimalUI (crashes otherwise)
- Server-authoritative: WebSocket on Authority branch, UI on Remote branch
- INI reading: GetStringOption on ShooterGameMode (Section + OptionName params)
- "On WebSocket Message Recieved" is misspelled in the DevKit — use that exact spelling
- Map Extensions, NOT GameMode (only first mod's GameMode loads)
- CurseForge cloud cooking handles PC/Xbox/PS5 cross-platform
- Console platforms require wss:// (not ws://)
- Create WebSocket returns last valid ID on failure (known bug, will be fixed to return -1)

---

## MANUAL APPROACH (Approach B — fallback)

See the detailed step-by-step instructions in the git history of this file (pre-automation version)
or follow the detailed steps below. The manual process involves:
1. Create mod via UGC → Create Mod → Empty template
2. Create BP_SheldonWebSocket (Actor Component parent)
3. Add BPSecureNetworkingInterface, implement audit functions
4. Create variables, build functions, wire nodes manually
5. Create WBP_SheldonChat widget (PrimalUI parent)
6. Create BP_SheldonMapExtension
7. Register in ModDataAsset
8. Cook via UGC → Share Mod → CurseForge cloud cooking

---

## PUBLISHING TO CURSEFORGE

1. UGC → Share Mod → select SheldonAI
2. Fill in: name, image (400x400 1:1), category, summary, description
3. Choose PC-Only (auto-approve) or Cross-Platform (moderation review)
4. Cloud cooking: 10-60 minutes
5. Publish when "Ready For Review"
