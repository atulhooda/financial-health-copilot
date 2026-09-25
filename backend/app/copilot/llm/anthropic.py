"""Anthropic Messages API adapter (COPILOT.md §2, D16).

- No sampling parameters: current Claude models reject `temperature`.
- tool_choice {"type": "any"} forces a tool call. Some models reject forced tool use with a 400 (e.g. Claude Opus
  5.5, Fable 5.1); the adapter then retries with {"type": "auto"} and remembers that for the model, for this
  process. Under auto, a plain-text reply is a validation failure (NO_RESPOND) in the orchestrator.
- `respond` is sent with strict: true (schema-valid arguments); length limits are checked by the validator.
- stop_reason "refusal" goes to the template path; tool calls cut off by max_tokens are never run.
- The full response content (thinking blocks included) is appended unchanged as the assistant turn.
"""
from __future__ import annotations

from typing import Literal

from app.copilot.llm.base import LLMError, LLMReply, ToolCall

MAX_TOKENS = 8000
_NO_FORCED_TOOL_CHOICE: set[str] = set()
_NO_EFFORT: set[str] = set()  # models that rejected output_config.effort (remembered for the process)


def _content(m: dict) -> dict:
    if m["role"] == "user":
        return {"role": "user", "content": m["content"]}
    if m["role"] == "assistant":
        return {"role": "assistant", "content": m["reply"].raw}
    return {"role": "user", "content": [{"type": "tool_result", "tool_use_id": r["id"], "content": r["content"],
                                         "is_error": bool(r.get("is_error"))} for r in m["results"]]}


class AnthropicClient:
    provider = "anthropic"

    def __init__(self, model: str, api_key: str | None, base_url: str | None = None, timeout: float = 20.0,
                 effort: str | None = None, client=None, sdk=None):
        if sdk is None:
            import anthropic as sdk
        self._sdk = sdk
        self.model = model
        self.effort = effort
        self.client = client or sdk.Anthropic(api_key=api_key or None, base_url=base_url or None, timeout=timeout,
                                              max_retries=1)

    def _create(self, system: str, messages: list[dict], tools: list[dict], choice: str, timeout: float | None):
        kwargs: dict = {
            "model": self.model, "max_tokens": MAX_TOKENS, "system": system,
            "messages": [_content(m) for m in messages],
            "tools": [{k: t[k] for k in ("name", "description", "input_schema", "strict") if k in t} for t in tools],
            "tool_choice": {"type": choice},
        }
        if self.effort and self.model not in _NO_EFFORT:
            kwargs["output_config"] = {"effort": self.effort}
        if timeout is not None:
            kwargs["timeout"] = timeout
        return self.client.messages.create(**kwargs)

    def chat(self, system: str, messages: list[dict], tools: list[dict],
             tool_choice: Literal["any", "auto"], timeout: float | None = None) -> LLMReply:
        a = self._sdk
        choice = "auto" if self.model in _NO_FORCED_TOOL_CHOICE else tool_choice
        try:
            for _ in range(3):  # at most: forced tool choice rejected, then effort rejected
                try:
                    resp = self._create(system, messages, tools, choice, timeout)
                    break
                except a.BadRequestError as e:
                    if choice == "any" and "tool_choice" in str(e):
                        _NO_FORCED_TOOL_CHOICE.add(self.model)  # D16: remember, and fall back to auto
                        choice = "auto"
                    elif self.effort and self.model not in _NO_EFFORT and "effort" in str(e):
                        _NO_EFFORT.add(self.model)  # this model has no effort control: run at its default
                    else:
                        raise
            else:
                raise LLMError("the request kept being rejected")
        except a.APIStatusError as e:  # 4xx/5xx incl. auth, permission, not found, rate limit, overloaded
            raise LLMError(f"{type(e).__name__} {e.status_code}: {getattr(e, 'message', e)}") from e
        except (a.APIConnectionError, a.APITimeoutError) as e:
            raise LLMError(f"{type(e).__name__}: {e}") from e
        text = "".join(b.text for b in resp.content if b.type == "text")
        calls = [ToolCall(b.id, b.name, dict(b.input) if isinstance(b.input, dict) else {})
                 for b in resp.content if b.type == "tool_use"]
        if resp.stop_reason == "max_tokens":
            calls = []  # a tool call cut off mid-way has incomplete input: never run it
        usage = {"input_tokens": resp.usage.input_tokens, "output_tokens": resp.usage.output_tokens}
        return LLMReply(text, calls, resp.stop_reason or "", raw=resp.content, usage=usage)
