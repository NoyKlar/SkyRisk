import json
from types import SimpleNamespace

import anthropic
import httpx2
import openai
import pytest
from anthropic.types import Message

from skyrisk.agent.providers import anthropic_provider as ap
from skyrisk.agent.providers import openai_provider as op
from skyrisk.agent.providers.base import (
    HistoryTurn,
    ProviderMisconfigured,
    ProviderResponseError,
    ProviderUnavailable,
    ToolDef,
    ToolResultMsg,
    call_with_retries,
)

REQUEST = httpx2.Request("POST", "https://api.example.test/v1")


def _message(stop_reason, content):
    return Message.model_validate({
        "id": "msg_1", "type": "message", "role": "assistant", "model": "claude-sonnet-5",
        "stop_reason": stop_reason, "stop_sequence": None, "content": content,
        "usage": {"input_tokens": 10, "output_tokens": 5},
    })


def test_anthropic_tool_use_parsed():
    msg = _message("tool_use", [
        {"type": "text", "text": "Let me check."},
        {"type": "tool_use", "id": "toolu_1", "name": "rank_hubs", "input": {"hazard": "winter"}},
    ])
    step = ap.parse_response(msg)
    assert step.tool_calls[0].id == "toolu_1" and step.tool_calls[0].arguments == {"hazard": "winter"}


def test_anthropic_end_turn_returns_text_and_pause_returns_none():
    assert ap.parse_response(_message("end_turn", [{"type": "text", "text": "{}"}])).final_text == "{}"
    assert ap.parse_response(_message("pause_turn", [])) is None


@pytest.mark.parametrize("reason", ["max_tokens", "refusal"])
def test_anthropic_unusable_stops_raise(reason):
    with pytest.raises(ProviderResponseError):
        ap.parse_response(_message(reason, [{"type": "text", "text": "partial"}]))


def test_anthropic_translation_shapes():
    tools = ap.to_tools([ToolDef("t", "d", {"type": "object"})])
    assert tools == [{"name": "t", "description": "d", "input_schema": {"type": "object"}, "strict": True}]
    msgs = ap.to_messages([HistoryTurn("q1", "a1")], "q2")
    assert [m["role"] for m in msgs] == ["user", "assistant", "user"]
    result = ap.to_tool_results([ToolResultMsg("toolu_1", "oops", True)])
    assert result["content"][0] == {"type": "tool_result", "tool_use_id": "toolu_1", "content": "oops", "is_error": True}


def test_openai_function_calls_parsed():
    response = SimpleNamespace(status="completed", output=[
        SimpleNamespace(type="reasoning"),
        SimpleNamespace(type="function_call", call_id="call_1", name="weather_stat",
                        arguments=json.dumps({"hub_ids": ["denver"]})),
    ], output_text="")
    step = op.parse_response(response)
    assert step.tool_calls[0].id == "call_1" and step.tool_calls[0].arguments == {"hub_ids": ["denver"]}


def test_openai_final_text_and_incomplete():
    done = SimpleNamespace(status="completed", output=[SimpleNamespace(type="message")], output_text='{"a": 1}')
    assert op.parse_response(done).final_text == '{"a": 1}'
    with pytest.raises(ProviderResponseError):
        op.parse_response(SimpleNamespace(status="incomplete", incomplete_details="max_output_tokens",
                                          output=[], output_text=""))


def test_openai_translation_shapes():
    tools = op.to_tools([ToolDef("t", "d", {"type": "object"})])
    assert tools[0] == {"type": "function", "name": "t", "description": "d",
                        "parameters": {"type": "object"}, "strict": True}
    outputs = op.to_tool_outputs([ToolResultMsg("call_1", "bad hub", True)])
    assert outputs == [{"type": "function_call_output", "call_id": "call_1", "output": "ERROR: bad hub"}]
    assert op.text_format({"type": "object"})["format"]["strict"] is True


def _status(sdk, code, body=None, message="err"):
    cls = {400: sdk.BadRequestError, 429: sdk.RateLimitError, 500: sdk.InternalServerError}[code]
    return cls(message, response=httpx2.Response(code, request=REQUEST), body=body)


