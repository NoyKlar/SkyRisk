"""The structured answer contract between the LLM and the application."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field


class ScoreCited(BaseModel):
    hub_id: str
    hazard: str = Field(description="'overall' or a hazard name, exactly as returned by a tool")
    score: float = Field(description="Copied from a tool result; never computed or estimated")


class AgentAnswer(BaseModel):
    status: Literal["answered", "refused_off_topic", "refused_injection", "needs_clarification"]
    answer: str = Field(description="Plain-language answer for a logistics analyst")
    hubs_referenced: list[str] = Field(description="Hub ids discussed in the answer")
    scores_cited: list[ScoreCited] = Field(
        description="Every risk score (0-100) mentioned in the answer, copied from tool results"
    )
    assumptions_and_limitations: list[str] = Field(
        min_length=1,
        description="Assumptions made (e.g. 'last year' = 2025) and data limits relevant to this answer",
    )
    data_sources: list[str] = Field(description="Tools and datasets the answer relies on")
