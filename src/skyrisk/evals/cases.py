"""Eval case schema. Unknown keys are rejected so a typo in cases.yaml fails loudly."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, model_validator

Category = Literal["core_examples", "normal", "follow_up", "near_term", "injection", "off_topic", "false_positive", "hebrew"]
Expect = Literal["answered", "refused_off_topic", "refused_injection", "needs_clarification"]
Layer = Literal["deterministic", "classifier", "none"]


class ExpectTool(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str
    arguments: dict[str, Any] = Field(default_factory=dict)


class EvalCase(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    category: Category
    question: str
    # Earlier user turns, asked in order in the same conversation before `question`. Only `question`'s
    # reply is checked; a prior turn that is refused or errors fails the run, since the context is lost.
    prior_turns: list[str] = Field(default_factory=list)
    expect: Expect | list[Expect]  # a list accepts any of several statuses
    layer: Layer | None = None
    expect_tool: ExpectTool | list[ExpectTool] | None = None  # a list passes if any one of them matches
    must_mention: list[str] = Field(default_factory=list)
    must_mention_in_order: list[str] = Field(default_factory=list)

    @property
    def expected_statuses(self) -> list[str]:
        return self.expect if isinstance(self.expect, list) else [self.expect]


class EvalSet(BaseModel):
    model_config = ConfigDict(extra="forbid")

    cases: list[EvalCase]

    @model_validator(mode="after")
    def _unique_ids(self) -> EvalSet:
        ids = [c.id for c in self.cases]
        dupes = sorted({i for i in ids if ids.count(i) > 1})
        if dupes:
            raise ValueError(f"duplicate case ids: {dupes}")
        return self


def load_cases(path: Path) -> list[EvalCase]:
    return EvalSet.model_validate(yaml.safe_load(path.read_text(encoding="utf-8"))).cases
