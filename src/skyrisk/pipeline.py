"""Orchestration: ingest data into SQLite, and compute score runs from it."""

from __future__ import annotations

import sqlite3
import time
from collections.abc import Callable

import httpx

from skyrisk import db
from skyrisk.config import HubRegistry, ScoringConfig
from skyrisk.ingest import fema_nri, open_meteo
from skyrisk.scoring import engine, metrics

Log = Callable[[str], None]

# Pause between Open-Meteo archive requests: a 10-year, 5-variable request counts as
# many weighted calls against the free per-minute limit.
WEATHER_REQUEST_PAUSE_S = 10.0


def ingest(
    conn: sqlite3.Connection,
    registry: HubRegistry,
    config: ScoringConfig,
    client: httpx.Client,
    *,
    refresh: bool = False,
    hub_ids: list[str] | None = None,
    log: Log = print,
    sleep: Callable[[float], None] = time.sleep,
) -> None:
    db.upsert_hubs(conn, registry.hubs)
    hubs = [registry.get(h) for h in hub_ids] if hub_ids else registry.hubs
    window = config.window
    fetched_weather = False
    for hub in hubs:
        have = db.weather_day_count(conn, hub.id, window.start, window.end)
        if refresh or have < window.num_days:
            if fetched_weather:
                sleep(WEATHER_REQUEST_PAUSE_S)
            fetched_weather = True
            days = open_meteo.fetch_daily(hub, window.start, window.end, client)
            db.upsert_weather(conn, hub.id, days)
            log(f"{hub.id}: weather fetched ({len(days)} days)")
        else:
            log(f"{hub.id}: weather cached ({have} days)")

        if refresh or db.load_nri(conn, hub.id) is None:
            county = fema_nri.fetch_county(hub, client)
            db.upsert_nri(conn, hub.id, county)
            log(f"{hub.id}: NRI fetched ({county.county_name}, {county.state})")
        else:
            log(f"{hub.id}: NRI cached")


def compute_scores(
    conn: sqlite3.Connection, registry: HubRegistry, config: ScoringConfig
) -> engine.ScoreResult:
    metrics_by_hub: dict[str, dict[str, float]] = {}
    notes: dict[str, dict[str, str]] = {}
    for hub in registry.hubs:
        rows = db.load_weather(conn, hub.id, config.window.start, config.window.end)
        county = db.load_nri(conn, hub.id)
        if not rows or county is None:
            raise RuntimeError(f"no cached data for hub {hub.id}; run `skyrisk ingest` first")
        nri, hub_notes = metrics.nri_metrics(county, config.nri_area_floor_sqmi)
        metrics_by_hub[hub.id] = metrics.weather_metrics(rows, config.thresholds) | nri
        if hub_notes:
            notes[hub.id] = hub_notes
    return engine.score(metrics_by_hub, config, notes)
