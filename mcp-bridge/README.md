# Sheldon Bridge

The server-side companion for the **Sheldon AI for ARK** mod, an LLM-powered
in-game assistant for ARK: Survival Ascended. The bridge runs next to your ARK
server (as a Docker container or a Python program); the mod connects to it over
a WebSocket and relays player chat to the LLM.

New here? Start with the full [Setup Guide](../SETUP.md), which walks through
everything from nothing to a working assistant.

## Quick start (Docker)

From the repository root:

```bash
cp mcp-bridge/.env.example .env      # set the two required values below
docker compose up -d
```

## Quick start (pip)

```bash
pip install .            # from this mcp-bridge directory
sheldon-bridge init      # pick a provider, paste your key, generate a secret
sheldon-bridge run       # run from the repo root so it finds ./data
```

## Required settings

Two values are required; everything else has a sensible default.

- `SHELDON_LLM_API_KEY` - your LLM provider's API key (or set the provider's own
  native variable instead, e.g. `OPENROUTER_API_KEY`).
- `SHELDON_SHARED_SECRET` - a password shared with the mod. It must match the
  `AuthSecret` in the ARK server's `GameUserSettings.ini` `[SheldonAI]` section.
  Generate one with `sheldon-bridge secret` or `openssl rand -base64 48`.

See [`.env.example`](.env.example) for every available setting, with examples.

## Connecting the mod

In the ARK server's `GameUserSettings.ini`:

```ini
[SheldonAI]
WebSocketURL="ws://your-bridge-host:8443"
AuthSecret=the-same-value-as-SHELDON_SHARED_SECRET
```

The bridge serves a plain `ws://` WebSocket by default, which is the normal
setup. To encrypt the connection, set `SHELDON_SSL_CERT` and `SHELDON_SSL_KEY`
and change the mod's `WebSocketURL` to `wss://` (see [`.env.example`](.env.example)).

## License

MIT
