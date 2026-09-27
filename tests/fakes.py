"""Scripted fake LLM provider for offline agent tests."""

from __future__ import annotations

import json
from dataclasses import dataclass, field

from skyrisk.agent.providers.base import ProviderUnavailable, Step, ToolCall


def answer(text="ok", scores=(), status="answered", limitations=("Scores are relative.",)) -> Step:
    return Step(final_text=json.dumps({
        "status": status, "answer": text, "hubs_referenced": [s[0] for s in scores],
        "scores_cited": [{"hub_id": h, "hazard": z, "score": v} for h, z, v in scores],
        "assumptions_and_limitations": list(limitations), "data_sources": ["rank_hubs"],
    }))


def call(name, **arguments) -> Step:
    return Step(tool_calls=[ToolCall(id=f"call_{name}", name=name, arguments=arguments)])


@dataclass
class FakeSession:
    provider: "FakeProvider"

    def step(self, tool_results, *, allow_tools=True, feedback=None):
        self.provider.received.append({"tool_results": tool_results, "allow_tools": allow_tools,
                                       "feedback": feedback})
        if not self.provider.script:
            raise AssertionError("fake provider script exhausted")
        return self.provider.script.pop(0)


@dataclass
class FakeProvider:
    script: list = field(default_factory=list)
    name: str = "fake:primary"
    unavailable: bool = False
    error: Exception | None = None
    received: list = field(default_factory=list)
    histories: list = field(default_factory=list)

    def start_turn(self, system, history, user_message, tools, answer_schema):
        if self.error is not None:
            raise self.error
        if self.unavailable:
            raise ProviderUnavailable(f"{self.name} is down")
        self.histories.append(list(history))
        return FakeSession(self)
