# Spec: Data + Scoring Core (Roadmap Phase 1, items 1–3)

## Context

SkyRisk is a greenfield project: the repo holds only `agent-os/` docs. Before the agent layer can exist (roadmap items 4–7), there has to be a data foundation it can call. This spec builds the **hub registry**, the **public-data ingestion + SQLite cache**, and the **deterministic scoring engine**. The mission rules this work must follow:

- The LLM never produces scores. Every score comes from code and is reproducible.
- Scores are **relative rankings across hubs**, not probabilities of shutdown.
- Raw data must support ad-hoc stats such as "% of snow days".
- Score history is persisted, which prepares for the stretch-goal alerts.

Outcome: `uv run skyrisk ingest && uv run skyrisk score` fills SQLite and prints a ranked table of the 13 hubs, with per-hazard sub-scores that trace back to raw metrics.

**Shaping decisions:**
- 13 hubs: Memphis, Louisville, Chicago, Dallas, Atlanta, Houston, Miami, Kansas City, Denver, Phoenix, Columbus, Newark, Minneapolis. They are stored as a YAML seed that is loaded into SQLite.
- Scoring: min-max normalization (0–100 across hubs), with per-hazard and overall weights taken from a versioned YAML config.
- FEMA NRI scoring inputs: annualized frequency (`*_AFREQ`), not the composite `*_RISKS` score. RISKS is stored for explanations only.
- Open-Meteo window: 10 years, from 2016-01-01 to 2025-12-31. The end date is fixed so results are reproducible.
- No visuals, no existing code references, and no standards: `agent-os/standards/index.yml` is empty.

---

## Task 1: Save spec documentation

Create `agent-os/specs/2026-09-27-1426-data-scoring-core/` with:
- **plan.md**: this plan.
- **shape.md**: scope, the decisions above, and product alignment (relative ranking, determinism, score history). It includes a decision entry: **score on NRI `*_AFREQ` (hazard frequency at the location), not `*_RISKS`**, because RISKS is driven by population and building exposure and by social vulnerability, which inflates large counties like Cook for every hazard. RISKS is stored for reference only.
- **standards.md**: records that no standards exist yet and suggests running `/discover-standards` after this spec.
- **references.md**: no code references. It lists the external data docs instead: Open-Meteo Archive API; FEMA NRI Counties ArcGIS layer (item `39485e8035d446a5bff03259508ae355`); NRI Technical Documentation v1.20.
- No `visuals/` folder, since there are no visuals.

## Task 2: Project scaffold

- `pyproject.toml` (uv, Python 3.12). Dependencies: `httpx`, `pydantic>=2`, `pyyaml`. Dev dependencies: `pytest`, `respx` (or `httpx.MockTransport`). Console script: `skyrisk = skyrisk.cli:main`.
- Layout:
  ```
  config/hubs.yaml, config/scoring.yaml
  src/skyrisk/{config.py, db.py, hubs.py, cli.py}
  src/skyrisk/ingest/{open_meteo.py, fema_nri.py}
  src/skyrisk/scoring/{metrics.py, engine.py}
  tests/ (+ tests/fixtures/)
  data/            # skyrisk.db, gitignored
  ```
- `.gitignore` (`data/`, `.env`, `.venv`) and `.env.example` (placeholder only; no keys are needed yet).

## Task 3: Hub registry

- `config/hubs.yaml`: 13 entries with `id` (slug), `name`, `city`, `state`, `lat`, `lon`, `region` (Northeast/Southeast/Midwest/South/West).
- `config.py`: Pydantic models (`Hub`, `HubRegistry`) validate the entries: lat/lon within US bounds, unique ids.
- `db.py`: builds the schema with plain `sqlite3`, which is idempotent. Tables:
  - `hubs`
  - `weather_daily(hub_id, date, snowfall_cm, temp_max_c, temp_min_c, precip_mm, wind_gust_max_kmh)`, primary key `(hub_id, date)`
  - `nri_county(hub_id, county_fips, county_name, nri_version, fetched_at, <hz>_afreq, <hz>_risks)` for each hazard `hz` in hrcn, ifld, cfld, trnd, wntw, hwav, cwav. `afreq` is used for scoring; `risks` is kept for reference and explanations.
  - `score_runs(run_id, created_at, config_version, config_hash, data_hash)`
  - `hub_scores(run_id, hub_id, overall, rank)`
  - `hazard_scores(run_id, hub_id, hazard, sub_score)`
  - `metric_values(run_id, hub_id, metric, raw, normalized)`
- `hubs.py`: `seed_hubs(conn, registry)` upserts the hubs.

## Task 4: Open-Meteo ingestion

- `ingest/open_meteo.py`: `fetch_daily(hub, start, end, client) -> list[WeatherDay]`. It calls `https://archive-api.open-meteo.com/v1/archive` with `daily=snowfall_sum,temperature_2m_max,temperature_2m_min,precipitation_sum,wind_gusts_10m_max`, metric units, and `timezone=auto`.
- Responses are validated with a Pydantic model, and a missing or null day is kept as `NULL`.
- Caching: if a hub already has the full date range in `weather_daily`, it is skipped unless `--refresh` is passed. Uses one call per hub (~3,650 rows), polite sequential requests, and retry with backoff on 429/5xx.

## Task 5: FEMA NRI ingestion

