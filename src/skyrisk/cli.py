"""Command-line entry point: `skyrisk ingest | score | show`."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import httpx

from skyrisk import db, pipeline
from skyrisk.config import load_hubs, load_scoring_config
from skyrisk.models import NRI_HAZARDS

HTTP_TIMEOUT_S = 60.0


def _parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="skyrisk", description="Weather-risk scoring for logistics hubs")
    p.add_argument("--db", type=Path, default=Path("data/skyrisk.db"), help="SQLite database path")
    p.add_argument("--config-dir", type=Path, default=Path("config"), help="directory with hubs.yaml and scoring.yaml")
    sub = p.add_subparsers(dest="command", required=True)

    ing = sub.add_parser("ingest", help="fetch Open-Meteo and FEMA NRI data into the cache")
    ing.add_argument("--refresh", action="store_true", help="re-fetch even if cached")
    ing.add_argument("--hub", action="append", dest="hubs", metavar="ID", help="limit to hub id (repeatable)")

    sub.add_parser("score", help="compute, store and print a ranked score run")

    show = sub.add_parser("show", help="explain one hub's scores from the latest run")
    show.add_argument("hub_id")
    return p


def _print_ranking(result) -> None:
    hazards = [h.hazard for h in result.hubs[0].hazards]
    header = f"{'#':>2}  {'hub':<12} {'overall':>7}  " + "  ".join(f"{h:>9}" for h in hazards)
    print(header)
    print("-" * len(header))
    for hub in result.hubs:
        subs = "  ".join(f"{h.sub_score:>9.2f}" for h in hub.hazards)
        print(f"{hub.rank:>2}  {hub.hub_id:<12} {hub.overall:>7.2f}  {subs}")


def _show(conn, hub_id: str) -> int:
    run_id = db.latest_run_id(conn)
    if run_id is None:
        print("no score runs yet; run `skyrisk score` first", file=sys.stderr)
        return 1
    hub = conn.execute(
        "SELECT overall, rank FROM hub_scores WHERE run_id = ? AND hub_id = ?", (run_id, hub_id)
    ).fetchone()
    if hub is None:
        print(f"hub {hub_id!r} not in run {run_id}", file=sys.stderr)
        return 1
    run = conn.execute("SELECT * FROM score_runs WHERE run_id = ?", (run_id,)).fetchone()
    total = conn.execute("SELECT COUNT(*) FROM hub_scores WHERE run_id = ?", (run_id,)).fetchone()[0]
    print(f"{hub_id}: overall {hub['overall']:.2f}, rank {hub['rank']} of {total}")
    print(f"run {run_id} ({run['created_at']}), config v{run['config_version']} {run['config_hash'][:12]}")
    print("Scores are relative to the other hubs (0 = lowest, 100 = highest exposure), not probabilities.\n")

    hazards = conn.execute(
        "SELECT hazard, sub_score FROM hazard_scores WHERE run_id = ? AND hub_id = ?", (run_id, hub_id)
    ).fetchall()
    for hz in hazards:
        print(f"{hz['hazard']}: {hz['sub_score']:.2f}")
        rows = conn.execute(
            """SELECT metric, raw, normalized, contribution, note FROM metric_values
               WHERE run_id = ? AND hub_id = ? AND hazard = ?""",
            (run_id, hub_id, hz["hazard"]),
        ).fetchall()
        for m in rows:
            note = f"  ({m['note']})" if m["note"] else ""
            print(f"    {m['metric']:<28} raw {m['raw']:>9.3f}  norm {m['normalized']:>6.2f}"
                  f"  +{m['contribution']:.2f} pts{note}")

    unscored = conn.execute(
        "SELECT metric, raw FROM hub_metrics WHERE run_id = ? AND hub_id = ? AND scored = 0",
        (run_id, hub_id),
    ).fetchall()
    if unscored:
        print("\nComputed but not scored:")
        for m in unscored:
            print(f"    {m['metric']:<28} raw {m['raw']:>9.3f}")

    county = db.load_nri(conn, hub_id)
    if county:
        print(f"\nFEMA NRI {county.nri_version}: {county.county_name} County, {county.state} "
              f"({county.county_fips}), {county.area_sqmi:,.0f} sq mi")
        afreq = ", ".join(
            f"{h}={county.afreq[h.lower()]:.3f}" if county.afreq[h.lower()] is not None else f"{h}=n/a"
            for h in NRI_HAZARDS
        )
        print(f"  raw *_AFREQ (events/yr, whole county): {afreq}")
        risks = ", ".join(
            f"{h}={county.risks[h.lower()]:.1f}" if county.risks[h.lower()] is not None else f"{h}=n/a"
            for h in NRI_HAZARDS
        )
        print(f"  composite *_RISKS (reference only, not scored): {risks}")
    return 0


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    registry = load_hubs(args.config_dir / "hubs.yaml")
    config = load_scoring_config(args.config_dir / "scoring.yaml")
    conn = db.connect(args.db)
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
            return _show(conn, args.hub_id)
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
