"""SQLite storage: schema, caching helpers and score-run persistence."""

from __future__ import annotations

import sqlite3
from datetime import date, datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING

from skyrisk.config import Hub
from skyrisk.models import NRI_HAZARDS, NriCounty, WeatherDay

if TYPE_CHECKING:
    from skyrisk.scoring.engine import ScoreResult

_NRI_COLUMNS = ",\n    ".join(
    f"{h.lower()}_afreq REAL, {h.lower()}_risks REAL" for h in NRI_HAZARDS
)

SCHEMA = f"""
CREATE TABLE IF NOT EXISTS hubs (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    city TEXT NOT NULL,
    state TEXT NOT NULL,
    lat REAL NOT NULL,
    lon REAL NOT NULL,
    region TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS weather_daily (
    hub_id TEXT NOT NULL REFERENCES hubs(id),
    date TEXT NOT NULL,
    snowfall_cm REAL,
    temp_max_c REAL,
    temp_min_c REAL,
    precip_mm REAL,
    wind_gust_max_kmh REAL,
    PRIMARY KEY (hub_id, date)
);
CREATE TABLE IF NOT EXISTS nri_county (
    hub_id TEXT PRIMARY KEY REFERENCES hubs(id),
    county_fips TEXT NOT NULL,
    county_name TEXT NOT NULL,
    state TEXT NOT NULL,
    nri_version TEXT NOT NULL,
    fetched_at TEXT NOT NULL,
    area_sqmi REAL,
    {_NRI_COLUMNS}
);
CREATE TABLE IF NOT EXISTS score_runs (
    run_id INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at TEXT NOT NULL,
    config_version TEXT NOT NULL,
    config_hash TEXT NOT NULL,
    data_hash TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS hub_scores (
    run_id INTEGER NOT NULL REFERENCES score_runs(run_id),
    hub_id TEXT NOT NULL REFERENCES hubs(id),
    overall REAL NOT NULL,
    rank INTEGER NOT NULL,
    PRIMARY KEY (run_id, hub_id)
);
CREATE TABLE IF NOT EXISTS hazard_scores (
    run_id INTEGER NOT NULL REFERENCES score_runs(run_id),
    hub_id TEXT NOT NULL REFERENCES hubs(id),
    hazard TEXT NOT NULL,
    sub_score REAL NOT NULL,
    PRIMARY KEY (run_id, hub_id, hazard)
);
CREATE TABLE IF NOT EXISTS hub_metrics (
    run_id INTEGER NOT NULL REFERENCES score_runs(run_id),
    hub_id TEXT NOT NULL REFERENCES hubs(id),
    metric TEXT NOT NULL,
    raw REAL NOT NULL,
    scored INTEGER NOT NULL,
    PRIMARY KEY (run_id, hub_id, metric)
);
CREATE TABLE IF NOT EXISTS metric_values (
    run_id INTEGER NOT NULL REFERENCES score_runs(run_id),
    hub_id TEXT NOT NULL REFERENCES hubs(id),
    hazard TEXT NOT NULL,
    metric TEXT NOT NULL,
    raw REAL NOT NULL,
    normalized REAL NOT NULL,
    contribution REAL NOT NULL,
    note TEXT,
    PRIMARY KEY (run_id, hub_id, hazard, metric)
);
CREATE TABLE IF NOT EXISTS near_term_snapshots (
    hub_id TEXT NOT NULL REFERENCES hubs(id),
    checked_at TEXT NOT NULL,
    score REAL NOT NULL,
    level TEXT NOT NULL,
    config_version TEXT NOT NULL,
    PRIMARY KEY (hub_id, checked_at)
);
CREATE TABLE IF NOT EXISTS alerts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at TEXT NOT NULL,
    hub_id TEXT NOT NULL REFERENCES hubs(id),
    prev_score REAL NOT NULL,
    new_score REAL NOT NULL,
    prev_level TEXT NOT NULL,
    new_level TEXT NOT NULL,
    delta REAL NOT NULL,
    reason TEXT NOT NULL,
    detail TEXT NOT NULL,
    demo INTEGER NOT NULL,
    webhook_status TEXT NOT NULL
);
"""


