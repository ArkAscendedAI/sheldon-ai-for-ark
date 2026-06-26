"""Model catalog.

A lean, provider-agnostic registry of LLM endpoints Sheldon can route to. It backs the
cross-model failover chain in ``providers/llm.py``: a fallback spec in
``SHELDON_LLM_FALLBACK_MODELS`` may be either a **catalog id** (resolved here to a litellm
model string + an optional custom ``api_base`` + auth + tool-capability) or a **bare litellm
model string** (passed straight through, backward-compatible with pre-catalog config).

Why ``supports_tools`` matters
------------------------------
Sheldon's whole value is its tool loop (``get_vitals``/``find_dinos``/``add_pin``/...). Some
endpoints are CHAT-ONLY — e.g. a relay that ignores the caller's ``tools`` and runs with no
tools enabled. Routing a tool-bearing turn to such an endpoint would silently drop every lookup
and make Sheldon hallucinate game state. The failover chain therefore **SKIPS
``supports_tools=False`` entries whenever the turn carries tools**, so a chat-only endpoint is
safe to list (it can only ever serve the rare forced-final no-tools turn in ``agent.py``) and
can never break a lookup.

Custom chat-only endpoints can be listed as documented, env-gated entries. They are NOT enabled
by default — wiring one as a real Sheldon endpoint needs (a) the matching ``*_URL``/``*_SECRET``
env vars and (b) the auth header format confirmed against the endpoint. To make such an endpoint
serve the *main* loop it would need real caller-tool passthrough (a separate piece of work).
"""
from __future__ import annotations

import os
from dataclasses import dataclass


@dataclass(frozen=True)
class ModelEntry:
    """One catalog endpoint."""

    id: str  # friendly catalog id used in SHELDON_LLM_FALLBACK_MODELS
    provider: str  # anthropic | openai | gemini | openrouter | claude-code | codex
    litellm_model: str  # the model string passed to litellm.acompletion
    supports_tools: bool = True  # False = chat-only endpoint (see module docstring)
    api_base_env: str | None = None  # env var holding a custom endpoint URL (bridges); None = provider default
    auth_env: str | None = None  # env var holding the bearer/api key for a custom endpoint
    context_window: int | None = None  # forward-looking (V18+ variable context caps); None = let litellm decide
    notes: str = ""

    @property
    def api_base(self) -> str | None:
        """The custom endpoint URL, read from env at call time (None = provider default)."""
        return os.environ.get(self.api_base_env) if self.api_base_env else None

    @property
    def api_key(self) -> str | None:
        """The explicit endpoint key, read from env at call time. None → litellm uses the
        provider's own env key (ANTHROPIC_API_KEY/OPENAI_API_KEY/...)."""
        return os.environ.get(self.auth_env) if self.auth_env else None


# Real provider entries track the project's configured model strings (see config.py
# default_models). Update the version strings here when the project bumps models.
_ENTRIES: list[ModelEntry] = [
    ModelEntry("sonnet", "anthropic", "anthropic/claude-sonnet-4-6", context_window=200_000,
               notes="Default working model — full tool support."),
    ModelEntry("haiku", "anthropic", "anthropic/claude-haiku-4-5", context_window=200_000,
               notes="Cheaper/faster Anthropic fallback (needs ANTHROPIC_API_KEY)."),
    ModelEntry("gpt-4o", "openai", "openai/gpt-4o", context_window=128_000,
               notes="OpenAI fallback (needs OPENAI_API_KEY)."),
    ModelEntry("gemini-flash", "gemini", "gemini/gemini-2.0-flash", context_window=1_000_000,
               notes="Google fallback (needs GOOGLE_API_KEY)."),
    # --- Example custom chat-only endpoint (e.g. a Claude Code SDK relay). CHAT-ONLY: a relay of
    # this kind ignores the caller's `tools` and runs with no tools enabled, so supports_tools=False
    # keeps the failover chain from ever sending it a tool-bearing turn — SAFE to list, but it can
    # only serve the forced-final no-tools turn. Inert unless SHELDON_CLAUDECODE_BRIDGE_URL +
    # _SECRET are set AND it is added to SHELDON_LLM_FALLBACK_MODELS (with the caveat understood).
    # An OpenAI-dialect relay would follow this exact pattern.
    ModelEntry("claude-code-bridge", "claude-code", "anthropic/claude-sonnet-4-6",
               supports_tools=False, api_base_env="SHELDON_CLAUDECODE_BRIDGE_URL",
               auth_env="SHELDON_CLAUDECODE_BRIDGE_SECRET", context_window=200_000,
               notes="Example custom chat-only relay endpoint — no tool-calling; "
                     "confirm the auth header format against the endpoint before live use."),
]

CATALOG: dict[str, ModelEntry] = {e.id: e for e in _ENTRIES}


def get_model(model_id: str) -> ModelEntry | None:
    """Look up a catalog entry by id (None if unknown)."""
    return CATALOG.get(model_id)


@dataclass(frozen=True)
class ResolvedEndpoint:
    """A concrete call target for the failover chain (one link)."""

    model: str  # litellm model string
    api_key: str | None  # explicit key (custom endpoints); None → litellm uses the provider env key
    api_base: str | None  # custom endpoint URL; None → provider default
    supports_tools: bool
    label: str  # short name for logs


def resolve_endpoint(spec: str, *, api_key: str | None = None) -> ResolvedEndpoint:
    """Resolve a fallback spec to a concrete endpoint.

    ``spec`` may be a catalog id (resolved to its fields) or a bare litellm model string
    (passed through as a tool-capable, provider-default endpoint — backward-compatible with
    pre-catalog ``SHELDON_LLM_FALLBACK_MODELS`` values). ``api_key`` overrides the resolved key
    (used for the primary, which carries its own explicit key)."""
    entry = CATALOG.get(spec)
    if entry is None:
        return ResolvedEndpoint(model=spec, api_key=api_key, api_base=None,
                                supports_tools=True, label=spec)
    return ResolvedEndpoint(
        model=entry.litellm_model,
        api_key=api_key or entry.api_key,
        api_base=entry.api_base,
        supports_tools=entry.supports_tools,
        label=entry.id,
    )


def validate_catalog() -> list[str]:
    """Startup invariant check. Returns a list of human-readable problems (empty = clean).
    Non-fatal — the caller logs warnings. Catches duplicate ids and half-configured custom
    endpoints (a URL set without a secret will 401; a base_env without an auth_env is a bug)."""
    problems: list[str] = []
    seen: set[str] = set()
    for e in _ENTRIES:
        if e.id in seen:
            problems.append(f"duplicate catalog id: {e.id}")
        seen.add(e.id)
        if e.api_base_env and not e.auth_env:
            problems.append(f"{e.id}: declares api_base_env but no auth_env")
        if e.api_base and not e.api_key:
            problems.append(
                f"{e.id}: {e.api_base_env} is set but {e.auth_env} is empty — the endpoint will 401"
            )
    return problems
