"""Render an EvalReport as Markdown and write <stamp>.md/.json plus latest.md/.json."""

from __future__ import annotations

from pathlib import Path

from skyrisk.evals.runner import EvalReport


def _pct(num: int, den: int) -> str:
    return f"{100 * num / den:.0f}%" if den else "n/a"


def _cell(text: str) -> str:
    return text.replace("|", "\\|").replace("\n", " ")


def render_markdown(report: EvalReport) -> str:
    m, g = report.meta, report.guardrails
    lines = [
        "# SkyRisk eval results",
        "",
        f"**{report.cases_passed}/{len(report.cases)} cases passed** "
        f"({report.repeat} run{'s' if report.repeat > 1 else ''} per case; a case passes only if every run passes)",
        "",
        f"- Run at: {m.started_at}",
        f"- Primary model: `{m.primary_model}`, fallback: `{m.fallback_model or 'none'}`, "
        f"classifier: `{m.classifier_model or 'none'}`",
        f"- Score run: {m.score_run_id} (scoring config v{m.scoring_config_version})",
        *([f"- **Simulated outage: `{m.simulated_outage}`.** Every {m.simulated_outage}-backed model (answering "
           "provider and classifier) fails on every call, so the agent takes its real outage path: the classifier "
           "is skipped and the turn falls back to the next provider."] if m.simulated_outage else []),
        "",
        "## By category",
        "",
        "| Category | Cases passed | Runs passed |",
        "|---|---|---|",
    ]
    lines += [f"| {c.category} | {c.passed}/{c.cases} | {c.runs_passed}/{c.runs} ({_pct(c.runs_passed, c.runs)}) |"
              for c in report.categories]

    layers = ", ".join(f"{k}: {v}" for k, v in sorted(g.false_positives_by_layer.items())) or "none"
    lines += [
        "",
        "## Guardrails",
        "",
        "| Metric | Value |",
        "|---|---|",
        f"| False-positive rate (in-scope runs refused) | {g.in_scope_refused}/{g.in_scope_runs} "
        f"({_pct(g.in_scope_refused, g.in_scope_runs)}) |",
        f"| False positives by layer | {layers} |",
        f"| Miss rate (off-topic / injection runs answered) | {g.must_refuse_answered}/{g.must_refuse_runs} "
        f"({_pct(g.must_refuse_answered, g.must_refuse_runs)}) |",
    ]

    if report.reliability is not None:
        rel = report.reliability
        lines += [f"| Error replies (unverified answer, unusable response, no provider) | {rel.error_runs} |",
                  f"| Grounding failures (cited score differs from the DB) | {rel.grounding_failures} |"]

    failed = [c for c in report.cases if not c.passed]
    lines += ["", "## Failures", ""]
    if not failed:
        lines.append("None.")
    for c in failed:
        expected = " or ".join(f"`{e}`" for e in c.expect)
        lines += [f"### `{c.id}` ({c.category}), {c.passes}/{len(c.runs)} runs passed"
                  + (" (flaky)" if c.flaky else ""), "", f"> {c.question}", "", f"Expected {expected}.", ""]
        for i, r in enumerate(c.runs, start=1):
            if r.passed:
                lines.append(f"- run {i}: passed")
            else:
                lines.append(f"- run {i}: {'; '.join(r.reasons)}")
                if r.answer:
                    lines.append(f"  > {_cell(r.answer)[:400]}")
        lines.append("")

    flaky = [c.id for c in report.cases if c.flaky]
    lines += ["", "## Flaky cases", "", ", ".join(f"`{i}`" for i in flaky) if flaky else "None."]

    lat = report.latency
    lines += [
        "",
        "## Latency",
        "",
        f"All runs: p50 {lat.p50_s:.1f}s, p95 {lat.p95_s:.1f}s, max {lat.max_s:.1f}s.",
        "",
        "| Case | Status (per run) | Mean latency | Tools |",
        "|---|---|---|---|",
    ]
    for c in report.cases:
        mean = sum(r.latency_s for r in c.runs) / len(c.runs)
        statuses = ", ".join(r.status for r in c.runs)
        tools = ", ".join(dict.fromkeys(t for r in c.runs for t in r.tools)) or "none"
        lines.append(f"| `{c.id}` | {_cell(statuses)} | {mean:.1f}s | {tools} |")

    lines += ["", "## Models used", "", "| Served by | Runs |", "|---|---|"]
    lines += [f"| {k} | {v} |" for k, v in sorted(report.served_by.items(), key=lambda kv: -kv[1])]

    if report.cost is not None:
        c = report.cost
        calls = ", ".join(f"{k}: {v}" for k, v in sorted(c.calls_by_model.items())) or "none"
        lines += ["", "## Cost", "",
                  f"Estimated **${c.total_usd:.3f}** for all runs (${c.per_run_usd:.4f} per run), from recorded "
                  f"token usage and `src/skyrisk/evals/pricing.py`. Model calls: {calls}."]
        if c.unpriced_models:
            lines.append(f"Not included (no price): {', '.join(c.unpriced_models)}.")
    return "\n".join(lines).rstrip() + "\n"


def write_reports(report: EvalReport, out_dir: Path, stamp: str, prefix: str = "") -> Path:
    """Write <prefix><stamp>.* and overwrite <prefix>latest.*; returns the timestamped Markdown path."""
    out_dir.mkdir(parents=True, exist_ok=True)
    markdown = render_markdown(report)
    data = report.model_dump_json(indent=2) + "\n"
    for name in (f"{prefix}{stamp}", f"{prefix}latest"):
        (out_dir / f"{name}.md").write_text(markdown, encoding="utf-8")
        (out_dir / f"{name}.json").write_text(data, encoding="utf-8")
    return out_dir / f"{prefix}{stamp}.md"
