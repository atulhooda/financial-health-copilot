"""LLM adapters (COPILOT.md §2, D16) against fake SDKs: no network."""
from types import SimpleNamespace

import pytest

from app.copilot.llm import LLMUnavailable, get_llm
from app.copilot.llm.anthropic import _NO_FORCED_TOOL_CHOICE, AnthropicClient
from app.copilot.llm.base import LLMError, LLMReply, MaskingLLM, ToolCall
from app.copilot.llm.openai_compat import _NO_REQUIRED, OpenAICompatClient
from app.copilot.masking import Masker
from app.copilot.tools import RESPOND_SPEC, TOOL_SPECS


class APIStatusError(Exception):
    def __init__(self, msg, status_code=400):
        super().__init__(msg)
        self.status_code, self.message = status_code, msg


class BadRequestError(APIStatusError):
    pass


class APIConnectionError(Exception):
    pass


class APITimeoutError(APIConnectionError):
    pass


SDK = SimpleNamespace(APIStatusError=APIStatusError, BadRequestError=BadRequestError,
                      APIConnectionError=APIConnectionError, APITimeoutError=APITimeoutError)


class Recorder:
    def __init__(self, replies):
        self.calls, self.replies = [], list(replies)

    def create(self, **kwargs):
        self.calls.append(kwargs)
        r = self.replies.pop(0)
        if isinstance(r, Exception):
            raise r
        return r


def anthropic_reply(blocks, stop="tool_use"):
    return SimpleNamespace(content=blocks, stop_reason=stop, usage=SimpleNamespace(input_tokens=10, output_tokens=5))


def tool_use(name, args, id_="tu_1"):
    return SimpleNamespace(type="tool_use", id=id_, name=name, input=args)


def make_anthropic(replies, model="m-test"):
    rec = Recorder(replies)
    client = AnthropicClient(model, "k", client=SimpleNamespace(messages=rec), sdk=SDK)
    return client, rec


TOOLS = [*TOOL_SPECS, RESPOND_SPEC]


def test_anthropic_request_shape_and_reply():
    client, rec = make_anthropic([anthropic_reply([tool_use("respond", {"language": "en", "statements": []})])])
    reply = client.chat("sys", [{"role": "user", "content": "hi"}], TOOLS, "any")
    kw = rec.calls[0]
    assert "temperature" not in kw and kw["tool_choice"] == {"type": "any"} and kw["system"] == "sys"
    assert next(t for t in kw["tools"] if t["name"] == "respond")["strict"] is True
    assert reply.tool_calls == [ToolCall("tu_1", "respond", {"language": "en", "statements": []})]


def test_anthropic_low_effort_per_call_timeout_and_effort_fallback():
    from app.copilot.llm.anthropic import _NO_EFFORT

    _NO_EFFORT.discard("m-effort")
    ok = anthropic_reply([tool_use("get_metrics", {})])
    rec = Recorder([BadRequestError("output_config.effort: not supported for this model"), ok, ok])
    client = AnthropicClient("m-effort", "k", effort="low", client=SimpleNamespace(messages=rec), sdk=SDK)
    client.chat("s", [{"role": "user", "content": "q"}], TOOLS, "any", timeout=7.5)
    client.chat("s", [{"role": "user", "content": "q"}], TOOLS, "any")
    assert rec.calls[0]["output_config"] == {"effort": "low"} and rec.calls[0]["timeout"] == 7.5
    assert "output_config" not in rec.calls[1] and "output_config" not in rec.calls[2]  # remembered


def test_anthropic_forced_tool_choice_falls_back_to_auto_and_remembers():
    _NO_FORCED_TOOL_CHOICE.discard("m-forced")
    err = BadRequestError("tool_choice: type 'tool' and 'any' are not supported for this model.")
    ok = anthropic_reply([tool_use("get_metrics", {})])
    client, rec = make_anthropic([err, ok, ok], model="m-forced")
    client.chat("s", [{"role": "user", "content": "q"}], TOOLS, "any")
    client.chat("s", [{"role": "user", "content": "q"}], TOOLS, "any")
    assert [c["tool_choice"]["type"] for c in rec.calls] == ["any", "auto", "auto"]


