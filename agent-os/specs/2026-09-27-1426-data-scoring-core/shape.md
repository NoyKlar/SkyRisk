# Data + Scoring Core — Shaping Notes

## Scope

Roadmap Phase 1, items 1–3: the hub registry, public-data ingestion with a local SQLite cache, and the deterministic scoring engine. This is the foundation the agent layer (items 4–7) will call through tools. Out of scope here: the agent, API, UI, guardrails and evals.

## Decisions

- **Hubs:** 13 fixed US hubs: Memphis, Louisville, Chicago, Dallas, Atlanta, Houston, Miami, Kansas City, Denver, Phoenix, Columbus, Newark, Minneapolis. Minneapolis is included so Midwest winter comparisons have a strong reference point. They are stored as a YAML seed (`config/hubs.yaml`) and loaded into SQLite.
- **Weather window:** Open-Meteo daily history from 2016-01-01 to 2025-12-31 (10 years). The end date is fixed so results are reproducible.
- **Scoring method:** each metric is min-max normalized to 0–100 across the hubs. Each hazard sub-score is the weighted mean of its metrics, and the overall score is the weighted sum of hazard sub-scores. Weights and thresholds live in a versioned `config/scoring.yaml`. Scores are relative rankings between hubs, not probabilities.
- **FEMA NRI inputs: score on `*_AFREQ`, not `*_RISKS`.** The NRI composite risk score is driven by population/building exposure and social vulnerability, which would inflate large counties (e.g. Cook County / Chicago) for every hazard. For hub-site exposure we want the hazard's annualized frequency at the location. `*_RISKS` is still stored in `nri_county` for reference and explanations, but is never a scoring input. Exact field names are verified against the NRI v1.20 data dictionary at implementation time.
- **NRI v1.20 field names verified against the live service:** HRCN, IFLD (Inland Flooding, which replaced RFLD Riverine), CFLD, TRND, WNTW, HWAV, CWAV, each with `_AFREQ` and `_RISKS`; the version field is `NRI_VER` ("December 2025"). Example: Cook County `*_RISKS` = 100 for WNTW and CWAV, and ~99.9 for TRND and IFLD, which confirms the population bias.
- **County-size normalization (decided from real scoring runs; current config v1.2):** NRI `*_AFREQ` is a county-wide event count, so for localized hazards it grows with county size. In v1.0, Houston (Harris County, 1,795 sq mi) scored 100 on tornado, and Phoenix (Maricopa, 9,319 sq mi) outranked Kansas City.
  - **TRND only** is scored per 1,000 sq mi (`nri_trnd_afreq_per_1k_sqmi`), using NRI `AREA` (sq mi). Tornadoes are localized, so a larger county simply catches more of them.
  - **Area floor:** the denominator is `max(county_area, 1000 sq mi)` (`nri_area_floor_sqmi` in scoring.yaml). Without it, small counties were inflated by a handful of events: in v1.1, Denver County (156 sq mi, 0.16 tornadoes/yr) scored 100 on tornado. With the floor, Denver drops to 11th of 13.
  - **IFLD stays raw (reverted in v1.2).** v1.1 also area-normalized IFLD. That put Houston 11th of 13 on inland flooding (Harris: 5.1 events/yr ÷ 1,795 sq mi = 2.9 per 1k sq mi) and Newark 1st (Essex: 3.1 ÷ 131 = 23.5), which contradicts Houston's well-known flood exposure. NRI inland-flood events behave like area-wide event-days rather than point events, so dividing by area over-corrects.
  - **HRCN stays raw**, because hurricane footprints are far larger than any county; dividing by area would wrongly penalize large coastal counties like Miami-Dade. WNTW, HWAV, CWAV and CFLD also stay raw, since they are area-wide events.
  - `AREA` and the raw `*_AFREQ` values are stored in `nri_county`.
  - **v1.2 check:** the tornado order is Dallas, Houston, Chicago, Memphis, …, Kansas City 7th, Denver 11th, Phoenix 12th. Kansas City's middle rank reflects the NRI source data (Jackson County 0.38/yr); it is not an artifact of normalization.
  - **Known limitation:** raw county-level IFLD is still affected by very large counties. Phoenix (Maricopa, ~8.5 inland-flood event-days/yr countywide) ranks 4th on flood. The agent should state that NRI values describe the whole county, not the hub site.
- **Wind is not a hurricane signal (config v1.1):** `high_wind_days` (gusts ≥ 90 km/h) was removed from the hurricane hazard in v1.1. High gusts also come from thunderstorms, winter storms and chinook winds (e.g. Denver), and at 0–1 days/yr single storms decided the ranking (Newark was #1). Hurricane now uses NRI `HRCN_AFREQ` only. The wind metric is still computed and stored per run in `hub_metrics` (with `scored = 0`), so the agent can answer wind questions as a stat.
- **Reproducibility:** every score run records the config version, a SHA-256 of the config, and a hash of the input metrics. All raw and normalized metric values are persisted, so any score can be explained.
- **Score history:** kept per run. This is groundwork for the stretch-goal alerts.
- **No live network in tests:** ingestion is tested against recorded JSON fixtures.

## Context

- **Visuals:** None
- **References:** None. This is a greenfield repo. External data docs are listed in references.md.
- **Product alignment:**
  - The LLM never invents scores; scoring is deterministic code.
  - Scores are relative rankings, not absolute predictions.
  - Raw daily data supports stats such as "% of snow days".
  - SQLite score history fits the tech stack and the alert groundwork.

## Standards Applied

None. `agent-os/standards/index.yml` is empty. Consider running `/discover-standards` once this code exists.
