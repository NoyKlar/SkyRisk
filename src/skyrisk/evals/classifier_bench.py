"""Classifier-only benchmark: call each guardrail classifier directly on the guardrail eval cases.

No agent model is involved, so this measures the classifier alone: accuracy (false positives,
misses), escalation rate, latency and cost. Results are reported both for every case and for the
cases that reach the classifier in production (the deterministic input check lets them through).
"""

from __future__ import annotations

import math
import time
from collections.abc import Callable, Mapping
from pathlib import Path

from pydantic import BaseModel, computed_field

from skyrisk.agent.guardrails import CallUsage, Classifier, Log, check_input
from skyrisk.evals.cases import EvalCase

DEFAULT_CATEGORIES = ("injection", "off_topic", "false_positive", "hebrew")
EXPECTED_LABEL = {"answered": "in_scope", "refused_off_topic": "off_topic", "refused_injection": "injection"}

# USD per million tokens (input, output).
# Haiku 4.5: Anthropic API price list (claude-api skill model table, cached 2026-06-24).
# Jev 1.13: docs.typesafe.ai pricing (checked 2026-09-27); output tokens are free.
PRICES: dict[str, tuple[float, float]] = {
    "claude-haiku-4-5": (1.00, 5.00),
    "jev-1.13.0": (0.042, 0.0),
}
# Rough per-call token counts, used only by the dry-run cost estimate. Jev: the recorded fixture call.
ESTIMATED_TOKENS: dict[str, tuple[int, int]] = {"haiku": (600, 60), "jev": (500, 40)}


class BenchRun(BaseModel):
    label: str | None  # None when the classifier raised
    decided_by: str | None
    reason: str | None = None  # the classifier's own reason; for Jev, the probabilities
    latency_s: float
    usage: list[CallUsage]
    error: str | None = None


class BenchCase(BaseModel):
    id: str
    category: str
    question: str
    expected: str
    reaches_classifier: bool  # False when the deterministic input check already blocks it
    runs: list[BenchRun]

    @computed_field
    @property
    def correct(self) -> int:
        return sum(r.label == self.expected for r in self.runs)

    @computed_field
    @property
    def flaky(self) -> bool:
        return len({r.label for r in self.runs}) > 1


class Ratio(BaseModel):
    count: int
    total: int

    @computed_field
    @property
    def rate(self) -> float:
        return self.count / self.total if self.total else 0.0

    def __str__(self) -> str:
        return f"{self.count}/{self.total} ({100 * self.rate:.0f}%)" if self.total else "n/a"


class Metrics(BaseModel):
    false_positives_production: Ratio  # in-scope runs labelled off_topic/injection, production-reaching cases
    false_positives_all: Ratio
    misses_production: Ratio  # must-refuse runs labelled in_scope, production-reaching cases
    misses_all: Ratio
    wrong_refusal_type: int  # refused, but as the other refusal type
    accuracy_by_category: dict[str, Ratio]
    flaky_cases: list[str]
    escalations: Ratio
    errors: int
    latency_p50_s: float
    latency_p95_s: float
    latency_max_s: float
    calls_by_model: dict[str, int]
    cost_per_1k_usd: float | None  # None if a model has no entry in PRICES
    unpriced_models: list[str]


class ClassifierResult(BaseModel):
    key: str
    name: str
    cases: list[BenchCase]
    metrics: Metrics


class BenchReport(BaseModel):
    started_at: str
    repeat: int
    categories: list[str]
    results: list[ClassifierResult]


def expected_label(case: EvalCase) -> str:
    labels = {EXPECTED_LABEL[e] for e in case.expected_statuses if e in EXPECTED_LABEL}
    if len(labels) != 1:
        raise ValueError(f"case {case.id} has no single expected classifier label: {case.expected_statuses}")
    return labels.pop()


def has_expected_label(case: EvalCase) -> bool:
    """False for cases such as `needs_clarification` ones, where no classifier label is right or wrong."""
    try:
        expected_label(case)
    except ValueError:
        return False
    return True


def estimate(cases: list[EvalCase], keys: list[str], repeat: int, *, escalation_possible: bool) -> list[str]:
    """Human-readable call and cost estimate for a run; makes no calls."""
    n = len(cases) * repeat
    lines, total_calls, total_cost = [], 0, 0.0
    for key in keys:
        in_tok, out_tok = ESTIMATED_TOKENS[key]
        model = "jev-1.13.0" if key == "jev" else "claude-haiku-4-5"
        cost = n * _price(model, in_tok, out_tok)
        calls = n
        line = f"{key}: {n} calls (~${cost:.4f})"
        if key == "jev" and escalation_possible:
            esc_cost = n * _price("claude-haiku-4-5", *ESTIMATED_TOKENS["haiku"])
            line += f" + at most {n} Haiku escalations (~${esc_cost:.4f} if every case escalates)"
            calls += n
            cost += esc_cost
        lines.append(line)
        total_calls += calls
        total_cost += cost
    lines.append(f"total: at most {total_calls} calls, at most ~${total_cost:.2f} "
                 f"(token assumptions per call: {ESTIMATED_TOKENS})")
    return lines


