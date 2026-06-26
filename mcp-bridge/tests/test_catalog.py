"""V18 Path A — model catalog + endpoint-capable, tool-aware failover.

Covers the catalog itself (lookup / resolution / passthrough / validation) and the failover-chain
integration: a chat-only endpoint (supports_tools=False) is SKIPPED for a tool-bearing turn so a
lookup is never silently dropped, but it can still serve a tools=None turn. Drives complete() with
the per-model backoff mocked so no litellm calls / real sleeps happen."""
import sys
import types
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sheldon_bridge import catalog
from sheldon_bridge.providers.llm import LLMConfig, LLMProvider


def _resp(text):
    msg = types.SimpleNamespace(content=text, tool_calls=None)
    choice = types.SimpleNamespace(message=msg, finish_reason="stop")
    return types.SimpleNamespace(choices=[choice],
                                 usage=types.SimpleNamespace(prompt_tokens=1, completion_tokens=1))


# ---- catalog units -------------------------------------------------------------------------

def test_get_model_known_and_unknown():
    assert catalog.get_model("sonnet").provider == "anthropic"
    assert catalog.get_model("nope") is None


def test_resolve_catalog_id():
    ep = catalog.resolve_endpoint("sonnet")
    assert ep.model == "anthropic/claude-sonnet-4-6"
    assert ep.supports_tools is True
    assert ep.api_base is None
    assert ep.label == "sonnet"


def test_resolve_bare_string_passthrough():
    # A non-catalog spec is passed straight through as a tool-capable provider-default endpoint
    # (backward-compatible with pre-catalog SHELDON_LLM_FALLBACK_MODELS values).
    ep = catalog.resolve_endpoint("openai/gpt-4o-mini")
    assert ep.model == "openai/gpt-4o-mini"
    assert ep.supports_tools is True
    assert ep.api_base is None and ep.api_key is None


def test_bridge_entry_is_chat_only():
    e = catalog.get_model("claude-code-bridge")
    assert e is not None and e.supports_tools is False


def test_resolve_reads_endpoint_env(monkeypatch):
    monkeypatch.setenv("SHELDON_CLAUDECODE_BRIDGE_URL", "http://127.0.0.1:8088")
    monkeypatch.setenv("SHELDON_CLAUDECODE_BRIDGE_SECRET", "shh")
    ep = catalog.resolve_endpoint("claude-code-bridge")
    assert ep.api_base == "http://127.0.0.1:8088"
    assert ep.api_key == "shh"
    assert ep.supports_tools is False


def test_validate_clean_by_default(monkeypatch):
    # No bridge env set → no half-configured endpoints flagged.
    monkeypatch.delenv("SHELDON_CLAUDECODE_BRIDGE_URL", raising=False)
    assert catalog.validate_catalog() == []


def test_validate_flags_url_without_secret(monkeypatch):
    monkeypatch.setenv("SHELDON_CLAUDECODE_BRIDGE_URL", "http://127.0.0.1:8088")
    monkeypatch.delenv("SHELDON_CLAUDECODE_BRIDGE_SECRET", raising=False)
    problems = catalog.validate_catalog()
    assert any("will 401" in p for p in problems)


# ---- failover-chain integration -----------------------------------------------------------

def _provider(monkeypatch, behavior, fallbacks):
    cfg = LLMConfig(provider="anthropic", model="anthropic/primary", api_key="k",
                    fallback_models=fallbacks)
    prov = LLMProvider(cfg)
    seen = []

    async def fake_backoff(kwargs):
        seen.append(kwargs["model"])
        return behavior(kwargs["model"])

    monkeypatch.setattr(prov, "_acompletion_with_backoff", fake_backoff)
    return prov, seen


async def test_chat_only_fallback_skipped_when_tools_present(monkeypatch):
    # Primary is down; the only fallback is chat-only. With tools on the turn it must be SKIPPED,
    # and the primary's failure surfaces (we do NOT silently answer a lookup with no tools).
    def behave(model):
        if model == "anthropic/primary":
            raise RuntimeError("primary down")
        return _resp("should not be reached")
    prov, seen = _provider(monkeypatch, behave, ["claude-code-bridge"])
    with pytest.raises(RuntimeError):
        await prov.complete([{"role": "user", "content": "what's my health?"}],
                            tools=[{"type": "function", "function": {"name": "get_vitals"}}])
    assert seen == ["anthropic/primary"]  # bridge skipped, never called


async def test_chat_only_fallback_used_when_no_tools(monkeypatch):
    # Same chat-only fallback, but a tools=None turn (agent.py's forced-final iteration) may use it.
    def behave(model):
        if model == "anthropic/primary":
            raise RuntimeError("primary down")
        return _resp("narrated by the bridge")
    prov, seen = _provider(monkeypatch, behave, ["claude-code-bridge"])
    r = await prov.complete([{"role": "user", "content": "summarize"}])  # tools default None
    assert r.content == "narrated by the bridge"
    assert seen == ["anthropic/primary", "anthropic/claude-sonnet-4-6"]  # bridge attempted


async def test_tool_capable_fallback_still_used_with_tools(monkeypatch):
    # A normal (tool-capable) fallback is NOT skipped on a tool-bearing turn.
    def behave(model):
        if model == "anthropic/primary":
            raise RuntimeError("primary down")
        return _resp("served by gpt-4o")
    prov, seen = _provider(monkeypatch, behave, ["gpt-4o"])
    r = await prov.complete([{"role": "user", "content": "hi"}], tools=[{"x": 1}])
    assert r.content == "served by gpt-4o"
    assert seen == ["anthropic/primary", "openai/gpt-4o"]  # catalog id resolved + used