def connect(path: Path | str, *, check_same_thread: bool = True) -> sqlite3.Connection:
    if str(path) != ":memory:":
        Path(path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, check_same_thread=check_same_thread)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.executescript(SCHEMA)
    _migrate(conn)
    return conn


def _migrate(conn: sqlite3.Connection) -> None:
    columns = {r["name"] for r in conn.execute("PRAGMA table_info(nri_county)")}
    if "area_sqmi" not in columns:
        # Rows cached before area was fetched keep NULL and get re-fetched on ingest.
        conn.execute("ALTER TABLE nri_county ADD COLUMN area_sqmi REAL")
        conn.commit()


def upsert_hubs(conn: sqlite3.Connection, hubs: list[Hub]) -> None:
    conn.executemany(
        """INSERT INTO hubs (id, name, city, state, lat, lon, region)
           VALUES (:id, :name, :city, :state, :lat, :lon, :region)
           ON CONFLICT(id) DO UPDATE SET name=excluded.name, city=excluded.city,
             state=excluded.state, lat=excluded.lat, lon=excluded.lon, region=excluded.region""",
        [h.model_dump() for h in hubs],
    )
    conn.commit()


# --- weather -----------------------------------------------------------------

def weather_day_count(conn: sqlite3.Connection, hub_id: str, start: date, end: date) -> int:
    row = conn.execute(
        "SELECT COUNT(*) FROM weather_daily WHERE hub_id = ? AND date BETWEEN ? AND ?",
        (hub_id, start.isoformat(), end.isoformat()),
    ).fetchone()
    return row[0]


def upsert_weather(conn: sqlite3.Connection, hub_id: str, days: list[WeatherDay]) -> None:
    conn.executemany(
        """INSERT OR REPLACE INTO weather_daily
           (hub_id, date, snowfall_cm, temp_max_c, temp_min_c, precip_mm, wind_gust_max_kmh)
           VALUES (?, ?, ?, ?, ?, ?, ?)""",
        [
            (hub_id, d.date.isoformat(), d.snowfall_cm, d.temp_max_c, d.temp_min_c,
             d.precip_mm, d.wind_gust_max_kmh)
            for d in days
        ],
    )
    conn.commit()


def load_weather(conn: sqlite3.Connection, hub_id: str, start: date, end: date) -> list[WeatherDay]:
    rows = conn.execute(
        """SELECT date, snowfall_cm, temp_max_c, temp_min_c, precip_mm, wind_gust_max_kmh
           FROM weather_daily WHERE hub_id = ? AND date BETWEEN ? AND ? ORDER BY date""",
        (hub_id, start.isoformat(), end.isoformat()),
    ).fetchall()
    return [WeatherDay(**dict(r)) for r in rows]


# --- FEMA NRI ------------------------------------------------------------------

