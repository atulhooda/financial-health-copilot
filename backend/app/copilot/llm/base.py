"""The LLM client contract (COPILOT.md §2) and the masking wrapper every adapter sits behind.

Messages use one neutral shape; adapters convert it to their provider's format:
  {"role": "user", "content": str}
  {"role": "assistant", "reply": LLMReply}          # appended unchanged (provider content, thinking included)
  {"role": "tool", "results": [{"id": str, "content": str, "is_error": bool}]}
Tool specs are {"name", "description", "input_schema", "strict"?}.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Literal, Protocol

from app.copilot.masking import Masker


class LLMUnavailable(Exception):
    """No provider configured (LLM_PROVIDER=none): answer from templates."""


class LLMError(Exception):
    """Transport, auth, rate-limit or API errors: the turn falls back to templates (logged)."""


@dataclass
class ToolCall:
    id: str
    name: str
    args: dict


@dataclass
class LLMReply:
    text: str
    tool_calls: list[ToolCall]
    stop_reason: str  # end_turn | tool_use | max_tokens | refusal | ... (provider values, normalised where needed)
    raw: Any = None  # the provider's assistant content, re-sent unchanged on the next call
    usage: dict = field(default_factory=dict)


class LLMClient(Protocol):
    provider: str
    model: str

    def chat(self, system: str, messages: list[dict], tools: list[dict],
             tool_choice: Literal["any", "auto"], timeout: float | None = None) -> LLMReply: ...


SAFE_KEYS = frozenset({"id", "kind", "display"})  # registry fields our code generates from numbers


def mask_tool_content(masker: Masker, content: str) -> str:
    """Tool results are JSON: mask every string value, leaving registry ids and display strings intact (unless one
    looks like PII). Anything that isn't JSON is masked as plain text."""
    try:
        obj = json.loads(content)
    except (TypeError, ValueError):
        return masker.mask(content)
    return json.dumps(masker.mask_obj(obj, safe_keys=SAFE_KEYS), ensure_ascii=False)


class MaskingLLM:
    """Every outbound string goes through the masker: the system prompt, user messages (regex + known names + best-
    effort first names) and tool results. Assistant turns are the provider's own output and are re-sent unchanged,
    because providers bind thinking blocks to the exact content they produced."""

    def __init__(self, inner: LLMClient, masker: Masker):
        self.inner, self.masker = inner, masker
        self.provider, self.model = inner.provider, inner.model

    def chat(self, system: str, messages: list[dict], tools: list[dict],
             tool_choice: Literal["any", "auto"], timeout: float | None = None) -> LLMReply:
        masked = []
        for m in messages:
            if m["role"] == "user":
                masked.append({"role": "user", "content": self.masker.mask(m["content"], free_text=True)})
            elif m["role"] == "tool":
                masked.append({"role": "tool", "results": [
                    {**r, "content": mask_tool_content(self.masker, r["content"])} for r in m["results"]]})
            else:
                masked.append(m)
        return self.inner.chat(self.masker.mask(system), masked, tools, tool_choice, timeout=timeout)
