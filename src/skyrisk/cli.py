"""Command-line entry point: `skyrisk ingest | score | show | chat | serve | eval | eval-classifier`."""

from __future__ import annotations

import argparse
import fnmatch
import os
import sys
from datetime import datetime
from pathlib import Path

import httpx
from dotenv import load_dotenv

from skyrisk import db, pipeline
from skyrisk.agent.core import Conversation
from skyrisk.agent.factory import CLASSIFIER_KEYS, AgentSetupError, build_agent, build_classifier, build_classifiers
from skyrisk.agent.tools import ExplainScoreInput, ToolContext, ToolError, explain_score
from skyrisk.config import CLASSIFIER_NAMES, load_agent_config, load_hubs, load_scoring_config
from skyrisk.evals import classifier_bench
from skyrisk.evals.cases import load_cases
from skyrisk.evals.report import write_reports
from skyrisk.evals.runner import EvalMeta, run_evals

HTTP_TIMEOUT_S = 60.0


def _parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="skyrisk", description="Weather-risk scoring for logistics hubs")
    p.add_argument("--db", type=Path, default=Path("data/skyrisk.db"), help="SQLite database path")
    p.add_argument("--config-dir", type=Path, default=Path("config"), help="directory with hubs.yaml, scoring.yaml and agent.yaml")
    sub = p.add_subparsers(dest="command", required=True)

    ing = sub.add_parser("ingest", help="fetch Open-Meteo and FEMA NRI data into the cache")
    ing.add_argument("--refresh", action="store_true", help="re-fetch even if cached")
    ing.add_argument("--hub", action="append", dest="hubs", metavar="ID", help="limit to hub id (repeatable)")

    sub.add_parser("score", help="compute, store and print a ranked score run")

    show = sub.add_parser("show", help="explain one hub's scores from the latest run")
    show.add_argument("hub_id")

    chat = sub.add_parser("chat", help="ask the SkyRisk agent questions (interactive)")
    chat.add_argument("--question", "-q", help="answer one question and exit")

    serve = sub.add_parser("serve", help="run the chat web page and JSON API")
    serve.add_argument("--host", default=os.environ.get("HOST", "127.0.0.1"), help="bind address (default: $HOST or 127.0.0.1)")
    serve.add_argument("--port", type=int, default=int(os.environ.get("PORT", "8000")), help="port (default: $PORT or 8000)")

    ev = sub.add_parser("eval", help="run the eval cases against the real agent (uses API credits); "
                                     "exits 1 if any case fails")
    ev.add_argument("--cases", type=Path, default=Path("evals/cases.yaml"), help="eval cases file")
    ev.add_argument("--repeat", type=int, default=1, metavar="N", help="runs per case; a case passes only if all pass")
    ev.add_argument("--case", action="append", dest="case_globs", metavar="GLOB", help="only case ids matching (repeatable)")
    ev.add_argument("--category", action="append", dest="categories", metavar="NAME", help="only this category (repeatable)")
    ev.add_argument("--out", type=Path, default=Path("evals/results"), help="report directory")

    ec = sub.add_parser("eval-classifier", help="benchmark the guardrail classifiers directly on the guardrail "
                                                "eval cases (uses API credits unless --dry-run)")
    ec.add_argument("--classifier", action="append", dest="classifiers", choices=CLASSIFIER_NAMES,
                    help="classifier to benchmark (repeatable; default: all)")
    ec.add_argument("--cases", type=Path, default=Path("evals/cases.yaml"), help="eval cases file")
    ec.add_argument("--category", action="append", dest="categories", metavar="NAME",
                    help=f"only this category (repeatable; default: {', '.join(classifier_bench.DEFAULT_CATEGORIES)})")
    ec.add_argument("--repeat", type=int, default=1, metavar="N", help="calls per case per classifier")
    ec.add_argument("--out", type=Path, default=Path("evals/results"), help="report directory")
    ec.add_argument("--dry-run", action="store_true", help="print the call and cost estimate and exit")
    return p


def _print_ranking(result) -> None:
    hazards = [h.hazard for h in result.hubs[0].hazards]
    header = f"{'#':>2}  {'hub':<12} {'overall':>7}  " + "  ".join(f"{h:>9}" for h in hazards)
    print(header)
    print("-" * len(header))
    for hub in result.hubs:
        subs = "  ".join(f"{h.sub_score:>9.2f}" for h in hub.hazards)
        print(f"{hub.rank:>2}  {hub.hub_id:<12} {hub.overall:>7.2f}  {subs}")


