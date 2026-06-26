"""The LLM provider retries transient API failures with backoff so a single Anthropic
overload/connection blip doesn't surface as a user-visible 'brain' failure. Permanent errors
(context-window, auth) fail fast. This is the resilience the single-blip 'brain' spam needed."""
import asyncio
from unittest.mock import patch, AsyncMock

import litellm
from sheldon_bridge.providers import llm as L
from sheldon_bridge.providers.llm import LLMProvider, LLMConfig


def _prov(retries=2):
    return LLMProvider(LLMConfig(provider="anthropic", model="claude-sonnet-4-6",
                                 api_key="x", num_retries=retries))


def _timeout():
    return litellm.Timeout("transient test error", model="m", llm_provider="anthropic")


def test_retryable_set_includes_timeout():
    assert L._RETRYABLE_LLM_ERRORS  # non-empty
    assert any(c is getattr(litellm, "Timeout", None) for c in L._RETRYABLE_LLM_ERRORS)


def test_transient_then_success_is_invisible():
    seq = [_timeout(), _timeout(), "RESULT"]

    async def fake(**kw):
        x = seq.pop(0)
        if isinstance(x, BaseException):
            raise x
        return x

    with patch.object(L, "acompletion", fake), \
         patch("sheldon_bridge.providers.llm.asyncio.sleep", new_callable=AsyncMock):
        out = asyncio.run(_prov()._acompletion_with_backoff({}))
    assert out == "RESULT"


def test_exhausts_then_raises():
    err = _timeout()

    async def always_fail(**kw):
        raise err

    with patch.object(L, "acompletion", always_fail), \
         patch("sheldon_bridge.providers.llm.asyncio.sleep", new_callable=AsyncMock) as slept:
        try:
            asyncio.run(_prov(retries=2)._acompletion_with_backoff({}))
            assert False, "should have raised after exhausting attempts"
        except type(err):
            pass
    assert slept.await_count == 2  # 3 attempts -> 2 backoff sleeps between them


def test_auth_error_fails_fast_no_retry():
    calls = {"n": 0}

    async def auth_fail(**kw):
        calls["n"] += 1
        raise litellm.AuthenticationError("bad key", model="m", llm_provider="anthropic")

    with patch.object(L, "acompletion", auth_fail), \
         patch("sheldon_bridge.providers.llm.asyncio.sleep", new_callable=AsyncMock):
        try:
            asyncio.run(_prov()._acompletion_with_backoff({}))
            assert False, "auth error should propagate"
        except litellm.AuthenticationError:
            pass
    assert calls["n"] == 1  # no retries on a permanent error
