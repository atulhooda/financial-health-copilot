"""LLM_PROVIDER=none: no network. The orchestrator answers from templates (COPILOT.md §2)."""
from __future__ import annotations

from app.copilot.llm.base import LLMReply, LLMUnavailable


class NoneClient:
    provider = "none"
    model = "none"

    def chat(self, *args, **kwargs) -> LLMReply:
        raise LLMUnavailable("LLM_PROVIDER=none")
