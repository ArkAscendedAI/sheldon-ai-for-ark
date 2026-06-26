"""V18 Path A — cross-model failover. After the primary model exhausts its own backoff (a
sustained outage), complete() advances to the next model in the chain. ContextWindowExceeded is
never failed-over (no model fits a too-long prompt). Tests drive complete() with the per-model
backoff mocked so no litellm calls / real sleeps happen."""
import sys
import types
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import litellm
from sheldon_bridge.providers.llm import LLMConfig, LLMProvider


def _resp(text):
    msg = types.SimpleNamespace(content=text, tool_calls=None)
    choice = types.SimpleNamespace(message=msg, finish_reason="stop")
    return types.SimpleNamespace(choices=[choice],
                                 usage=types.SimpleNamespace(prompt_tokens=1, completion_tokens=1))


def _provider(monkeypatch, behavior):
    cfg = LLMConfig(provider="anthropic", model="anthropic/primary", api_key="k",
                    fallback_models=["openai/backup", "gemini/last"])
    prov = LLMProvider(cfg)
    seen = []

    async def fake_backoff(kwargs):
        seen.append(kwargs["model"])
        return behavior(kwargs["model"])

    monkeypatch.setattr(prov, "_acompletion_with_backoff", fake_backoff)
    return prov, seen


async def test_primary_success_skips_fallbacks(monkeypatch):
    prov, seen = _provider(monkeypatch, lambda m: _resp("primary ok"))
    r = await prov.complete([{"role": "user", "content": "hi"}])
    assert r.content == "primary ok" and seen == ["anthropic/primary"]


async def test_fails_over_to_first_available_fallback(monkeypatch):
    def behave(model):
        if model == "anthropic/primary":
            raise RuntimeError("anthropic sustained outage")
        return _resp("served by backup")
    prov, seen = _provider(monkeypatch, behave)
    r = await prov.complete([{"role": "user", "content": "hi"}])
    assert r.content == "served by backup"
    assert seen == ["anthropic/primary", "openai/backup"]  # stopped at the first that worked


async def test_all_models_down_raises_last(monkeypatch):
    def behave(model):
        raise RuntimeError(f"{model} down")
    prov, seen = _provider(monkeypatch, behave)
    with pytest.raises(RuntimeError):
        await prov.complete([{"role": "user", "content": "hi"}])
    assert seen == ["anthropic/primary", "openai/backup", "gemini/last"]  # tried the whole chain


async def test_context_window_never_fails_over(monkeypatch):
    def behave(model):
        raise litellm.ContextWindowExceededError("too long", model=model, llm_provider="anthropic")
    prov, seen = _provider(monkeypatch, behave)
    with pytest.raises(litellm.ContextWindowExceededError):
        await prov.complete([{"role": "user", "content": "hi"}])
    assert seen == ["anthropic/primary"]  # no point trying other models for an oversized prompt
