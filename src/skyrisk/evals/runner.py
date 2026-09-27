"""Run eval cases against an agent, repeat each case N times, and aggregate the results."""

from __future__ import annotations

import math
import time
from collections import Counter
from collections.abc import Callable

from pydantic import BaseModel, computed_field

from skyrisk.agent.core import Agent, Conversation, Log
from skyrisk.agent.tools import ToolContext
from skyrisk.evals.cases import EvalCase
from skyrisk.evals.checks import check_run, refusing_layer

REFUSALS = ("refused_off_topic", "refused_injection")


class EvalMeta(BaseModel):
    started_at: str
    primary_model: str
    fallback_model: str | None
    classifier_model: str | None
    score_run_id: int
    scoring_config_version: str


class RunResult(BaseModel):
    status: str
    passed: bool
    reasons: list[str]
    latency_s: float
    served_by: str | None
    refused_by: str | None
    tools: list[str]


class CaseResult(BaseModel):
    id: str
    category: str
    question: str
    expect: list[str]  # any of these statuses passes
    runs: list[RunResult]

    @computed_field
    @property
    def passes(self) -> int:
        return sum(r.passed for r in self.runs)

    @computed_field
    @property
    def passed(self) -> bool:
        return self.passes == len(self.runs)  # every run must pass

    @computed_field
    @property
    def flaky(self) -> bool:
        return 0 < self.passes < len(self.runs)


class CategoryStat(BaseModel):
    category: str
    cases: int
    passed: int
    runs: int
    runs_passed: int


class GuardrailStats(BaseModel):
    in_scope_runs: int              # runs of cases expected to be answered
    in_scope_refused: int           # ...that a guardrail or the model refused (false positives)
    false_positives_by_layer: dict[str, int]
    must_refuse_runs: int           # runs of off-topic / injection cases
    must_refuse_answered: int       # ...that were answered anyway (misses)

    @computed_field
    @property
    def false_positive_rate(self) -> float:
        return self.in_scope_refused / self.in_scope_runs if self.in_scope_runs else 0.0

    @computed_field
    @property
    def miss_rate(self) -> float:
        return self.must_refuse_answered / self.must_refuse_runs if self.must_refuse_runs else 0.0


class LatencyStats(BaseModel):
    p50_s: float
    p95_s: float
    max_s: float


class EvalReport(BaseModel):
    meta: EvalMeta
    repeat: int
    cases: list[CaseResult]
    categories: list[CategoryStat]
    guardrails: GuardrailStats
    latency: LatencyStats
    served_by: dict[str, int]

    @computed_field
    @property
    def passed(self) -> bool:
        return all(c.passed for c in self.cases)

    @computed_field
    @property
    def cases_passed(self) -> int:
        return sum(c.passed for c in self.cases)


def _run_once(agent: Agent, case: EvalCase, ctx: ToolContext, now: Callable[[], float]) -> RunResult:
    start = now()
    try:
        reply = agent.ask(Conversation(), case.question)
    except Exception as e:  # noqa: BLE001 - one broken run must not stop the eval
        return RunResult(status="exception", passed=False, reasons=[f"exception: {type(e).__name__}: {e}"],
                         latency_s=now() - start, served_by=None, refused_by=None, tools=[])
    latency = now() - start
    reasons = check_run(case, reply, ctx)
    return RunResult(status=reply.status, passed=not reasons, reasons=reasons, latency_s=latency,
                     served_by=reply.served_by, refused_by=refusing_layer(reply),
                     tools=list(dict.fromkeys(reply.tools_used)))


def _percentile(sorted_values: list[float], pct: float) -> float:
    if not sorted_values:
        return 0.0
    return sorted_values[max(0, math.ceil(pct / 100 * len(sorted_values)) - 1)]


def run_evals(agent: Agent, cases: list[EvalCase], ctx: ToolContext, meta: EvalMeta, *, repeat: int = 1,
              now: Callable[[], float] = time.perf_counter, log: Log = print) -> EvalReport:
    if repeat < 1:
        raise ValueError("repeat must be at least 1")
    results = []
    for case in cases:
        result = CaseResult(id=case.id, category=case.category, question=case.question, expect=case.expected_statuses,
                            runs=[_run_once(agent, case, ctx, now) for _ in range(repeat)])
        mean = sum(r.latency_s for r in result.runs) / repeat
        log(f"{'✓' if result.passed else '✗'} {case.id:<32} {result.passes}/{repeat}  {mean:.1f}s")
        results.append(result)
    return _aggregate(meta, repeat, results)


def _aggregate(meta: EvalMeta, repeat: int, results: list[CaseResult]) -> EvalReport:
    categories = []
    for category in dict.fromkeys(c.category for c in results):
        group = [c for c in results if c.category == category]
        categories.append(CategoryStat(
            category=category, cases=len(group), passed=sum(c.passed for c in group),
            runs=sum(len(c.runs) for c in group), runs_passed=sum(c.passes for c in group),
        ))

    in_scope = [r for c in results if "answered" in c.expect for r in c.runs]
    refused = [r for r in in_scope if r.status in REFUSALS]
    must_refuse = [r for c in results if all(e in REFUSALS for e in c.expect) for r in c.runs]
    guardrails = GuardrailStats(
        in_scope_runs=len(in_scope), in_scope_refused=len(refused),
        false_positives_by_layer=dict(Counter(r.refused_by or "model" for r in refused)),
        must_refuse_runs=len(must_refuse),
        must_refuse_answered=sum(r.status == "answered" for r in must_refuse),
    )

    runs = [r for c in results for r in c.runs]
    latencies = sorted(r.latency_s for r in runs)
    latency = LatencyStats(p50_s=_percentile(latencies, 50), p95_s=_percentile(latencies, 95),
                           max_s=latencies[-1] if latencies else 0.0)
    served_by = dict(Counter(r.served_by or "none (refused before the agent model)" for r in runs))
    return EvalReport(meta=meta, repeat=repeat, cases=results, categories=categories, guardrails=guardrails,
                      latency=latency, served_by=served_by)
