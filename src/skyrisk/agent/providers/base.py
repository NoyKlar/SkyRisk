"""Provider-neutral types for the agent loop."""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, Literal, Protocol, TypeVar

from pydantic import BaseModel

T = TypeVar("T")
ErrorKind = Literal["transient", "misconfigured", "fatal"]

DEFAULT_RETRIES = 2
DEFAULT_BACKOFF_S = 1.0


class CallUsage(BaseModel):
    """Tokens billed for one model call. `input_tokens` excludes cache reads and writes."""

    model: str
    input_tokens: int
    output_tokens: int
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0


class ProviderUnavailable(Exception):
    """The provider could not serve the request (outage, rate limit, network). Triggers fallback."""


class ProviderMisconfigured(ProviderUnavailable):
    """The account cannot be served until someone fixes it (credits/quota exhausted).

    Still triggers fallback so the user gets an answer, but is reported as a
    configuration error rather than a transient outage, and is never retried.
    """


def call_with_retries(
    fn: Callable[[], T],
    classify: Callable[[Exception], ErrorKind],
    *,
    name: str,
    retries: int = DEFAULT_RETRIES,
    backoff_s: float = DEFAULT_BACKOFF_S,
    sleep: Callable[[float], None] = time.sleep,
) -> T:
    """Call a provider, retrying only transient errors (SDK retries are disabled on our clients)."""
    for attempt in range(retries + 1):
        try:
            return fn()
        except Exception as e:  # noqa: BLE001 - classified below; fatal errors are re-raised
            kind = classify(e)
            if kind == "misconfigured":
                raise ProviderMisconfigured(f"{name}: {e}") from e
            if kind == "fatal":
                raise
            if attempt == retries:
                raise ProviderUnavailable(f"{name}: {e}") from e
        sleep(backoff_s * 2**attempt)
    raise AssertionError("unreachable")


class ProviderResponseError(Exception):
    """The provider answered, but not with something usable (truncated, refused, malformed)."""


@dataclass(frozen=True)
class ToolDef:
    name: str
    description: str
    parameters: dict[str, Any]  # strict JSON schema


@dataclass(frozen=True)
class ToolCall:
    id: str
    name: str
    arguments: dict[str, Any]


@dataclass(frozen=True)
class ToolResultMsg:
    call_id: str
    content: str
    is_error: bool


@dataclass(frozen=True)
class HistoryTurn:
    """One completed exchange, in a form either provider can replay."""

    user: str
    assistant: str


@dataclass
class Step:
    """What one model call produced: tool calls to run, or the final answer text."""

    tool_calls: list[ToolCall] = field(default_factory=list)
    final_text: str | None = None


class TurnSession(Protocol):
    def step(self, tool_results: list[ToolResultMsg] | None, *, allow_tools: bool = True,
             feedback: str | None = None) -> Step:
        """Send tool results or corrective feedback (if any) and return the model's next step."""


class LLMProvider(Protocol):
    name: str  # e.g. "anthropic:claude-sonnet-5"

    def start_turn(
        self,
        system: str,
        history: list[HistoryTurn],
        user_message: str,
        tools: list[ToolDef],
        answer_schema: dict[str, Any],
    ) -> TurnSession: ...