def _show(ctx: ToolContext, hub_id: str) -> int:
    try:
        r = explain_score(ctx, ExplainScoreInput(hub_id=hub_id))
    except ToolError as e:
        print(str(e), file=sys.stderr)
        return 1
    run = ctx.conn.execute("SELECT * FROM score_runs WHERE run_id = ?", (r.run_id,)).fetchone()
    print(f"{hub_id}: overall {r.overall:.2f}, rank {r.overall_rank} of {r.hub_count}")
    print(f"run {r.run_id} ({run['created_at']}), config v{r.config_version} {run['config_hash'][:12]}")
    print("Scores are relative to the other hubs (0 = lowest, 100 = highest exposure), not probabilities.\n")

    for hz in r.hazards:
        print(f"{hz.hazard}: {hz.sub_score:.2f}")
        for m in hz.metrics:
            note = f"  ({m.note})" if m.note else ""
            print(f"    {m.metric:<28} raw {m.raw:>9.3f}  norm {m.normalized:>6.2f}  +{m.points:.2f} pts{note}")

    if r.unscored_metrics:
        print("\nComputed but not scored:")
        for metric, raw in r.unscored_metrics.items():
            print(f"    {metric:<28} raw {raw:>9.3f}")

    if r.nri:
        n = r.nri
        print(f"\nFEMA NRI {n.nri_version}: {n.county} County, {n.state} ({n.county_fips}), {n.area_sqmi:,.0f} sq mi")
        fmt = lambda values, spec: ", ".join(  # noqa: E731
            f"{h.upper()}={v:{spec}}" if v is not None else f"{h.upper()}=n/a" for h, v in values.items()
        )
        print(f"  raw *_AFREQ (events/yr, whole county): {fmt(n.afreq, '.3f')}")
        print(f"  composite *_RISKS (reference only, not scored): {fmt(n.risks_reference_only, '.1f')}")
    return 0


def _print_reply(reply) -> None:
    print(f"\n{reply.text}")
    if reply.limitations:
        print("\nLimitations:")
        for item in reply.limitations:
            print(f"  - {item}")
    footer = [f"served by {reply.served_by}" if reply.served_by else "no model call"]
    if reply.tools_used:
        footer.append("tools: " + ", ".join(dict.fromkeys(reply.tools_used)))
    if reply.guardrail:
        footer.append(f"guardrail: {reply.guardrail}")
    if reply.warnings:
        footer.append("warnings: " + "; ".join(reply.warnings))
    print(f"\033[2m[{' | '.join(footer)}]\033[0m\n")


def _log(msg: str) -> None:
    style = "\033[1;31m" if msg.startswith("CONFIGURATION ERROR") else "\033[2m"  # bold red vs dim
    print(f"{style}{msg}\033[0m", file=sys.stderr)


def _build_agent(conn, config_dir: Path):
    load_dotenv()
    try:
        return build_agent(conn, config_dir, os.environ, log=_log)
    except AgentSetupError as e:
        print(f"{e} Set it in .env (see .env.example).", file=sys.stderr)
        return None


