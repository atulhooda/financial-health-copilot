"""OpenAI-compatible /chat/completions adapter (Groq, Cerebras, OpenRouter ... via LLM_BASE_URL). COPILOT.md §2.

tool_choice "required" forces a tool call; if the provider rejects it, retry with "auto" and remember (D16).
Temperature 0. `strict` is not sent: support varies across compatible providers, and the validator checks anyway.
"""
from __future__ import annotations

import json
from typing import Literal

from app.copilot.llm.base import LLMError, LLMReply, ToolCall

_NO_REQUIRED: set[tuple[str, str]] = set()
FINISH = {"tool_calls": "tool_use", "stop": "end_turn", "length": "max_tokens", "content_filter": "refusal"}


def _messages(system: str, messages: list[dict]) -> list[dict]:
    out: list[dict] = [{"role": "system", "content": system}]
    for m in messages:
        if m["role"] == "user":
            out.append({"role": "user", "content": m["content"]})
        elif m["role"] == "assistant":
            out.append(m["reply"].raw)
        else:
            out += [{"role": "tool", "tool_call_id": r["id"], "content": r["content"]} for r in m["results"]]
    return out


class OpenAICompatClient:
    provider = "openai_compat"

    def __init__(self, model: str, api_key: str | None, base_url: str | None, timeout: float = 20.0, client=None,
                 sdk=None):
        if sdk is None:
            import openai as sdk
        self._sdk = sdk
        self.model, self.base_url = model, base_url or ""
        self.client = client or sdk.OpenAI(api_key=api_key, base_url=base_url or None, timeout=timeout, max_retries=1)

    def _create(self, system: str, messages: list[dict], tools: list[dict], choice: str, timeout: float | None):
        extra = {"timeout": timeout} if timeout is not None else {}
        return self.client.chat.completions.create(
            model=self.model, temperature=0, messages=_messages(system, messages), tool_choice=choice,
            tools=[{"type": "function", "function": {"name": t["name"], "description": t["description"],
                                                     "parameters": t["input_schema"]}} for t in tools], **extra)

    def chat(self, system: str, messages: list[dict], tools: list[dict],
             tool_choice: Literal["any", "auto"], timeout: float | None = None) -> LLMReply:
        o = self._sdk
        key = (self.base_url, self.model)
        choice = "auto" if tool_choice == "auto" or key in _NO_REQUIRED else "required"
        try:
            try:
                resp = self._create(system, messages, tools, choice, timeout)
            except o.BadRequestError as e:
                if choice == "required" and "tool_choice" in str(e):
                    _NO_REQUIRED.add(key)
                    resp = self._create(system, messages, tools, "auto", timeout)
                else:
                    raise
        except o.APIStatusError as e:
            raise LLMError(f"{type(e).__name__} {e.status_code}: {getattr(e, 'message', e)}") from e
        except (o.APIConnectionError, o.APITimeoutError) as e:
            raise LLMError(f"{type(e).__name__}: {e}") from e
        choice0 = resp.choices[0]
        msg = choice0.message
        calls, raw_calls = [], []
        for tc in msg.tool_calls or []:
            try:
                args = json.loads(tc.function.arguments or "{}")
            except json.JSONDecodeError:
                args = {"__invalid_json__": tc.function.arguments}
            calls.append(ToolCall(tc.id, tc.function.name, args if isinstance(args, dict) else {}))
            raw_calls.append({"id": tc.id, "type": "function",
                              "function": {"name": tc.function.name, "arguments": tc.function.arguments or "{}"}})
        raw = {"role": "assistant", "content": msg.content or ""}
        if raw_calls:
            raw["tool_calls"] = raw_calls
        stop = FINISH.get(choice0.finish_reason or "", choice0.finish_reason or "")
        if stop == "max_tokens":
            calls = []
        usage = {"input_tokens": getattr(resp.usage, "prompt_tokens", None),
                 "output_tokens": getattr(resp.usage, "completion_tokens", None)} if resp.usage else {}
        return LLMReply(msg.content or "", calls, stop, raw=raw, usage=usage)
