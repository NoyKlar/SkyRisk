"""Command-line entry point: `skyrisk ingest | score | show | chat | serve | eval | eval-classifier | alerts`."""

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
from skyrisk.agent.factory import (
    CLASSIFIER_KEYS,
    OUTAGE_VENDORS,
    AgentSetupError,
    build_agent,
    build_classifier,
    build_classifiers,
)
from skyrisk.agent.tools import ExplainScoreInput, ToolContext, ToolError, explain_score
from skyrisk.config import (
    CLASSIFIER_NAMES,
    HubRegistry,
    ScoringConfig,
    load_agent_config,
    load_hubs,
    load_near_term_config,
    load_scoring_config,
)
from skyrisk.evals import classifier_bench
from skyrisk.evals.cases import load_cases
from skyrisk.evals.report import write_reports
from skyrisk.evals.runner import EvalMeta, run_evals
from skyrisk.ingest import http
from skyrisk.history.ytd import YtdService
from skyrisk.ingest.open_meteo import fetch_daily, fetch_forecast
from skyrisk.nearterm import alerts as nt_alerts
from skyrisk.nearterm.alerts import Notify
from skyrisk.nearterm.service import NearTermService, NearTermUnavailable

HTTP_TIMEOUT_S = 60.0
FORECAST_TIMEOUT_S = 10.0  # forecasts are fetched while a user waits (chat) or a cron job waits (alerts)
NEAR_TERM_COMMANDS = ("chat", "serve", "eval", "alerts")
YTD_COMMANDS = ("chat", "serve", "eval")


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
    chat.add_argument("--simulate-outage", choices=OUTAGE_VENDORS, metavar="VENDOR",
                      help="make every model from VENDOR (anthropic|openai) fail, to test the outage path")

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
    ev.add_argument("--report-name", metavar="NAME",
                    help="write <NAME>-<stamp>.* and <NAME>-latest.* instead of latest.* (e.g. for a subset run)")
    ev.add_argument("--simulate-outage", choices=OUTAGE_VENDORS, metavar="VENDOR",
                    help="make every model from VENDOR (anthropic|openai) fail, e.g. anthropic: OpenAI answers and "
                         "the classifier is skipped; reports go to <VENDOR>-outage-*")

    al = sub.add_parser("alerts", help="near-term risk alerts (7-day forecast)")
    al_sub = al.add_subparsers(dest="alerts_command", required=True)
    al_check = al_sub.add_parser("check", help="recompute near-term scores, compare with the last snapshot, "
                                               "store alerts and post the webhook (ALERT_WEBHOOK_URL)")
    al_check.add_argument("--demo", metavar="HUB", help="simulate a storm for this hub: a demo alert, no baseline change")
    al_list = al_sub.add_parser("list", help="print recent alerts")
    al_list.add_argument("--limit", type=int, default=20)

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


def _near_term_service(config_dir: Path, registry: HubRegistry, client: httpx.Client) -> NearTermService:
    cfg = load_near_term_config(config_dir / "near_term.yaml")
    return NearTermService(registry, cfg, lambda hub: fetch_forecast(hub, client, days=cfg.forecast_days))


def _ytd_service(registry: HubRegistry, config: ScoringConfig, client: httpx.Client) -> YtdService:
    """Current-year year-to-date data for weather_stat; one retry, since a user is waiting."""
    return YtdService(registry, config.window, lambda hub, start, end: fetch_daily(hub, start, end, client, retries=1))


def _webhook(client: httpx.Client) -> Notify | None:
    """Posts a Slack-compatible `{"text": ...}` body to ALERT_WEBHOOK_URL; None when it is unset."""
    url = os.environ.get("ALERT_WEBHOOK_URL")
    if not url:
        return None
    return lambda text: http.post(client, url, {"text": text})


def _build_agent(conn, config_dir: Path, simulate_outage: str | None = None, near_term=None, ytd=None):
    load_dotenv()
    if simulate_outage:
        _log(f"SIMULATED OUTAGE: every {simulate_outage} model fails on every call")
    try:
        return build_agent(conn, config_dir, os.environ, log=_log, simulate_outage=simulate_outage,
                           near_term=near_term, ytd=ytd)
    except AgentSetupError as e:
        print(f"{e} Set it in .env (see .env.example).", file=sys.stderr)
        return None


def _chat(conn, config_dir: Path, question: str | None, simulate_outage: str | None = None, near_term=None,
          ytd=None) -> int:
    built = _build_agent(conn, config_dir, simulate_outage, near_term, ytd)
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


