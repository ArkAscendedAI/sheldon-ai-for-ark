# Security Policy

## Supported Versions

| Version | Supported |
|---------|-----------|
| Latest  | Yes       |

## Reporting a Vulnerability

If you discover a security vulnerability — especially in the permission enforcement system — **please do not open a public issue.**

Instead, report it via [GitHub Security Advisories](https://github.com/ArkAscendedAI/sheldon-ai-for-ark/security/advisories/new).

### What Qualifies

- Permission bypass (player accessing admin tools)
- HMAC signature forgery or validation bypass
- Session isolation failures (player A seeing player B's context)
- Rate limiting bypass
- WebSocket authentication issues

### Response Timeline

- **Acknowledgment:** within 48 hours
- **Initial assessment:** within 1 week
- **Fix and disclosure:** coordinated with reporter

## Architecture

The permission model is documented in [docs/PERMISSIONS.md](docs/PERMISSIONS.md). The LLM is treated as an untrusted component — all enforcement happens in deterministic code.
