"""LLM provider abstraction using LiteLLM.

Provides a unified async interface for calling any LLM with tool support.
LiteLLM handles translation between providers (Anthropic, OpenAI, Google,
OpenRouter) behind the scenes.
"""

from __future__ import annotations

import asyncio
import logging
import random
from dataclasses import dataclass, field
from typing import Any

import litellm
from litellm import acompletion, completion_cost, get_max_tokens, token_counter

from sheldon_bridge import catalog

logger = logging.getLogger(__name__)

# Transient API conditions worth retrying with backoff. Anthropic returns 429/529 under load
# and brief 5xx/connection blips happen — LiteLLM's own num_retries fire back-to-back with no
# real backoff (the observed "Timeout … time taken=0.006s, Retried 2 times"), so a single blip
# still surfaces as the "trouble connecting to my brain" failure. We own the retry loop with
# exponential backoff + jitter instead. Built defensively (some classes vary by litellm version).
_RETRYABLE_LLM_ERRORS = tuple(
    c for c in (
        getattr(litellm, "Timeout", None),
        getattr(litellm, "APIConnectionError", None),
        getattr(litellm, "InternalServerError", None),
        getattr(litellm, "ServiceUnavailableError", None),
        getattr(litellm, "RateLimitError", None),
        getattr(litellm, "APIError", None),  # generic upstream API error (overload, 5xx)
    ) if isinstance(c, type)
)


@dataclass
class LLMResponse:
    """Normalized response from an LLM call."""

    content: str | None
    tool_calls: list[Any] | None
    finish_reason: str
    input_tokens: int
    output_tokens: int
    cost: float
    raw: Any  # The original litellm response


@dataclass
class LLMConfig:
    """Configuration for the LLM provider."""

    provider: str  # "anthropic", "openai", "gemini", "openrouter"
    model: str  # e.g., "claude-sonnet-4-20250514", "gpt-4o"
    api_key: str
    max_tokens: int = 4096
    temperature: float = 0.7
    timeout: int = 60
    num_retries: int = 2
    # V18 Path A — cross-model fallback. LiteLLM model strings tried IN ORDER after the primary
    # exhausts its own backoff (a SUSTAINED provider outage, not a transient blip the backoff
    # already absorbs). Each uses its provider's env key (ANTHROPIC_API_KEY/OPENAI_API_KEY/…), so
    # a different-provider fallback keeps Sheldon answering when the primary provider is down.
    fallback_models: list[str] = field(default_factory=list)

    @property
    def litellm_model(self) -> str:
        """Get the LiteLLM-format model string."""
        # LiteLLM uses provider prefixes
        prefix_map = {
            "anthropic": "anthropic/",
            "openai": "openai/",
            "gemini": "gemini/",
            "openrouter": "openrouter/",
        }
        prefix = prefix_map.get(self.provider, "")

        # Don't double-prefix if already prefixed
        if self.model.startswith(prefix):
            return self.model
        return f"{prefix}{self.model}"

    @property
    def env_var_name(self) -> str:
        """Get the expected environment variable name for the API key."""
        env_map = {
            "anthropic": "ANTHROPIC_API_KEY",
            "openai": "OPENAI_API_KEY",
            "gemini": "GOOGLE_API_KEY",
            "openrouter": "OPENROUTER_API_KEY",
        }
        return env_map.get(self.provider, "ANTHROPIC_API_KEY")


