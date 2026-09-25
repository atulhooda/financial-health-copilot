"""Test doubles: a scripted LLM that records every payload it receives, and one that fails if it is called."""
from __future__ import annotations

import copy
import json
from collections.abc import Callable
from typing import Literal

from app.copilot.llm.base import LLMReply, ToolCall

Step = LLMReply | Callable[[str, list[dict]], LLMReply]


class FakeLLM:
    """Replies from a script: each step is an LLMReply or a function (system, messages) -> LLMReply."""

    provider = "fake"
    model = "fake-model"

    def __init__(self, steps: list[Step]):
        self.steps = list(steps)
        self.payloads: list[dict] = []  # exactly what an adapter would send, after masking
        self.timeouts: list[float | None] = []

    def chat(self, system: str, messages: list[dict], tools: list[dict],
             tool_choice: Literal["any", "auto"], timeout: float | None = None) -> LLMReply:
        self.payloads.append({"system": system, "messages": copy.deepcopy(
            [m if m["role"] != "assistant" else {"role": "assistant"} for m in messages]), "tools": tools,
            "tool_choice": tool_choice})
        self.timeouts.append(timeout)
        if not self.steps:
            raise AssertionError("FakeLLM script exhausted")
        step = self.steps.pop(0)
        return step(system, messages) if callable(step) else step

    def payload_strings(self) -> list[str]:
        """Every string sent, for PII assertions."""
        return [json.dumps(p, ensure_ascii=False) for p in self.payloads]


class ExplodingLLM:
    """Fails the test if the orchestrator ever calls it (guards must short-circuit)."""

    provider = "exploding"
    model = "none"

    def chat(self, *args, **kwargs) -> LLMReply:
        raise AssertionError("the LLM must not be called for this input")


def call(name: str, args: dict, call_id: str = "t1") -> LLMReply:
    return LLMReply("", [ToolCall(call_id, name, args)], "tool_use", raw=[])


def text(t: str) -> LLMReply:
    return LLMReply(t, [], "end_turn", raw=[])
