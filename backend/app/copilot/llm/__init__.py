"""LLM adapters (COPILOT.md §2). Config comes only from env; no model name lives in code."""
from __future__ import annotations

import os

from app.copilot.llm.base import LLMClient, LLMError, LLMReply, LLMUnavailable, MaskingLLM, ToolCall

__all__ = ["LLMClient", "LLMError", "LLMReply", "LLMUnavailable", "MaskingLLM", "ToolCall", "get_llm"]


def get_llm(provider: str | None = None) -> LLMClient:
    """The configured adapter (NoneClient for LLM_PROVIDER=none). Fails clearly on missing config."""
    from app.core.config import get_settings

    s = get_settings()
    provider = (provider or s.llm_provider or "none").lower()
    if provider == "none":
        from app.copilot.llm.none import NoneClient

        return NoneClient()
    if not s.llm_model:
        raise LLMUnavailable(f"LLM_MODEL is required when LLM_PROVIDER={provider}")
    timeout = s.llm_timeout_s
    if provider == "anthropic":
        from app.copilot.llm.anthropic import AnthropicClient

        if not (s.llm_api_key or os.environ.get("ANTHROPIC_API_KEY")):
            raise LLMUnavailable("LLM_API_KEY (or ANTHROPIC_API_KEY) is required when LLM_PROVIDER=anthropic")
        return AnthropicClient(s.llm_model, s.llm_api_key, s.llm_base_url, timeout, s.llm_effort)
    if provider == "openai_compat":
        from app.copilot.llm.openai_compat import OpenAICompatClient

        if not s.llm_base_url or not s.llm_api_key:
            raise LLMUnavailable("LLM_BASE_URL and LLM_API_KEY are required when LLM_PROVIDER=openai_compat")
        return OpenAICompatClient(s.llm_model, s.llm_api_key, s.llm_base_url, timeout)
    raise LLMUnavailable(f"unknown LLM_PROVIDER {provider!r} (anthropic | openai_compat | none)")