def upsert_nri(conn: sqlite3.Connection, hub_id: str, county: NriCounty) -> None:
    values: dict[str, object] = {
        "hub_id": hub_id,
        "county_fips": county.county_fips,
        "county_name": county.county_name,
        "state": county.state,
        "nri_version": county.nri_version,
        "area_sqmi": county.area_sqmi,
        "fetched_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }
    for h in NRI_HAZARDS:
        key = h.lower()
        values[f"{key}_afreq"] = county.afreq.get(key)
        values[f"{key}_risks"] = county.risks.get(key)
    cols = ", ".join(values)
    params = ", ".join(f":{c}" for c in values)
    conn.execute(f"INSERT OR REPLACE INTO nri_county ({cols}) VALUES ({params})", values)
    conn.commit()


def load_nri(conn: sqlite3.Connection, hub_id: str) -> NriCounty | None:
    row = conn.execute("SELECT * FROM nri_county WHERE hub_id = ?", (hub_id,)).fetchone()
    if row is None or row["area_sqmi"] is None:
        return None
    return NriCounty(
        county_fips=row["county_fips"],
        county_name=row["county_name"],
        state=row["state"],
        nri_version=row["nri_version"],
        area_sqmi=row["area_sqmi"],
        afreq={h.lower(): row[f"{h.lower()}_afreq"] for h in NRI_HAZARDS},
        risks={h.lower(): row[f"{h.lower()}_risks"] for h in NRI_HAZARDS},
    )


# --- score runs ------------------------------------------------------------------

def persist_run(conn: sqlite3.Connection, result: ScoreResult) -> int:
    cur = conn.execute(
        """INSERT INTO score_runs (created_at, config_version, config_hash, data_hash)
           VALUES (?, ?, ?, ?)""",
        (datetime.now(timezone.utc).isoformat(timespec="seconds"),
         result.config_version, result.config_hash, result.data_hash),
    )
    run_id = cur.lastrowid
    conn.executemany(
        "INSERT INTO hub_metrics (run_id, hub_id, metric, raw, scored) VALUES (?, ?, ?, ?, ?)",
        [
            (run_id, hub_id, metric, raw, metric in result.scored_metrics)
            for hub_id, metrics in sorted(result.inputs.items())
            for metric, raw in sorted(metrics.items())
        ],
    )
    for hub in result.hubs:
        conn.execute(
            "INSERT INTO hub_scores (run_id, hub_id, overall, rank) VALUES (?, ?, ?, ?)",
            (run_id, hub.hub_id, hub.overall, hub.rank),
        )
        for hazard in hub.hazards:
            conn.execute(
                "INSERT INTO hazard_scores (run_id, hub_id, hazard, sub_score) VALUES (?, ?, ?, ?)",
                (run_id, hub.hub_id, hazard.hazard, hazard.sub_score),
            )
            conn.executemany(
                """INSERT INTO metric_values
                   (run_id, hub_id, hazard, metric, raw, normalized, contribution, note)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                [
                    (run_id, hub.hub_id, hazard.hazard, m.metric, m.raw, m.normalized,
                     m.contribution, result.notes.get(hub.hub_id, {}).get(m.metric))
                    for m in hazard.metrics
                ],
            )
    conn.commit()
    return run_id


def latest_run_id(conn: sqlite3.Connection) -> int | None:
    row = conn.execute("SELECT MAX(run_id) FROM score_runs").fetchone()
    return row[0]


# --- near-term snapshots and alerts ------------------------------------------------
# Written at runtime by the alert check. On Render's free tier the disk is ephemeral, so these
# tables start empty after every restart (the first check then only sets a baseline).

def latest_snapshots(conn: sqlite3.Connection) -> dict[str, sqlite3.Row]:
    rows = conn.execute(
        """SELECT s.* FROM near_term_snapshots s
           JOIN (SELECT hub_id, MAX(checked_at) AS latest FROM near_term_snapshots GROUP BY hub_id) m
             ON s.hub_id = m.hub_id AND s.checked_at = m.latest"""
    ).fetchall()
    return {r["hub_id"]: r for r in rows}


def insert_snapshots(conn: sqlite3.Connection, checked_at: str, rows: list[tuple[str, float, str]],
                     config_version: str) -> None:
    conn.executemany(
        "INSERT OR REPLACE INTO near_term_snapshots (hub_id, checked_at, score, level, config_version) "
        "VALUES (?, ?, ?, ?, ?)",
        [(hub_id, checked_at, score, level, config_version) for hub_id, score, level in rows],
    )


def insert_alert(conn: sqlite3.Connection, values: dict[str, object]) -> int:
    cols = ", ".join(values)
    params = ", ".join(f":{c}" for c in values)
    return conn.execute(f"INSERT INTO alerts ({cols}) VALUES ({params})", values).lastrowid


def set_webhook_status(conn: sqlite3.Connection, alert_ids: list[int], status: str) -> None:
    conn.executemany("UPDATE alerts SET webhook_status = ? WHERE id = ?", [(status, i) for i in alert_ids])
    conn.commit()


def recent_alerts(conn: sqlite3.Connection, *, limit: int, hub_ids: list[str] | None = None,
                  since: str | None = None) -> list[sqlite3.Row]:
    where, params = [], []
    if hub_ids:
        where.append(f"hub_id IN ({', '.join('?' for _ in hub_ids)})")
        params += hub_ids
    if since:
        where.append("created_at >= ?")
        params.append(since)
    clause = f"WHERE {' AND '.join(where)}" if where else ""
    return conn.execute(f"SELECT * FROM alerts {clause} ORDER BY created_at DESC, id DESC LIMIT ?",
                        (*params, limit)).fetchall()


def last_check_at(conn: sqlite3.Connection) -> str | None:
    return conn.execute("SELECT MAX(checked_at) FROM near_term_snapshots").fetchone()[0]
