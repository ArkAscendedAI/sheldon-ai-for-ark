"""LLM provider abstraction using LiteLLM.

Provides a unified async interface for calling any LLM with tool support.
LiteLLM handles translation between providers (Anthropic, OpenAI, Google,
OpenRouter) behind the scenes.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

import litellm
from litellm import acompletion, completion_cost, get_max_tokens, token_counter

logger = logging.getLogger(__name__)


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
        kwargs: dict[str, Any] = {
            "model": self._model,
            "messages": messages,
            "max_tokens": self.config.max_tokens,
            "temperature": self.config.temperature,
            "timeout": self.config.timeout,
            "num_retries": self.config.num_retries,
            "api_key": self.config.api_key,
        }

        if tools:
            kwargs["tools"] = tools
            kwargs["tool_choice"] = tool_choice

        try:
            response = await acompletion(**kwargs)
        except litellm.ContextWindowExceededError:
            raise
        except litellm.AuthenticationError as e:
            logger.error(f"LLM authentication failed: {e}")
            raise
        except litellm.RateLimitError as e:
            logger.warning(f"LLM rate limited: {e}")
            raise
        except litellm.APIError as e:
            logger.error(f"LLM API error: {e}")
            raise

        choice = response.choices[0]
        message = choice.message
        usage = response.usage or {}

        # Calculate cost
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