def _price(model: str, input_tokens: int, output_tokens: int) -> float:
    per_in, per_out = PRICES[model]
    return (input_tokens * per_in + output_tokens * per_out) / 1_000_000


def _percentile(sorted_values: list[float], pct: float) -> float:
    if not sorted_values:
        return 0.0
    return sorted_values[max(0, math.ceil(pct / 100 * len(sorted_values)) - 1)]


def _run_once(classifier: Classifier, question: str, now: Callable[[], float]) -> BenchRun:
    start = now()
    try:
        v = classifier.classify(question)
    except Exception as e:  # noqa: BLE001 - one failed call must not stop the benchmark
        return BenchRun(label=None, decided_by=None, latency_s=now() - start, usage=[],
                        error=f"{type(e).__name__}: {e}")
    return BenchRun(label=v.label, decided_by=v.decided_by, reason=v.reason, latency_s=now() - start, usage=v.usage)


def run_bench(classifiers: Mapping[str, Classifier], cases: list[EvalCase], *, repeat: int, max_input_chars: int,
              started_at: str, categories: list[str], now: Callable[[], float] = time.perf_counter,
              log: Log = print) -> BenchReport:
    if repeat < 1:
        raise ValueError("repeat must be at least 1")
    results = []
    for key, classifier in classifiers.items():
        log(f"--- {key}: {classifier.name}")
        bench_cases = []
        for case in cases:
            expected = expected_label(case)
            runs = [_run_once(classifier, case.question, now) for _ in range(repeat)]
            bc = BenchCase(id=case.id, category=case.category, question=case.question, expected=expected,
                           reaches_classifier=check_input(case.question, max_input_chars) is None, runs=runs)
            labels = ", ".join(r.label or "error" for r in runs)
            log(f"{'✓' if bc.correct == repeat else '✗'} {case.id:<32} {labels}")
            bench_cases.append(bc)
        results.append(ClassifierResult(key=key, name=classifier.name, cases=bench_cases,
                                        metrics=_metrics(bench_cases)))
    return BenchReport(started_at=started_at, repeat=repeat, categories=categories, results=results)


def _metrics(cases: list[BenchCase]) -> Metrics:
    def fp(subset: list[BenchCase]) -> Ratio:
        runs = [r for c in subset if c.expected == "in_scope" for r in c.runs]
        return Ratio(count=sum(r.label in ("off_topic", "injection") for r in runs), total=len(runs))

    def misses(subset: list[BenchCase]) -> Ratio:
        runs = [r for c in subset if c.expected != "in_scope" for r in c.runs]
        return Ratio(count=sum(r.label == "in_scope" for r in runs), total=len(runs))

    production = [c for c in cases if c.reaches_classifier]
    runs = [r for c in cases for r in c.runs]
    accuracy = {}
    for category in dict.fromkeys(c.category for c in cases):
        group = [c for c in cases if c.category == category]
        accuracy[category] = Ratio(count=sum(c.correct for c in group), total=sum(len(c.runs) for c in group))

    calls: dict[str, int] = {}
    tokens: dict[str, tuple[int, int]] = {}
    for r in runs:
        for u in r.usage:
            calls[u.model] = calls.get(u.model, 0) + 1
            i, o = tokens.get(u.model, (0, 0))
            tokens[u.model] = (i + u.input_tokens, o + u.output_tokens)
    unpriced = sorted(m for m in tokens if m not in PRICES)
    answered = [r for r in runs if r.error is None]
    cost = None
    if not unpriced and answered:
        cost = sum(_price(m, i, o) for m, (i, o) in tokens.items()) / len(answered) * 1000

    latencies = sorted(r.latency_s for r in runs)
    return Metrics(
        false_positives_production=fp(production), false_positives_all=fp(cases),
        misses_production=misses(production), misses_all=misses(cases),
        wrong_refusal_type=sum(r.label in ("off_topic", "injection") and r.label != c.expected
                               for c in cases if c.expected != "in_scope" for r in c.runs),
        accuracy_by_category=accuracy,
        flaky_cases=[c.id for c in cases if c.flaky],
        escalations=Ratio(count=sum((r.decided_by or "").startswith(("jev→", "jev(band")) for r in runs),
                          total=len(runs)),
        errors=sum(r.error is not None for r in runs),
        latency_p50_s=_percentile(latencies, 50), latency_p95_s=_percentile(latencies, 95),
        latency_max_s=latencies[-1] if latencies else 0.0,
        calls_by_model=calls, cost_per_1k_usd=cost, unpriced_models=unpriced,
    )


