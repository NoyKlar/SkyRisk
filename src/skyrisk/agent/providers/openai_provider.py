"""OpenAI (Responses API) adapter, used as the fallback provider."""

from __future__ import annotations

import json
import time
from collections.abc import Callable
from typing import Any

import openai

from skyrisk.agent.providers.base import (
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

ERROR_PREFIX = "ERROR: "


# --- pure translation ------------------------------------------------------------------

def to_tools(tools: list[ToolDef]) -> list[dict[str, Any]]:
    return [
        {"type": "function", "name": t.name, "description": t.description,
         "parameters": t.parameters, "strict": True}
        for t in tools
    ]


def to_input(history: list[HistoryTurn], user_message: str) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    for turn in history:
        items.append({"role": "user", "content": turn.user})
        items.append({"role": "assistant", "content": turn.assistant})
    items.append({"role": "user", "content": user_message})
    return items


def to_tool_outputs(results: list[ToolResultMsg]) -> list[dict[str, Any]]:
    # The Responses API has no is_error flag on function outputs; mark errors in-band.
    return [
        {"type": "function_call_output", "call_id": r.call_id,
         "output": (ERROR_PREFIX + r.content) if r.is_error else r.content}
        for r in results
    ]


def text_format(answer_schema: dict[str, Any]) -> dict[str, Any]:
    return {"format": {"type": "json_schema", "name": "agent_answer",
                       "schema": answer_schema, "strict": True}}


def parse_response(response: Any) -> Step:
    status = getattr(response, "status", "completed")
    if status not in (None, "completed"):
        raise ProviderResponseError(f"response status {status}: {getattr(response, 'incomplete_details', None)}")
    calls = []
    for item in response.output:
        if item.type == "function_call":
            try:
                arguments = json.loads(item.arguments)
            except json.JSONDecodeError:
                arguments = {"__invalid_json__": item.arguments}
            calls.append(ToolCall(id=item.call_id, name=item.name, arguments=arguments))
    if calls:
        return Step(tool_calls=calls)
    text = response.output_text
    if not text:
        raise ProviderResponseError("completed response without text output")
    return Step(final_text=text)


# OpenAI signals exhausted credits as a 429 with one of these codes/types.
QUOTA_CODES = {"insufficient_quota", "credit_balance_exhausted", "billing_hard_limit_reached"}


def _codes(error: openai.APIStatusError) -> set[str]:
    codes = {getattr(error, "code", None), getattr(error, "type", None)}
    body = error.body if isinstance(error.body, dict) else {}
    inner = body.get("error") if isinstance(body.get("error"), dict) else body
    codes |= {inner.get("code"), inner.get("type")}
    return {c for c in codes if isinstance(c, str)}


def classify_error(error: Exception) -> ErrorKind:
    if isinstance(error, openai.APIStatusError):
        if _codes(error) & QUOTA_CODES:
            return "misconfigured"
        if isinstance(error, openai.RateLimitError) or error.status_code >= 500:
            return "transient"
        return "fatal"
    if isinstance(error, openai.APIConnectionError):
        return "transient"
    return "fatal"


# --- network ---------------------------------------------------------------------------

class _Session:
    def __init__(self, provider: OpenAIProvider, system: str, items: list[dict[str, Any]],
                 tools: list[dict[str, Any]], answer_schema: dict[str, Any]) -> None:
        self._p = provider
        self._instructions = system
        self._input = items
        self._tools = tools
        self._text = text_format(answer_schema)

    def step(self, tool_results: list[ToolResultMsg] | None, *, allow_tools: bool = True,
             feedback: str | None = None) -> Step:
        if tool_results:
            self._input += to_tool_outputs(tool_results)
        if feedback:
            self._input.append({"role": "user", "content": feedback})
        response = call_with_retries(
            lambda: self._p.client.responses.create(
                model=self._p.model,
                instructions=self._instructions,
                input=self._input,
                tools=self._tools,
                tool_choice="auto" if allow_tools else "none",
                text=self._text,
                reasoning={"effort": self._p.effort},
                max_output_tokens=self._p.max_tokens,
                store=False,
                include=["reasoning.encrypted_content"],
            ),
            classify_error, name=self._p.name, retries=self._p.retries, sleep=self._p.sleep,
        )
        # Carry reasoning + function_call items forward within the turn.
        self._input += [item.model_dump(mode="json", exclude_none=True) for item in response.output]
        return parse_response(response)


class OpenAIProvider:
    def __init__(self, client: openai.OpenAI, model: str, effort: str = "medium",
                 max_tokens: int = 16000, *, retries: int = DEFAULT_RETRIES,
                 sleep: Callable[[float], None] = time.sleep) -> None:
        # Retries are ours (they skip quota errors), so the SDK's own retries are off.
        self.client = client.with_options(max_retries=0)
        self.retries = retries
        self.sleep = sleep
        self.model = model
        self.effort = effort
        self.max_tokens = max_tokens
        self.name = f"openai:{model}"

    def start_turn(self, system: str, history: list[HistoryTurn], user_message: str,
                   tools: list[ToolDef], answer_schema: dict[str, Any]) -> _Session:
        return _Session(self, system, to_input(history, user_message), to_tools(tools), answer_schema)
