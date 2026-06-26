# SheldonAI Setup Guide

This guide takes you from nothing to a working in-game AI assistant. It is written for ARK server owners who are not programmers. Follow the steps in order and you will be fine.

If you only remember one thing: **the mod by itself does nothing.** It needs two other pieces running alongside it. This guide sets up all three.

> Platform note: SheldonAI is PC only right now. Console (Xbox / PlayStation) support is not available yet.

---

## How SheldonAI fits together

There are three parts, and all three must be present:

```
   ARK server (with the SheldonAI mod)
            |
            |  WebSocket  (ws://your-bridge-host:8443)
            v
   The Sheldon Bridge   <-- a small program you run
            |
            |  internet
            v
   An AI provider   (Anthropic, OpenAI, Google, OpenRouter, ...)
```

1. **The mod** runs in the game and adds the F8 chat window. You install it from CurseForge. (This guide assumes you can already add a mod to your server.)
2. **The Sheldon Bridge** is a small program you run on a computer that can reach your ARK server. It connects the mod to your AI and does all the real work.
3. **An AI provider** is the "brain". You bring your own account and API key. This part usually costs a small amount of money per message (often a fraction of a cent), and that cost grows the more people use it. Some providers have free tiers.

Leave out any one of these and Sheldon will not answer.

---

## Before you start

You will need:

- An ARK: Survival Ascended **server you control** (so you can edit `GameUserSettings.ini` and restart it).
- A computer or small server to **run the bridge**. It can be the same machine as your ARK server, a home PC, or a cheap cloud box. It needs either **Docker** or **Python 3.11+**.
- An **AI provider account and API key** (see Step 1). Be aware this can cost money per message.
- About 20 minutes.

---

## Step 1: Get an AI provider and API key

Sheldon's brain is a large language model, the same kind of AI behind ChatGPT. You point the bridge at a provider and give it a key.

**OpenRouter is the easiest starting point**: one account and one key gives you access to hundreds of models (including Anthropic, OpenAI, and Google). You can also go straight to a single provider if you prefer.

1. Make an account with a provider:
   - OpenRouter: https://openrouter.ai
   - Anthropic (Claude): https://console.anthropic.com
   - OpenAI: https://platform.openai.com
   - Google (Gemini): https://aistudio.google.com
2. Create an **API key** in that provider's dashboard and copy it somewhere safe.
3. Add a little credit if the provider requires it. Sheldon is cheap per message, but it is not free unless you are on a free tier.

Keep that API key handy. You will paste it into the bridge in the next step.

---

## Step 2: Get the bridge running

First get the project files (you need them for both methods, and they include the example configs and the knowledge base):

```bash
git clone https://github.com/ArkAscendedAI/sheldon-ai-for-ark.git
cd sheldon-ai-for-ark
```

You also need a **shared secret**. This is a password the mod and the bridge use to trust each other. Generate one now and keep it:

```bash
openssl rand -base64 48
```

Copy that output. You will use the same value in two places: the bridge (below) and the mod (Step 3).

Now pick **one** of the two methods below. They are equally supported.

### Method A: Docker (no Python needed)

1. Create your settings file from the example:
   ```bash
   cp mcp-bridge/.env.example .env
   ```
2. Open `.env` in a text editor and set the two required values:
   ```ini
   SHELDON_LLM_API_KEY=your-api-key-from-step-1
   SHELDON_SHARED_SECRET=the-secret-you-just-generated
   ```
   If you did not choose OpenRouter, also set `SHELDON_LLM_PROVIDER` (for example `anthropic`) and `SHELDON_LLM_MODEL`. Every other setting is optional and explained in the file.
3. Start the bridge:
   ```bash
   docker compose up -d
   ```
4. Check it is running:
   ```bash
   docker compose logs -f
   ```
   You are looking for a line like `Sheldon Bridge starting on ws://0.0.0.0:8443`. Press Ctrl+C to stop watching the logs (the bridge keeps running).

### Method B: pip (Python 3.11+)

1. Install the bridge:
   ```bash
   cd mcp-bridge
   pip install .
   cd ..
   ```
2. Run the setup wizard. It asks which provider you use, takes your API key, and generates a `config.json` plus a shared secret:
   ```bash
   sheldon-bridge init
   ```
   If you already generated a secret above and want to reuse it, you can instead copy `mcp-bridge/.env.example` to `.env`, fill in the two required values, and skip the wizard.
3. Start the bridge from the **repository root** (so it finds the bundled knowledge base in `./data`):
   ```bash
   sheldon-bridge run
   ```
   You should see `Sheldon Bridge starting on ws://0.0.0.0:8443`.

> Whichever method you used, the bridge now listens on port **8443** using a plain `ws://` WebSocket. That is the normal setup. (If you want to encrypt the connection, see "Encrypting the connection" near the end.)

---

## Step 3: Point your ARK server at the bridge

On your **ARK server**, open `GameUserSettings.ini` (it lives in `ShooterGame/Saved/Config/WindowsServer/GameUserSettings.ini`) and add this section:

```ini
[SheldonAI]
WebSocketURL="ws://your-bridge-host:8443"
AuthSecret=the-secret-you-generated-in-step-2
```