@pytest.mark.parametrize("module,sdk", [(ap, anthropic), (op, openai)])
def test_error_classification(module, sdk):
    assert module.classify_error(sdk.APIConnectionError(request=REQUEST)) == "transient"
    assert module.classify_error(_status(sdk, 429)) == "transient"
    assert module.classify_error(_status(sdk, 500)) == "transient"
    assert module.classify_error(_status(sdk, 400)) == "fatal"
    assert module.classify_error(ValueError("bug")) == "fatal"


def test_openai_quota_429_is_misconfiguration():
    # Body recorded from the real response (the SDK passes the inner "error" object as body).
    body = {"message": "You have no credits remaining.", "type": "insufficient_quota",
            "param": None, "code": "credit_balance_exhausted"}
    assert op.classify_error(_status(openai, 429, body=body)) == "misconfigured"
    assert op.classify_error(_status(openai, 429, body={"error": body})) == "misconfigured"


def test_anthropic_low_credit_is_misconfiguration():
    body = {"type": "error", "error": {"type": "invalid_request_error",
            "message": "Your credit balance is too low to access the Anthropic API."}}
    error = _status(anthropic, 400, body=body, message=body["error"]["message"])
    assert ap.classify_error(error) == "misconfigured"
    billing = {"type": "error", "error": {"type": "billing_error", "message": "billing"}}
    assert ap.classify_error(_status(anthropic, 400, body=billing)) == "misconfigured"


class _Flaky:
    def __init__(self, *errors):
        self.errors, self.calls = list(errors), 0

    def __call__(self):
        self.calls += 1
        if self.errors:
            raise self.errors.pop(0)
        return "ok"


def test_retry_helper_retries_transient_then_succeeds():
    fn, waits = _Flaky(_status(openai, 429)), []
    assert call_with_retries(fn, op.classify_error, name="p", sleep=waits.append) == "ok"
    assert fn.calls == 2 and waits == [1.0]


def test_retry_helper_gives_up_as_unavailable():
    fn = _Flaky(*[_status(openai, 500)] * 3)
    with pytest.raises(ProviderUnavailable) as info:
        call_with_retries(fn, op.classify_error, name="p", retries=2, sleep=lambda s: None)
    assert not isinstance(info.value, ProviderMisconfigured) and fn.calls == 3


def test_retry_helper_never_retries_quota():
    body = {"type": "insufficient_quota", "code": "credit_balance_exhausted"}
    fn, waits = _Flaky(_status(openai, 429, body=body)), []
    with pytest.raises(ProviderMisconfigured):
        call_with_retries(fn, op.classify_error, name="p", sleep=waits.append)
    assert fn.calls == 1 and waits == []


def test_retry_helper_reraises_fatal():
    fn = _Flaky(_status(anthropic, 400))
    with pytest.raises(anthropic.BadRequestError):
        call_with_retries(fn, ap.classify_error, name="p", sleep=lambda s: None)
    assert fn.calls == 1


def test_adapters_disable_sdk_retries():
    assert ap.AnthropicProvider(anthropic.Anthropic(api_key="x"), "m").client.max_retries == 0
    assert op.OpenAIProvider(openai.OpenAI(api_key="x"), "m").client.max_retries == 0


def test_usage_is_recorded_per_call_without_double_counting_cache():
    a = SimpleNamespace(usage=SimpleNamespace(input_tokens=86, output_tokens=900, cache_read_input_tokens=3139,
                                              cache_creation_input_tokens=0))
    u = ap.usage_of(a, "claude-sonnet-5")
    assert (u.input_tokens, u.cache_read_tokens, u.cache_write_tokens, u.output_tokens) == (86, 3139, 0, 900)

    o = SimpleNamespace(usage=SimpleNamespace(input_tokens=5000, output_tokens=700,
                                              input_tokens_details=SimpleNamespace(cached_tokens=3000)))
    u = op.usage_of(o, "gpt-6-luna")
    assert (u.input_tokens, u.cache_read_tokens, u.output_tokens) == (2000, 3000, 700)