- `ingest/fema_nri.py`: sends a point-in-polygon query (`geometry=lon,lat`, `esriGeometryPoint`, `outFields=...`) to the NRI Counties ArcGIS FeatureServer and returns two fields per hazard for the county:
  - **`*_AFREQ` (annualized frequency, events/yr): used for scoring.**
  - `*_RISKS` (composite risk score): stored only.
- Hazards: hurricane (HRCN), inland flooding (IFLD; named RFLD before v1.20), coastal flood (CFLD), tornado (TRND), winter weather (WNTW), heat wave (HWAV), cold wave (CWAV).
- **Why AFREQ, not RISKS:** RISKS depends on population and building exposure and on social vulnerability. That would inflate large counties (e.g. Cook County) for every hazard. For hub-site exposure we want how often the hazard occurs at the location.
- **Verify at implementation:** resolve the FeatureServer query URL from the ArcGIS item, and confirm the exact `*_AFREQ` / `*_RISKS` field names against the v1.20 data dictionary. Record `nri_version`.
- Cached the same way as the weather data. A null value (e.g. CFLD_AFREQ for inland counties) is stored as 0 for scoring and flagged in `metric_values` notes.

> **Implementation note (v1.2):** only TRND AFREQ is normalized by county area, as events/yr per 1,000 sq mi using max(AREA, 1,000 sq mi). IFLD stays raw. Wind was dropped from hurricane but is still stored as an unscored stat. See shape.md for the reasoning.

## Task 6: Deterministic scoring engine

- `config/scoring.yaml` (versioned, e.g. `version: "1.0"`), with suggested starting weights:
  - thresholds: snow day ≥ 1 cm; heavy snow ≥ 10 cm; extreme heat Tmax ≥ 35 °C; extreme cold Tmin ≤ −18 °C; heavy rain ≥ 50 mm; high wind gust ≥ 90 km/h
  - hazards → metrics (with weights inside each hazard):
    - **winter**: snow_days, heavy_snow_days, extreme_cold_days, nri_wntw_afreq, nri_cwav_afreq
    - **heat**: extreme_heat_days, nri_hwav_afreq
    - **flood**: heavy_rain_days, nri_ifld_afreq, nri_cfld_afreq
    - **hurricane**: nri_hrcn_afreq (high_wind_days computed and stored, not scored, per v1.1)
    - **tornado**: nri_trnd_afreq_per_1k_sqmi
    - NRI `*_RISKS` values are never scoring inputs. They appear only as context in `skyrisk show`.
  - overall weights: winter .25, hurricane .25, flood .20, tornado .15, heat .15 (sum validated = 1)
- `scoring/metrics.py`: pure functions that turn daily rows into per-year averages (e.g. `snow_days_per_year`). A generic `pct_days(rows, predicate)` also serves future "% of snow days" agent tools.
- `scoring/engine.py`: pure `score(metrics_by_hub, config) -> ScoreResult`.
  1. Min-max normalize each metric across hubs to 0–100. If all hubs are equal, every hub gets 0.
  2. Take each hazard's sub-score as the weighted mean of its normalized metrics.
  3. Compute overall as the weighted sum of sub-scores, then rank; ties are broken by hub id.
  - Values are rounded to 2 dp only for display, and no randomness is used.
- `persist_run(conn, result)` writes a new `score_runs` row with a SHA-256 of the config and a hash of the input metrics, plus all per-metric raw and normalized values. Every score can therefore be explained later.

## Task 7: CLI

`cli.py` (argparse, which adds no extra dependency):
- `skyrisk ingest [--refresh] [--hub ID]`: seeds the hubs, then fetches weather and NRI data.
- `skyrisk score`: computes the scores, persists them, and prints a ranked table with the sub-scores.
- `skyrisk show HUB_ID`: prints the latest run's metrics for one hub (raw → normalized → contribution).

## Task 8: Tests

- `test_config.py`: hub YAML validation, and the weight-sum check.
- `test_metrics.py`: threshold counting, per-year averaging, handling of null days.
- `test_engine.py`:
  - hand-computed 3-hub fixture, checked for exact scores and ranks
  - all-equal metric → 0
  - determinism: running twice gives identical output and hashes
  - raising one hub's metric never lowers its hazard score
- `test_ingest.py`: Open-Meteo and NRI parsing against recorded JSON fixtures via mocked httpx, plus a cache-skip check. **No live network in tests.**

---

## Verification

1. `uv sync && uv run pytest`: all tests pass.
2. `uv run skyrisk ingest`: 13 hubs, about 47k `weather_daily` rows, and 13 `nri_county` rows. `sqlite3 data/skyrisk.db "select hub_id,count(*) from weather_daily group by 1"`
3. `uv run skyrisk score`, sanity check: Minneapolis and Chicago lead winter; Miami and Houston lead hurricane; Phoenix and Dallas lead heat; Kansas City, Dallas and Memphis rank high on tornado.
4. Run `score` again and confirm the new run has identical scores and the same `config_hash`/`data_hash`.
5. `uv run skyrisk show minneapolis`: each sub-score traces back to its raw metrics.
6. Check that Chicago (Cook County) does not lead every hazard, which confirms AFREQ scoring removes the population bias. A test asserts that the engine's metric list contains no `*_risks` keys.