def render_markdown(report: BenchReport) -> str:
    rs = report.results
    header = "| Metric | " + " | ".join(f"{r.key}" for r in rs) + " |"
    sep = "|---|" + "---|" * len(rs)

    def row(label: str, cell: Callable[[ClassifierResult], str]) -> str:
        return f"| {label} | " + " | ".join(cell(r) for r in rs) + " |"

    lines = [
        "# SkyRisk classifier benchmark",
        "",
        f"- Run at: {report.started_at}",
        f"- Categories: {', '.join(report.categories)}; {report.repeat} run(s) per case; the classifier is called "
        "directly, with no agent model",
        *[f"- `{r.key}`: {r.name}" for r in rs],
        "",
        "\"Production\" counts only the cases the deterministic input check lets through to the classifier.",
        "",
        "## Side by side",
        "",
        header,
        sep,
        row("False positives (production)", lambda r: str(r.metrics.false_positives_production)),
        row("False positives (all cases)", lambda r: str(r.metrics.false_positives_all)),
        row("Misses (production)", lambda r: str(r.metrics.misses_production)),
        row("Misses (all cases)", lambda r: str(r.metrics.misses_all)),
        row("Wrong refusal type", lambda r: str(r.metrics.wrong_refusal_type)),
    ]
    for category in dict.fromkeys(k for r in rs for k in r.metrics.accuracy_by_category):
        lines.append(row(f"Accuracy: {category}",
                         lambda r, c=category: str(r.metrics.accuracy_by_category.get(c, "n/a"))))
    lines += [
        row("Escalated to Haiku", lambda r: str(r.metrics.escalations)),
        row("Errors", lambda r: str(r.metrics.errors)),
        row("Latency p50 / p95 / max", lambda r: f"{r.metrics.latency_p50_s:.2f}s / {r.metrics.latency_p95_s:.2f}s "
                                                 f"/ {r.metrics.latency_max_s:.2f}s"),
        row("Calls by model", lambda r: ", ".join(f"{m}: {n}" for m, n in r.metrics.calls_by_model.items()) or "none"),
        row("Est. cost per 1k questions", lambda r: f"${r.metrics.cost_per_1k_usd:.3f}"
            if r.metrics.cost_per_1k_usd is not None else f"n/a (no price for {', '.join(r.metrics.unpriced_models)})"),
        row("Flaky cases", lambda r: ", ".join(f"`{c}`" for c in r.metrics.flaky_cases) or "none"),
    ]

    lines += ["", "## Per case", "", "| Case | Expected | " + " | ".join(r.key for r in rs) + " |",
              "|---|---|" + "---|" * len(rs)]
    for i, case in enumerate(rs[0].cases if rs else []):
        cells = []
        for r in rs:
            c = r.cases[i]
            cells.append(", ".join((run.label or "error") + (f" ({run.decided_by})" if run.decided_by not in
                                                             (None, "haiku", "jev") else "") for run in c.runs))
        marker = "" if case.reaches_classifier else " (regex)"
        lines.append(f"| `{case.id}`{marker} | {case.expected} | " + " | ".join(cells) + " |")

    wrong = [(r.key, c.id, run.label, run.reason) for r in rs for c in r.cases for run in c.runs
             if run.label is not None and run.label != c.expected]
    if wrong:
        lines += ["", "## Wrong labels", ""] + [f"- `{k}` `{cid}` labelled {label}: {_cell(reason or '')}"
                                                for k, cid, label, reason in wrong]

    errors = [(r.key, c.id, run.error) for r in rs for c in r.cases for run in c.runs if run.error]
    if errors:
        lines += ["", "## Errors", ""] + [f"- `{k}` `{cid}`: {e}" for k, cid, e in errors]
    return "\n".join(lines).rstrip() + "\n"


def _cell(text: str) -> str:
    return text.replace("|", "\\|").replace("\n", " ")


def write_reports(report: BenchReport, out_dir: Path, stamp: str) -> Path:
    """Write classifier-<stamp>.md/.json and overwrite classifier-latest.*; returns the timestamped Markdown path."""
    out_dir.mkdir(parents=True, exist_ok=True)
    markdown = render_markdown(report)
    data = report.model_dump_json(indent=2) + "\n"
    for name in (f"classifier-{stamp}", "classifier-latest"):
        (out_dir / f"{name}.md").write_text(markdown, encoding="utf-8")
        (out_dir / f"{name}.json").write_text(data, encoding="utf-8")
    return out_dir / f"classifier-{stamp}.md"