- Replace `your-bridge-host` with the IP address or hostname of the machine running the bridge, **as seen from your ARK server**. If the bridge runs on the same machine, use that machine's LAN IP. If it runs elsewhere, use that machine's IP or hostname.
- Keep the port `8443` unless you changed `SHELDON_WS_PORT`.
- `AuthSecret` must match the bridge's `SHELDON_SHARED_SECRET` **exactly**.
- The mod only reads these two keys. Ignore any older guide that lists more.

---

## Step 4: Add the mod and restart

1. Add the **SheldonAI** mod (CurseForge) to your server's mod list, the same way you add any other mod.
2. **Restart your ARK server.** Mods and `GameUserSettings.ini` changes only take effect on a fresh server start.

---

## Step 5: Test it

1. Join your server.
2. Press **F8** to open the Sheldon chat window.
3. Ask something simple, like `What is the best kibble for a Therizino?` or `How is my food doing?`
4. You should get an answer within a few seconds.

If you watch the bridge logs while you do this, you will see the connection and the request come through.

---

## Making a player an admin

Sheldon has two permission levels for the people using it: **player** and **admin**. Regular players can ask questions and use the read-only and personal tools. Admins can also make Sheldon do things on the server (spawn a dino, set the time, give an item, broadcast, run a console command).

A player is treated as an admin automatically when they are a **server admin**, which in ARK means their ID is in your server's `AllowedCheaterPlayerIDs.txt`. Add them there (the normal ARK way), restart, and Sheldon will give them the admin tools. Regular players never even see the admin tools, so there is nothing for a clever message to unlock.

---

## Customizing Sheldon

All of this is optional. Set these on the bridge (`.env` or `config.json`). See [`mcp-bridge/.env.example`](mcp-bridge/.env.example) for the full list with examples.

- **Personality:** point `SHELDON_PERSONA_FILE` at a markdown file that defines his voice (dry and sarcastic, warm and helpful, fully in-character, whatever you like).
- **Your server's rules and lore:** point `SHELDON_SERVER_CONTEXT_DIR` at a folder of markdown files describing your server. Sheldon will answer questions about your server, not just vanilla ARK.
- **Model and cost:** `SHELDON_LLM_MODEL` chooses the model. A cheaper model costs less per message; a stronger one gives better answers.
- **Find-dinos and dino map pins:** set `SHELDON_CENSUS_ENABLED=true` to turn on the background dino scan that powers "where are the high-level dinos" and dino map pins. Leave it off if you do not want it.
- **Your own modded creatures and items:** drop JSON files into `data/custom` and Sheldon searches them alongside the built-in knowledge base.

---

## Encrypting the connection (optional)

By default the mod talks to the bridge over a plain `ws://` WebSocket. That is fine for a bridge on the same machine or a private/LAN network. If your bridge is reachable over the public internet and you want the traffic encrypted, turn on TLS:

1. On the bridge, set both of these to paths on a writable volume:
   ```ini
   SHELDON_SSL_CERT=/data/certs/bridge.crt
   SHELDON_SSL_KEY=/data/certs/bridge.key
   ```
   If the files do not exist, the bridge generates a self-signed pair there on first start. For the best result, mount your own real certificate at those paths instead.
2. Change the mod's `WebSocketURL` to use `wss://` instead of `ws://`.
3. Restart both the bridge and the ARK server.

---

## Troubleshooting

**The F8 window opens but Sheldon never answers.**
The mod cannot reach the bridge, or authentication is failing. Check, in order:
- Is the bridge actually running? (`docker compose logs -f`, or look at the `sheldon-bridge run` output.)
- Does `WebSocketURL` point at the right host and port, reachable from the ARK server? Try it without a path after the port.
- Does `AuthSecret` (mod) match `SHELDON_SHARED_SECRET` (bridge) exactly? A trailing space or quote will break it.
- If you turned on TLS, the mod must use `wss://`; if you did not, it must use `ws://`.

**The bridge logs show an LLM error.**
Your API key is wrong, has no credit, or the model id is not valid for your provider. Re-check `SHELDON_LLM_API_KEY`, `SHELDON_LLM_PROVIDER`, and `SHELDON_LLM_MODEL`.

**Sheldon answers general ARK questions but cannot look up dinos or items.**
The knowledge base was not found. With Docker it is bundled automatically. With pip, run the bridge from the repository root so it can find `./data`.

**Admin commands do nothing for an admin.**
Confirm that player is in `AllowedCheaterPlayerIDs.txt` and that the server was restarted after you added them.

**Connection refused / nothing listening on 8443.**
The bridge is not running, or a firewall is blocking the port between the ARK server and the bridge. Open the port (or run the bridge on the same machine as the server).

---

## A note on cost

The bridge and the mod are free and open source. The AI provider is not (unless you use a free tier). You pay the provider per message, usually a small amount. Watch your usage, pick a cheaper model if needed, and set spending limits in your provider's dashboard.

---

## Where to go next

- Full list of every bridge setting: [`mcp-bridge/.env.example`](mcp-bridge/.env.example)
- Example config files: [`examples/`](examples/)
- How permissions work in depth: [`docs/PERMISSIONS.md`](docs/PERMISSIONS.md)
- How it all works under the hood: [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md)
- Building or modifying the Blueprint mod yourself: [`MOD_BUILD_GUIDE.md`](MOD_BUILD_GUIDE.md)