def test_anthropic_history_is_sent_back_unchanged_with_tool_results():
    raw = [SimpleNamespace(type="thinking", thinking="", signature="sig"), tool_use("forecast", {}, "tu_9")]
    prev = LLMReply("", [ToolCall("tu_9", "forecast", {})], "tool_use", raw=raw)
    client, rec = make_anthropic([anthropic_reply([SimpleNamespace(type="text", text="done")], "end_turn")])
    client.chat("s", [{"role": "user", "content": "q"}, {"role": "assistant", "reply": prev},
                      {"role": "tool", "results": [{"id": "tu_9", "content": "{}", "is_error": True}]}], TOOLS, "any")
    msgs = rec.calls[0]["messages"]
    assert msgs[1] == {"role": "assistant", "content": raw}  # thinking block and signature untouched
    assert msgs[2] == {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "tu_9", "content": "{}",
                                                    "is_error": True}]}


def test_anthropic_refusal_truncation_and_errors():
    client, _ = make_anthropic([anthropic_reply([], "refusal")])
    assert client.chat("s", [{"role": "user", "content": "q"}], TOOLS, "any").stop_reason == "refusal"
    client, _ = make_anthropic([anthropic_reply([tool_use("respond", {"language": "en"})], "max_tokens")])
    assert client.chat("s", [{"role": "user", "content": "q"}], TOOLS, "any").tool_calls == []  # never run a cut call
    for exc in (APIStatusError("overloaded", 529), APIConnectionError("offline"), BadRequestError("bad schema")):
        client, _ = make_anthropic([exc])
        with pytest.raises(LLMError):
            client.chat("s", [{"role": "user", "content": "q"}], TOOLS, "any")


def openai_reply(calls=(), content=None, finish="tool_calls"):
    tcs = [SimpleNamespace(id=i, function=SimpleNamespace(name=n, arguments=a)) for i, n, a in calls]
    msg = SimpleNamespace(content=content, tool_calls=tcs or None)
    return SimpleNamespace(choices=[SimpleNamespace(message=msg, finish_reason=finish)],
                           usage=SimpleNamespace(prompt_tokens=3, completion_tokens=4))


def test_openai_compat_request_shape_fallback_and_parsing():
    _NO_REQUIRED.clear()
    rec = Recorder([BadRequestError("tool_choice 'required' is not supported"),
                    openai_reply([("c1", "respond", '{"language": "en", "statements": []}'), ("c2", "forecast", "{oops")])])
    client = OpenAICompatClient("m", "k", "https://example.invalid/v1",
                                client=SimpleNamespace(chat=SimpleNamespace(completions=rec)), sdk=SDK)
    reply = client.chat("sys", [{"role": "user", "content": "q"}], TOOLS, "any")
    assert [c["tool_choice"] for c in rec.calls] == ["required", "auto"] and rec.calls[0]["temperature"] == 0
    assert rec.calls[0]["messages"][0] == {"role": "system", "content": "sys"}
    assert reply.tool_calls[0].args == {"language": "en", "statements": []}
    assert "__invalid_json__" in reply.tool_calls[1].args
    assert reply.raw["tool_calls"][0]["function"]["name"] == "respond"


def test_masking_wrapper_masks_user_text_and_tool_results_only():
    seen = {}

    class Inner:
        provider, model = "fake", "fake"

        def chat(self, system, messages, tools, tool_choice, timeout=None):
            seen["messages"] = messages
            return LLMReply("", [], "end_turn")

    raw = object()
    MaskingLLM(Inner(), Masker({"Ramesh Kulkarni": "[CONTACT_01]"})).chat(
        "sys", [{"role": "user", "content": "pay Ramesh at 9876543210"},
                {"role": "assistant", "reply": LLMReply("", [], "tool_use", raw=raw)},
                {"role": "tool", "results": [{"id": "1", "content": "{\"payee\": \"Ramesh Kulkarni\"}"}]}], [], "any")
    m = seen["messages"]
    assert m[0]["content"] == "pay [CONTACT_01] at [PHONE]"
    assert m[1]["reply"].raw is raw and "[CONTACT_01]" in m[2]["results"][0]["content"]


def test_config_comes_from_env(monkeypatch):
    from app.core.config import get_settings

    assert get_llm("none").provider == "none"
    monkeypatch.setenv("LLM_MODEL", "")
    get_settings.cache_clear()
    try:
        with pytest.raises(LLMUnavailable, match="LLM_MODEL"):
            get_llm("anthropic")
    finally:
        get_settings.cache_clear()