class LLMProvider:
    """Async LLM client with tool calling support."""

    def __init__(self, config: LLMConfig):
        self.config = config
        self._model = config.litellm_model

        # Configure LiteLLM
        litellm.drop_params = True  # Silently drop unsupported params per provider

        # Surface catalog misconfiguration (half-set bridge endpoints, duplicate ids) as
        # warnings at construction — non-fatal.
        for problem in catalog.validate_catalog():
            logger.warning(f"[catalog] {problem}")

    async def complete(
        self,
        messages: list[dict],
        tools: list[dict] | None = None,
        tool_choice: str = "auto",
    ) -> LLMResponse:
        """Call the LLM with messages and optional tool definitions.

        Args:
            messages: Conversation history in OpenAI format.
            tools: Tool definitions in OpenAI format (from ToolRegistry.to_llm_format).
            tool_choice: "auto", "required", "none", or specific tool name.

        Returns:
            LLMResponse with content, tool_calls, and usage info.
        """
        base_kwargs: dict[str, Any] = {
            "messages": messages,
            "max_tokens": self.config.max_tokens,
            "temperature": self.config.temperature,
            "timeout": self.config.timeout,
            "num_retries": 0,  # we own retries+backoff below (litellm's fire with no real backoff)
        }
        if tools:
            base_kwargs["tools"] = tools
            base_kwargs["tool_choice"] = tool_choice

        # Model chain: the primary (its own explicit key) then each configured fallback. A fallback
        # spec is a catalog id (resolved to a litellm model + optional custom api_base/auth/tool-
        # capability) or a bare litellm model string (provider-default, tool-capable — backward
        # compatible). Each link gets the full exponential-backoff retry; we advance to the next link
        # only once one has exhausted its own retries (a sustained outage, not a transient blip the
        # backoff already absorbs).
        chain: list[catalog.ResolvedEndpoint] = [
            catalog.ResolvedEndpoint(model=self._model, api_key=self.config.api_key,
                                     api_base=None, supports_tools=True, label=self._model)
        ]
        chain += [catalog.resolve_endpoint(spec) for spec in self.config.fallback_models]

        wants_tools = bool(tools)
        last_exc: Exception | None = None
        for idx, ep in enumerate(chain):
            # A chat-only endpoint (e.g. the ClaudeCode RP bridge) cannot run Sheldon's tool loop —
            # it ignores caller tools. Skip it for any tool-bearing turn so a lookup is never silently
            # dropped; it can still serve a tools=None turn (agent.py's forced-final iteration).
            if wants_tools and not ep.supports_tools:
                logger.warning(
                    f"[llm-failover] skipping chat-only endpoint '{ep.label}' for a tool-bearing turn"
                )
                continue
            kwargs = dict(base_kwargs, model=ep.model)
            if ep.api_key:
                kwargs["api_key"] = ep.api_key
            if ep.api_base:
                kwargs["api_base"] = ep.api_base
            try:
                response = await self._acompletion_with_backoff(kwargs)
                if idx > 0:
                    logger.warning(
                        f"[llm-failover] primary chain failed; served by fallback #{idx} '{ep.label}'"
                    )
                return self._normalize(response)
            except litellm.ContextWindowExceededError:
                raise  # no model in the chain will fit a too-long prompt; let the agent truncate
            except Exception as e:
                last_exc = e
                logger.warning(
                    f"[llm-failover] '{ep.label}' unavailable ({type(e).__name__}) :: {str(e)[:100]}"
                )
                continue
        if last_exc is not None:
            raise last_exc
        # Reached only when every link was skipped (entire chain chat-only) for a tool-bearing turn.
        raise RuntimeError(
            "no tool-capable LLM endpoint available — the failover chain is entirely chat-only "
            "for a tool-bearing turn"
        )

    def _normalize(self, response) -> LLMResponse:
        """Build the normalized LLMResponse from a raw litellm response."""
        choice = response.choices[0]
        message = choice.message
        usage = response.usage or {}
        try:
            cost = completion_cost(completion_response=response)
        except Exception:
            cost = 0.0
        return LLMResponse(
            content=message.content,
            tool_calls=message.tool_calls if message.tool_calls else None,
            finish_reason=choice.finish_reason or "stop",
            input_tokens=getattr(usage, "prompt_tokens", 0),
            output_tokens=getattr(usage, "completion_tokens", 0),
            cost=cost,
            raw=response,
        )

    async def _acompletion_with_backoff(self, kwargs: dict[str, Any]):
        """Call litellm.acompletion, retrying transient API failures with exponential backoff +
        jitter. Permanent errors (context-window, auth) fail fast. This is what stops a single
        Anthropic overload/connection blip from becoming a user-visible "brain" failure — and it
        spreads retries out (jitter) so a fleet of concurrent turns don't all retry in lockstep."""
        attempts = max(3, self.config.num_retries + 1)
        base = 0.75
        last_exc: Exception | None = None
        for i in range(attempts):
            try:
                return await acompletion(**kwargs)
            except litellm.ContextWindowExceededError:
                raise
            except litellm.AuthenticationError as e:
                logger.error(f"LLM authentication failed (not retrying): {e}")
                raise
            except _RETRYABLE_LLM_ERRORS as e:
                last_exc = e
                if i >= attempts - 1:
                    logger.error(f"LLM failed after {attempts} attempts ({type(e).__name__}): {str(e)[:160]}")
                    raise
                delay = base * (2 ** i) + random.uniform(0, 0.4)
                logger.warning(
                    f"LLM transient {type(e).__name__} (attempt {i + 1}/{attempts}); "
                    f"retrying in {delay:.1f}s :: {str(e)[:120]}"
                )
                await asyncio.sleep(delay)
        assert last_exc is not None
        raise last_exc

    def count_tokens(self, messages: list[dict]) -> int:
        """Estimate token count for a set of messages."""
        try:
            return token_counter(model=self._model, messages=messages)
        except Exception:
            # Fallback: rough estimate
            total_chars = sum(
                len(m.get("content", "")) for m in messages if isinstance(m.get("content"), str)
            )
            return total_chars // 4

    def get_max_context(self) -> int:
        """Get the maximum context window for the configured model."""
        try:
            return get_max_tokens(self._model)
        except Exception:
            return 100_000  # Conservative default

    def supports_tools(self) -> bool:
        """Check if the configured model supports tool/function calling."""
        try:
            return litellm.supports_function_calling(model=self._model)
        except Exception:
            return True  # Assume yes for unknown models
