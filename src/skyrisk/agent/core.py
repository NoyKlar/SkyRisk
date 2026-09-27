"""Agent loop: guardrails -> tool loop on the primary provider (fallback on outage) -> validated, grounded answer."""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Literal

from pydantic import ValidationError

from skyrisk.agent import guardrails
from skyrisk.agent.providers.base import (
    CallUsage,
    HistoryTurn,
    LLMProvider,
    ProviderMisconfigured,
    ProviderResponseError,
    ProviderUnavailable,
    ToolDef,
    ToolResultMsg,
    TurnSession,
)
from skyrisk.agent.schema import AgentAnswer
from skyrisk.agent.tools import TOOLS, ScoreRef, ToolContext, run_tool, strict_json_schema

Log = Callable[[str], None]
Status = Literal["answered", "refused_off_topic", "refused_injection", "needs_clarification", "error"]

MAX_CORRECTIONS = 1  # regenerations allowed for an invalid or ungrounded answer

OFF_TOPIC_MESSAGE = (
    "I can only help with the weather and natural-hazard exposure of the company's distribution hubs, "
    "for example rankings, comparisons, why a hub scores the way it does, or historical weather stats."
)
INJECTION_MESSAGE = (
    "I can't follow instructions that change how I work or what the scores are. Scores come only from "
    "SkyRisk's deterministic scoring. Ask me about a hub's weather exposure and I'll help."
)
INVALID_INPUT_MESSAGE = "Please send a short question (up to {max_chars} characters) about the hubs' weather exposure."
UNAVAILABLE_MESSAGE = "The assistant is temporarily unavailable. Please try again in a few minutes."
UNVERIFIED_MESSAGE = (
    "I couldn't produce an answer whose numbers all match SkyRisk's data, so I won't guess. "
    "Try asking more specifically, e.g. for one hazard or one hub."
)


@dataclass
class AgentReply:
    status: Status
    text: str
    limitations: list[str] = field(default_factory=list)
    scores_cited: list[dict] = field(default_factory=list)
    hubs: list[str] = field(default_factory=list)
    served_by: str | None = None
    tools_used: list[str] = field(default_factory=list)
    tool_calls: list[dict] = field(default_factory=list)  # {"name", "arguments"} per call, in order
    guardrail: str | None = None  # which layer refused, if any
    warnings: list[str] = field(default_factory=list)
    usage: list[CallUsage] = field(default_factory=list)  # every model call behind this reply, for cost accounting


@dataclass
class Conversation:
    max_turns: int = 10
    history: list[HistoryTurn] = field(default_factory=list)

    def record(self, question: str, reply: AgentReply) -> None:
        # Refused turns stay out of history so injected text never reaches later turns.
        if reply.status not in ("answered", "needs_clarification"):
            return
        data = json.dumps({"tools_used": reply.tools_used, "scores_cited": reply.scores_cited})
        self.history.append(HistoryTurn(user=question, assistant=f"{reply.text}\n[data used: {data}]"))
        del self.history[: -self.max_turns]

    def reset(self) -> None:
        self.history.clear()


@dataclass
class _TurnState:
    tools_used: list[str] = field(default_factory=list)
    tool_calls: list[dict] = field(default_factory=list)
    scores: list[ScoreRef] = field(default_factory=list)
    tool_json: list[str] = field(default_factory=list)
    usage: list[CallUsage] = field(default_factory=list)