def _serve(ctx: ToolContext, config_dir: Path, host: str, port: int, client: httpx.Client) -> int:
    import uvicorn

    from skyrisk.api.app import create_app
    from skyrisk.api.ratelimit import RateLimiter
    from skyrisk.api.sessions import SessionStore

    built = _build_agent(ctx.conn, config_dir, near_term=ctx.near_term, ytd=ctx.ytd)
    if built is None:
        return 1
    agent, config = built
    token = os.environ.get("ALERT_TOKEN") or None
    if token is None:
        _log("warning: ALERT_TOKEN is not set; POST /api/alerts/check is disabled")
    app = create_app(agent, SessionStore(max_turns=config.max_history_turns), ctx, log=_log,
                     limiter=RateLimiter.from_config(config.rate_limit), alert_token=token,
                     notify=_webhook(client))
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
    built = _build_agent(ctx.conn, config_dir, args.simulate_outage, ctx.near_term, ctx.ytd)
    if built is None:
        return 2
    agent, config = built

    started = datetime.now().astimezone()
    meta = EvalMeta(
        started_at=started.isoformat(timespec="seconds"),
        primary_model=f"{config.primary.provider}:{config.primary.model}",
        fallback_model=f"{config.fallback.provider}:{config.fallback.model}" if config.fallback else None,
        classifier_model=_classifier_description(config, args.simulate_outage),
        score_run_id=run_id,
        scoring_config_version=ctx.scoring.version,
        simulated_outage=args.simulate_outage,
    )
    target = (f"a simulated {args.simulate_outage} outage (answering: {meta.fallback_model})"
              if args.simulate_outage else meta.primary_model)
    print(f"Running {len(cases)} case(s) x {args.repeat} against {target}...")
    report = run_evals(agent, cases, ctx, meta, repeat=args.repeat)
    prefix = (f"{args.report_name}-" if args.report_name
              else f"{args.simulate_outage}-outage-" if args.simulate_outage else "")
    path = write_reports(report, args.out, started.strftime("%Y%m%d-%H%M%S"), prefix)
    g = report.guardrails
    print(f"\n{report.cases_passed}/{len(report.cases)} cases passed | "
          f"guardrail false positives {g.in_scope_refused}/{g.in_scope_runs} | "
          f"misses {g.must_refuse_answered}/{g.must_refuse_runs} | p50 {report.latency.p50_s:.1f}s")
    if report.cost is not None:
        print(f"Estimated cost: ${report.cost.total_usd:.3f} ({', '.join(f'{k}: {v}' for k, v in report.cost.calls_by_model.items())})")
    print(f"Report: {path} (also {args.out / f'{prefix}latest.md'})")
    return 0 if report.passed else 1


def _alerts(ctx: ToolContext, args: argparse.Namespace, client: httpx.Client) -> int:
    if args.alerts_command == "list":
        for a in nt_alerts.recent(ctx.conn, limit=args.limit):
            tag = " [DEMO]" if a.demo else ""
            print(f"{a.created_at}  {a.hub_id:<12} {a.prev_score:>6.2f} -> {a.new_score:>6.2f} "
                  f"({a.prev_level} -> {a.new_level}){tag}  {a.reason}; {a.detail}")
        return 0
    load_dotenv()
    try:
        result = nt_alerts.run_check(ctx.conn, ctx.near_term, notify=_webhook(client), demo_hub=args.demo, log=_log)
    except KeyError as e:
        print(str(e), file=sys.stderr)
        return 1
    except NearTermUnavailable as e:
        print(str(e), file=sys.stderr)
        return 1
    print(f"checked {len(result.hubs_checked)} hub(s) at {result.checked_at}; "
          f"baseline only: {', '.join(result.baseline_only) or 'none'}; webhook: {result.webhook_status}")
    for a in result.alerts:
        print(f"  ALERT{' [DEMO]' if a.demo else ''} {a.hub_id}: {a.prev_score:.2f} -> {a.new_score:.2f} "
              f"({a.prev_level} -> {a.new_level}); {a.reason}; {a.detail}")
    return 0 if result.hubs_checked else 1


def _classifier_description(config, simulate_outage: str | None = None) -> str | None:
    """The classifier chain as built for this run, e.g. `anthropic:claude-haiku-4-5`."""
    if config.classifier is None:
        return None
    chain = build_classifier(config.classifier, os.environ, log=lambda m: None, down=simulate_outage)
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
    forecast_client = httpx.Client(timeout=FORECAST_TIMEOUT_S)
    near_term = (_near_term_service(args.config_dir, registry, forecast_client)
                 if args.command in NEAR_TERM_COMMANDS else None)
    ytd = _ytd_service(registry, config, forecast_client) if args.command in YTD_COMMANDS else None
    ctx = ToolContext(conn, registry, config, near_term, ytd)
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
            return _show(ctx, args.hub_id)
        elif args.command == "chat":
            return _chat(conn, args.config_dir, args.question, args.simulate_outage, near_term, ytd)
        elif args.command == "serve":
            return _serve(ctx, args.config_dir, args.host, args.port, forecast_client)
        elif args.command == "eval":
            return _eval(ctx, args.config_dir, args)
        elif args.command == "alerts":
            return _alerts(ctx, args, forecast_client)
    finally:
        forecast_client.close()
        conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