def _chat(conn, config_dir: Path, question: str | None) -> int:
    built = _build_agent(conn, config_dir)
    if built is None:
        return 1
    agent, config = built
    conversation = Conversation(max_turns=config.max_history_turns)
    if question is not None:
        reply = agent.ask(conversation, question)
        _print_reply(reply)
        return 0 if reply.status != "error" else 1

    print("SkyRisk chat. Ask about the hubs' weather exposure. /reset clears memory, /quit exits.")
    while True:
        try:
            line = input("> ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            return 0
        if line in ("/quit", "/exit"):
            return 0
        if line == "/reset":
            conversation.reset()
            print("Memory cleared.")
            continue
        if line:
            _print_reply(agent.ask(conversation, line))


def _serve(ctx: ToolContext, config_dir: Path, host: str, port: int) -> int:
    import uvicorn

    from skyrisk.api.app import create_app
    from skyrisk.api.ratelimit import RateLimiter
    from skyrisk.api.sessions import SessionStore

    built = _build_agent(ctx.conn, config_dir)
    if built is None:
        return 1
    agent, config = built
    app = create_app(agent, SessionStore(max_turns=config.max_history_turns), ctx, log=_log,
                     limiter=RateLimiter.from_config(config.rate_limit))
    uvicorn.run(app, host=host, port=port)
    return 0


def _eval(ctx: ToolContext, config_dir: Path, args: argparse.Namespace) -> int:
    """Exit codes: 0 = every case passed, 1 = at least one case failed, 2 = setup error."""
    cases = load_cases(args.cases)
    if args.case_globs:
        cases = [c for c in cases if any(fnmatch.fnmatch(c.id, g) for g in args.case_globs)]
    if args.categories:
        cases = [c for c in cases if c.category in args.categories]
    if not cases:
        print("No eval cases match the filters.", file=sys.stderr)
        return 2
    if args.repeat < 1:
        print("--repeat must be at least 1.", file=sys.stderr)
        return 2
    run_id = db.latest_run_id(ctx.conn)
    if run_id is None:
        print("No score run exists yet; run `skyrisk ingest` and `skyrisk score` first.", file=sys.stderr)
        return 2
    built = _build_agent(ctx.conn, config_dir)
    if built is None:
        return 2
    agent, config = built

    started = datetime.now().astimezone()
    meta = EvalMeta(
        started_at=started.isoformat(timespec="seconds"),
        primary_model=f"{config.primary.provider}:{config.primary.model}",
        fallback_model=f"{config.fallback.provider}:{config.fallback.model}" if config.fallback else None,
        classifier_model=_classifier_description(config),
        score_run_id=run_id,
        scoring_config_version=ctx.scoring.version,
    )
    print(f"Running {len(cases)} case(s) x {args.repeat} against {meta.primary_model}...")
    report = run_evals(agent, cases, ctx, meta, repeat=args.repeat)
    path = write_reports(report, args.out, started.strftime("%Y%m%d-%H%M%S"))
    g = report.guardrails
    print(f"\n{report.cases_passed}/{len(report.cases)} cases passed | "
          f"guardrail false positives {g.in_scope_refused}/{g.in_scope_runs} | "
          f"misses {g.must_refuse_answered}/{g.must_refuse_runs} | p50 {report.latency.p50_s:.1f}s")
    print(f"Report: {path} (also {args.out / 'latest.md'})")
    return 0 if report.passed else 1


def _classifier_description(config) -> str | None:
    """The classifier chain as built for this run, e.g. `anthropic:claude-haiku-4-5 → fallback jev:...`."""
    if config.classifier is None:
        return None
    chain = build_classifier(config.classifier, os.environ, log=lambda m: None)
    return chain.name if chain else None


def _eval_classifier(config_dir: Path, args: argparse.Namespace) -> int:
    """Exit codes: 0 = report written (or dry run), 2 = setup error."""
    load_dotenv()
    config = load_agent_config(config_dir / "agent.yaml").with_env_overrides(os.environ)
    if config.classifier is None:
        print("No classifier is configured in agent.yaml.", file=sys.stderr)
        return 2
    keys = args.classifiers or [k for k in CLASSIFIER_NAMES if k != "jev" or config.classifier.jev is not None]
    categories = args.categories or list(classifier_bench.DEFAULT_CATEGORIES)
    cases = [c for c in load_cases(args.cases) if c.category in categories]
    skipped = [c.id for c in cases if not classifier_bench.has_expected_label(c)]
    if skipped:
        print(f"Skipping case(s) with no classifier label to check: {', '.join(skipped)}")
        cases = [c for c in cases if c.id not in skipped]
    if not cases:
        print("No eval cases match the filters.", file=sys.stderr)
        return 2
    if args.repeat < 1:
        print("--repeat must be at least 1.", file=sys.stderr)
        return 2
    escalation = bool(os.environ.get(CLASSIFIER_KEYS["haiku"]))
    print(f"{len(cases)} case(s) x {args.repeat} per classifier ({', '.join(keys)}). Estimate:")
    for line in classifier_bench.estimate(cases, keys, args.repeat, escalation_possible=escalation):
        print(f"  {line}")
    if args.dry_run:
        return 0

    available = build_classifiers(config.classifier, os.environ, log=_log)
    missing = [k for k in keys if k not in available]
    if missing:
        print(f"Missing API key for: {', '.join(f'{k} ({CLASSIFIER_KEYS[k]})' for k in missing)}. "
              "Set it in .env (see .env.example).", file=sys.stderr)
        return 2
    started = datetime.now().astimezone()
    report = classifier_bench.run_bench({k: available[k] for k in keys}, cases,
                                        repeat=args.repeat, max_input_chars=config.max_input_chars,
                                        started_at=started.isoformat(timespec="seconds"), categories=categories)
    path = classifier_bench.write_reports(report, args.out, started.strftime("%Y%m%d-%H%M%S"))
    for r in report.results:
        m = r.metrics
        cost = f"${m.cost_per_1k_usd:.3f}/1k" if m.cost_per_1k_usd is not None else "cost n/a"
        print(f"{r.key}: FP {m.false_positives_production} | misses {m.misses_production} (production) | "
              f"p50 {m.latency_p50_s:.2f}s | escalated {m.escalations} | errors {m.errors} | {cost}")
    print(f"Report: {path} (also {args.out / 'classifier-latest.md'})")
    return 0


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if args.command == "eval-classifier":  # needs no database
        return _eval_classifier(args.config_dir, args)
    registry = load_hubs(args.config_dir / "hubs.yaml")
    config = load_scoring_config(args.config_dir / "scoring.yaml")
    # The API serves requests from a threadpool; tools only read the shared connection.
    conn = db.connect(args.db, check_same_thread=args.command != "serve")
    try:
        if args.command == "ingest":
            with httpx.Client(timeout=HTTP_TIMEOUT_S) as client:
                pipeline.ingest(conn, registry, config, client, refresh=args.refresh, hub_ids=args.hubs)
        elif args.command == "score":
            result = pipeline.compute_scores(conn, registry, config)
            run_id = db.persist_run(conn, result)
            print(f"run {run_id}: config v{result.config_version} {result.config_hash[:12]}, "
                  f"data {result.data_hash[:12]}\n")
            _print_ranking(result)
        elif args.command == "show":
            return _show(ToolContext(conn, registry, config), args.hub_id)
        elif args.command == "chat":
            return _chat(conn, args.config_dir, args.question)
        elif args.command == "serve":
            return _serve(ToolContext(conn, registry, config), args.config_dir, args.host, args.port)
        elif args.command == "eval":
            return _eval(ToolContext(conn, registry, config), args.config_dir, args)
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