class Agent:
    def __init__(
        self,
        ctx: ToolContext,
        providers: list[LLMProvider],
        system_prompt: str,
        *,
        classifier: guardrails.Classifier | None = None,
        max_tool_rounds: int = 6,
        max_input_chars: int = 2000,
        log: Log = print,
    ) -> None:
        if not providers:
            raise ValueError("at least one provider is required")
        self._ctx = ctx
        self._providers = providers
        self._system = system_prompt
        self._classifier = classifier
        self._max_tool_rounds = max_tool_rounds
        self._max_input_chars = max_input_chars
        self._log = log
        self._tools = [ToolDef(t.name, t.description, t.json_schema()) for t in TOOLS]
        self._answer_schema = strict_json_schema(AgentAnswer)

    def ask(self, conversation: Conversation, question: str) -> AgentReply:
        reply = self._ask(conversation, question)
        conversation.record(question, reply)
        return reply

    def _ask(self, conversation: Conversation, question: str) -> AgentReply:
        rejection = guardrails.check_input(question, self._max_input_chars)
        if rejection is not None:
            if rejection.reason == "injection":
                return AgentReply("refused_injection", INJECTION_MESSAGE, guardrail=f"input: {rejection.detail}")
            return AgentReply("needs_clarification",
                              INVALID_INPUT_MESSAGE.format(max_chars=self._max_input_chars),
                              guardrail=f"input: {rejection.detail}")

        verdict = guardrails.safe_classify(self._classifier, question, self._log)
        classifier_usage = list(verdict.usage) if verdict is not None else []
        if verdict is not None and verdict.label == "off_topic":
            return AgentReply("refused_off_topic", OFF_TOPIC_MESSAGE, guardrail=f"classifier: {verdict.reason}",
                              usage=classifier_usage)
        if verdict is not None and verdict.label == "injection":
            return AgentReply("refused_injection", INJECTION_MESSAGE, guardrail=f"classifier: {verdict.reason}",
                              usage=classifier_usage)

        warnings = [] if verdict is not None or self._classifier is None else ["classifier skipped"]
        for provider in self._providers:
            try:
                reply = self._run_turn(provider, conversation, question)
            except ProviderMisconfigured as e:
                self._log(f"CONFIGURATION ERROR: {provider.name} cannot serve requests until its "
                          f"account is fixed (credits/quota exhausted): {e}; using next provider")
                warnings.append(f"{provider.name} misconfigured: credits/quota exhausted")
                continue
            except ProviderUnavailable as e:
                self._log(f"warning: {e}; trying next provider")
                warnings.append(f"{provider.name} unavailable")
                continue
            except ProviderResponseError as e:
                self._log(f"warning: {provider.name} returned an unusable response: {e}")
                return AgentReply("error", UNVERIFIED_MESSAGE, served_by=provider.name,
                                  warnings=warnings + [str(e)], usage=classifier_usage)
            reply.warnings = warnings + reply.warnings
            reply.usage = classifier_usage + reply.usage
            return reply
        return AgentReply("error", UNAVAILABLE_MESSAGE, warnings=warnings, usage=classifier_usage)

    def _run_turn(self, provider: LLMProvider, conversation: Conversation, question: str) -> AgentReply:
        session = provider.start_turn(self._system, conversation.history, question, self._tools,
                                      self._answer_schema)
        state = _TurnState(usage=getattr(session, "usage", []))  # the session appends as it calls
        step = session.step(None)
        rounds = 0
        corrections = 0
        while True:
            if step.tool_calls:
                if rounds >= self._max_tool_rounds:
                    raise ProviderResponseError("model kept calling tools after the tool-round limit")
                rounds += 1
                results = self._run_tools(step.tool_calls, state)
                step = session.step(results, allow_tools=rounds < self._max_tool_rounds)
                continue

            problems = self._problems(step.final_text or "", state)
            if isinstance(problems, AgentAnswer):
                return self._reply(problems, provider, state)
            if corrections >= MAX_CORRECTIONS:
                self._log(f"warning: answer rejected after correction: {problems}")
                return AgentReply("error", UNVERIFIED_MESSAGE, served_by=provider.name,
                                  tools_used=state.tools_used, tool_calls=state.tool_calls, warnings=problems,
                                  usage=list(state.usage))
            corrections += 1
            step = session.step(None, allow_tools=rounds < self._max_tool_rounds, feedback=(
                "Your previous answer was rejected by validation:\n- " + "\n- ".join(problems)
                + "\nUse only numbers returned by tools (call a tool if you need one) and answer again "
                "in the required JSON format."
            ))

    def _run_tools(self, calls, state: _TurnState) -> list[ToolResultMsg]:
        results = []
        for call in calls:
            outcome = run_tool(self._ctx, call.name, call.arguments)
            state.tools_used.append(call.name)
            state.tool_calls.append({"name": call.name, "arguments": call.arguments})
            if outcome.result is not None:
                state.scores += outcome.result.scores()
                state.tool_json.append(outcome.content)
            results.append(ToolResultMsg(call.id, outcome.content, outcome.is_error))
        return results

    def _problems(self, text: str, state: _TurnState) -> AgentAnswer | list[str]:
        """The validated answer, or the list of problems with it."""
        try:
            answer = AgentAnswer.model_validate_json(text)
        except ValidationError as e:
            return [f"answer does not match the schema: {e.errors(include_url=False)}"]
        if answer.status != "answered":
            return answer
        problems = guardrails.check_grounding(answer, state.scores, guardrails.tool_numbers(state.tool_json))
        return problems or answer

    @staticmethod
    def _reply(answer: AgentAnswer, provider: LLMProvider, state: _TurnState) -> AgentReply:
        return AgentReply(
            status=answer.status,
            text=answer.answer,
            limitations=answer.assumptions_and_limitations,
            scores_cited=[c.model_dump() for c in answer.scores_cited],
            hubs=answer.hubs_referenced,
            served_by=provider.name,
            tools_used=state.tools_used,
            tool_calls=state.tool_calls,
            usage=list(state.usage),
        )
