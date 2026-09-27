"""Claude (Anthropic Messages API) adapter: manual tool loop, strict tools, structured final answer."""

from __future__ import annotations

import time
from collections.abc import Callable
from typing import Any

import anthropic

from skyrisk.agent.providers.base import (
    CallUsage,
    DEFAULT_RETRIES,
    ErrorKind,
    HistoryTurn,
    ProviderResponseError,
    Step,
    ToolCall,
    ToolDef,
    ToolResultMsg,
    call_with_retries,
)

MAX_PAUSE_CONTINUATIONS = 3


# --- pure translation ------------------------------------------------------------------

def to_tools(tools: list[ToolDef]) -> list[dict[str, Any]]:
    return [
        {"name": t.name, "description": t.description, "input_schema": t.parameters, "strict": True}
        for t in tools
    ]


def to_messages(history: list[HistoryTurn], user_message: str) -> list[dict[str, Any]]:
    messages: list[dict[str, Any]] = []
    for turn in history:
        messages.append({"role": "user", "content": turn.user})
        messages.append({"role": "assistant", "content": turn.assistant})
    messages.append({"role": "user", "content": user_message})
    return messages


def to_tool_results(results: list[ToolResultMsg]) -> dict[str, Any]:
    return {
        "role": "user",
        "content": [
            {"type": "tool_result", "tool_use_id": r.call_id, "content": r.content, "is_error": r.is_error}
            for r in results
        ],
    }


def usage_of(response: Any, model: str) -> CallUsage:
    u = response.usage
    return CallUsage(model=model, input_tokens=u.input_tokens, output_tokens=u.output_tokens,
                     cache_read_tokens=getattr(u, "cache_read_input_tokens", None) or 0,
                     cache_write_tokens=getattr(u, "cache_creation_input_tokens", None) or 0)


def parse_response(response: Any) -> Step | None:
    """Translate a Message into a Step. Returns None for pause_turn (caller re-sends)."""
    reason = response.stop_reason
    if reason == "pause_turn":
        return None
    if reason == "tool_use":
        calls = [
            ToolCall(id=b.id, name=b.name, arguments=dict(b.input))
            for b in response.content if b.type == "tool_use"
        ]
        return Step(tool_calls=calls)
    if reason == "end_turn":
        text = next((b.text for b in response.content if b.type == "text"), None)
        if text is None:
            raise ProviderResponseError("end_turn without a text block")
        return Step(final_text=text)
    if reason == "refusal":
        details = getattr(response, "stop_details", None)
        category = getattr(details, "category", None)
        raise ProviderResponseError(f"model refused (category={category})")
    raise ProviderResponseError(f"unusable stop_reason: {reason}")


# Anthropic reports exhausted credits / spend limits as a 4xx whose message says so.
_QUOTA_PHRASES = ("credit balance", "usage limit", "spend limit")


def _error_type(error: anthropic.APIStatusError) -> str | None:
    body = error.body if isinstance(error.body, dict) else {}
    inner = body.get("error") if isinstance(body.get("error"), dict) else body
    return inner.get("type") if isinstance(inner, dict) else None


def classify_error(error: Exception) -> ErrorKind:
    if isinstance(error, anthropic.APIStatusError):
        message = str(error).lower()
        if _error_type(error) == "billing_error" or any(p in message for p in _QUOTA_PHRASES):
            return "misconfigured"
        if isinstance(error, anthropic.RateLimitError) or error.status_code >= 500:
            return "transient"
        return "fatal"
    if isinstance(error, anthropic.APIConnectionError):
        return "transient"
    return "fatal"


# --- network ---------------------------------------------------------------------------

class _Session:
    def __init__(self, provider: AnthropicProvider, system: str, messages: list[dict[str, Any]],
                 tools: list[dict[str, Any]], answer_schema: dict[str, Any]) -> None:
        self._p = provider
        self._system = [{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}]
        self._messages = messages
        self._tools = tools
        self._schema = answer_schema
        self.usage: list[CallUsage] = []  # every call in this turn, for cost accounting

    def step(self, tool_results: list[ToolResultMsg] | None, *, allow_tools: bool = True,
             feedback: str | None = None) -> Step:
        if tool_results:
            self._messages.append(to_tool_results(tool_results))
        if feedback:
            self._messages.append({"role": "user", "content": feedback})
        for _ in range(MAX_PAUSE_CONTINUATIONS + 1):
            response = self._call(allow_tools)
            self.usage.append(usage_of(response, self._p.model))
            # Append full content (incl. thinking blocks) to keep the turn valid.
            self._messages.append({"role": "assistant", "content": response.content})
            step = parse_response(response)
            if step is not None:
                return step
        raise ProviderResponseError("too many pause_turn continuations")

    def _call(self, allow_tools: bool) -> Any:
        return call_with_retries(
            lambda: self._p.client.messages.create(
                model=self._p.model,
                max_tokens=self._p.max_tokens,
                system=self._system,
                messages=self._messages,
                tools=self._tools,
                tool_choice={"type": "auto"} if allow_tools else {"type": "none"},
                thinking={"type": "adaptive"},
                output_config={"effort": self._p.effort,
                               "format": {"type": "json_schema", "schema": self._schema}},
            ),
            classify_error, name=self._p.name, retries=self._p.retries, sleep=self._p.sleep,
        )


class AnthropicProvider:
    def __init__(self, client: anthropic.Anthropic, model: str, effort: str = "medium",
                 max_tokens: int = 16000, *, retries: int = DEFAULT_RETRIES,
                 sleep: Callable[[float], None] = time.sleep) -> None:
        # Retries are ours (they skip quota errors), so the SDK's own retries are off.
        self.client = client.with_options(max_retries=0)
        self.retries = retries
        self.sleep = sleep
        self.model = model
        self.effort = effort
        self.max_tokens = max_tokens
        self.name = f"anthropic:{model}"

    def start_turn(self, system: str, history: list[HistoryTurn], user_message: str,
                   tools: list[ToolDef], answer_schema: dict[str, Any]) -> _Session:
        return _Session(self, system, to_messages(history, user_message), to_tools(tools), answer_schema)
