# Near-term risk score + alerts (roadmap Phase 2 #1 and #4)

## Context

SkyRisk's only score today is **historical exposure**: Open-Meteo 2016–2025 plus FEMA NRI, min-max relative across 13 hubs. It answers "where should we invest", not "what's coming this week". Roadmap Phase 2 asks for scheduled alerts (#1) and a live forecast layer (#4).

This adds a second score per hub. The **near-term risk** score is 0–100 and absolute (not relative across hubs), computed deterministically from the Open-Meteo 7-day forecast with versioned YAML thresholds and weights. Around it:
- an alert flow: SQLite snapshots, a token-protected check endpoint, and a Slack-compatible webhook
- a daily GitHub Actions cron
- a demo mode
- a public alerts feed and UI panel
- an agent tool
- a prompt rule that the history covers full years 2016–2025 only, and that 2026 is excluded on purpose

The historical score is unchanged.

**Decisions made while shaping**
- **Agent tool source: live forecast plus a 1 h in-memory cache.** It never writes alert snapshots, so chat traffic can't move the alert baseline. It works right after a restart.
- **Score math: capped points.** Each hazard earns up to `max_points`. The config validator enforces `max_points ≥ levels.high`, so one hazard at full severity on day 1 reaches "high" on its own (user rule: one blizzard must be able to trigger a high alert).
- **Scheduling: GitHub Actions cron**, not APScheduler, because Render's free tier sleeps. Update `tech-stack.md` and `roadmap.md` to match.
- **Visuals:** none. The panel matches the existing hubs side panel.
- **Standards:** backend/external-api-clients, testing/injectable-side-effects, testing/no-live-network.
- **Repo rules:** commit on `main`, never push without an explicit OK (Render auto-deploys), and estimate cost before any paid eval run.

---

## Task 1: Save spec documentation

Create `agent-os/specs/2026-09-28-0947-near-term-risk-alerts/` (same format as `2026-09-27-2132-jev-classifier-comparison/`):
- **plan.md**: this plan
- **shape.md**: scope (the user's brief), the decisions above, context (visuals: none; product alignment: roadmap Phase 2 #1 + #4, with APScheduler replaced by a GH Actions cron)
- **standards.md**: the full text of the three standards
- **references.md**:
  - `ingest/open_meteo.py`: client pattern, `_ArchiveResponse` unit pinning
  - `agent/tools.py`: `ToolSpec`, `ToolResult.scores()`, caveats
  - `config.py`: Pydantic YAML config with validators and `config_hash`
  - `evals/checks.py`: `_check_grounding`
  - `api/app.py`: routes, `log`, `limiter`
  - `static/index.html`: the `#hubs-panel` aside
  - `ingest/http.py`: `_with_retries`

## Task 2: Near-term config + pure engine

- **`config/near_term.yaml`** (version `"1.0"`). Proposed defaults, tunable; `watch`/`severe` are chosen next to the historical thresholds:
  ```yaml
  version: "1.0"
  forecast_days: 7
  lead_time_weights: [1.0, 1.0, 0.9, 0.8, 0.7, 0.6, 0.6]   # day 0 = today; forecast skill decays
  hazards:
    snow:         {variable: snowfall_cm,       watch: 2,  severe: 15,  max_points: 70}
    wind:         {variable: wind_gust_max_kmh, watch: 60, severe: 100, max_points: 70}
    heavy_rain:   {variable: precip_mm,         watch: 25, severe: 75,  max_points: 70}
    extreme_heat: {variable: temp_max_c,        watch: 35, severe: 42,  max_points: 70}
  levels: {medium: 35, high: 65}          # <35 low, 35–65 medium, ≥65 high
  alerts: {change_threshold: 20}          # |Δscore| ≥ 20 or any level crossing
  cache_ttl_s: 3600
  demo: {day_offset: 1, values: {snowfall_cm: 30, wind_gust_max_kmh: 95}}
  ```
- **`config.py`**:
  - add a `NearTermConfig` model and `load_near_term_config()`, with `config_hash()` like `ScoringConfig`
  - validators: `watch < severe`; `len(lead_time_weights) == forecast_days`; weights in (0, 1]; `variable` is a `WeatherDay` field; `medium < high`; **every `max_points ≥ levels.high`**
- **`src/skyrisk/nearterm/engine.py`** (pure): `score_forecast(days: list[WeatherDay], cfg) -> NearTermScore` with fields `score`, `level`, and per-hazard `{peak_value, peak_date, severity, points}`.
  - For each hazard, a day's severity is `clamp((v − watch) / (severe − watch), 0, 1) × lead_time_weight[d]`.
  - The hazard's severity is the maximum over the days; its points are `max_points × severity`.
  - `score = min(100, Σ points)`, rounded to 2 decimals.
  - A `None` value counts as 0 severity and adds a note.
  - Also add `level_for(score, cfg)`.

## Task 3: Forecast client (external-api-clients standard)

- **`ingest/open_meteo.py`**:
  - add `FORECAST_URL = "https://api.open-meteo.com/v1/forecast"`
  - add `parse_forecast(payload) -> list[WeatherDay]`, reusing `_ArchiveResponse` (same `DAILY_VARS` and `EXPECTED_UNITS`, fails loud)
  - add `fetch_forecast(hub, client, *, days=7, retries=1)`, which only builds params and calls `get_json`
  - `retries` is passed through so the chat path doesn't inherit `get_json`'s ~2-minute retry schedule
- **`ingest/http.py`**: change `_with_retries` to return the `Response`. `get_json` and `post_json` call `.json()` on it. Add `post(client, url, body, *, retries=2, sleep)` for webhooks, since Slack replies with plain text `ok`, not JSON.
- **Fixtures**:
  - record one real forecast per hub type with curl: `tests/fixtures/open_meteo_forecast_houston.json` and `..._minneapolis.json`, never hand-edited
  - storm and heat scenarios are synthetic payloads built in the test code

## Task 4: Near-term service (live + cache)

`src/skyrisk/nearterm/service.py`: `NearTermService(registry, cfg, fetch: Callable[[Hub], list[WeatherDay]], *, now=utc_now)`.
- `score(hub_id)` and `score_all()` return `NearTermScore` plus `fetched_at`
- the per-hub cache honors `cache_ttl_s`, using the injected `now`
- a fetch failure raises `NearTermUnavailable`
- `override_forecast(hub_id, days)` supports the demo, which never touches the cache

## Task 5: Alert store + check flow

- **`db.py` SCHEMA**: add these tables. `connect()` creates them at runtime, and the build-time DB has them empty.
  - `near_term_snapshots(hub_id, checked_at, score, level, config_version, PRIMARY KEY(hub_id, checked_at))`
  - `alerts(id, created_at, hub_id, prev_score, new_score, prev_level, new_level, delta, reason, demo INTEGER, webhook_status)`
- **`src/skyrisk/nearterm/alerts.py`**: `run_check(conn, service, cfg, *, now, notify, demo_hub=None, lock) -> CheckResult`.
  - **Normal run:** for each hub, load the last snapshot.
    - With **no snapshot**, the run sets the baseline and raises no alert. This is the documented behavior after a restart.
    - Otherwise an alert fires if `|Δ| ≥ change_threshold` or the level changed, in either direction; `reason` says which.
    - A new snapshot is saved for every hub.
  - **Demo run:** swap `demo_hub`'s forecast for the `demo:` scenario (merged into the real forecast at `day_offset`) and score it through the same engine.
    - The baseline is this check's **live** score for that hub.
    - It always writes `demo=1`, prefixes the webhook text with `[DEMO]`, and **writes no snapshots**, so the real baseline is untouched.
  - A `threading.Lock` serializes checks: the API shares one connection, and two concurrent checks must not double-alert.
- **Webhook** (`notify`): Slack-compatible `{"text": "..."}`, e.g. `:warning: Houston near-term risk 28 → 71 (low → high): heavy rain peak 82 mm on 2026-09-30`.
  - `ALERT_WEBHOOK_URL` unset → `webhook_status="skipped"`
  - a failure → `"failed"` plus a log line; it never fails the check
  - One message per check that lists all alerts, so a big weather day doesn't mean 13 webhook calls.
- **CLI**: `skyrisk alerts check [--demo HUB]` runs the same function locally without a token. `skyrisk alerts list` prints recent alerts.

## Task 6: API endpoints (`api/app.py`)

- **`POST /api/alerts/check`**, body `{"demo_hub": str | null}`.
  - Requires `Authorization: Bearer <ALERT_TOKEN>`, compared with `hmac.compare_digest`.
  - `ALERT_TOKEN` unset → `503` "alert checks are disabled" (never open). A bad or missing token → `401`.
  - Not counted by the chat rate limiter.
  - Returns `{checked_at, hubs_checked, baseline_only: [...], alerts: [...], webhook_status}`.
- **`GET /api/alerts?limit=20&hub_id=`**: public (`limit ≤ 50`). Returns `{alerts, last_check_at}`.
- `create_app` gains `near_term: NearTermService | None` and an `alerts` dependency. `cli._serve` owns the `httpx.Client` (10 s timeout) and wires everything, following the injectable-side-effects rule.

## Task 7: Scheduling + deploy config

- **`.github/workflows/near-term-check.yml`**:
  - `schedule: cron "0 11 * * *"` plus `workflow_dispatch` with an optional `demo_hub` input
  - one step: `curl -fsS --retry 4 --retry-delay 30 --retry-all-errors --max-time 180 -X POST "$SKYRISK_URL/api/alerts/check" -H "Authorization: Bearer $ALERT_TOKEN" -d …`, which rides out the 30–60 s cold start
  - uses secret `ALERT_TOKEN` and repo var `SKYRISK_URL`
- **`render.yaml`**: add `ALERT_TOKEN` and `ALERT_WEBHOOK_URL` as `sync: false`.
- **`.env.example`**: add both, with comments.

## Task 8: Agent tool + prompt

- **`agent/tools.py`**:
  - `ToolContext` gains `near_term: NearTermService | None = None`, so existing constructions keep working
  - new tool `near_term_risk {hub_ids: list[str] | null}` (null = all hubs, sorted by score desc)
  - each row carries: score, level, hazard breakdown (peak value and date), forecast window, `fetched_at`, the last snapshot (score, `checked_at`) if any, and alerts from the last 7 days
  - `scores()` → `ScoreRef(hazard="near_term")`
  - caveats: absolute 0–100 forecast severity, **not** relative and not comparable with historical scores; forecasts are uncertain and weighted down with lead time; config version; alert history resets when the server restarts
  - `ToolError` when the service is unconfigured or unavailable
  - thread the service through `agent/factory.py` for chat, serve and eval
- **`agent/prompts.py`**:
  - **Scope:** add near-term forecast risk and alerts for these hubs.
  - **Numbers:** historical scores are relative. `near_term` scores are absolute, with low/medium/high levels. Never add, average or rank the two together.
  - **Time:** replace the current line with: "Historical weather data covers the full calendar years 2016–2025 only. 2026 is intentionally excluded because it is incomplete. For any historical question about 2026 (or 'this year'), say that no historical data exists for it and do not guess. For what is coming in the next 7 days, use near_term_risk."
  - The prompt stays byte-stable (built at startup).
- **`agent/guardrails.py` `CLASSIFIER_PROMPT`**: add "near-term forecasts and alerts for the hubs" to the in_scope list.
- **`weather_stat`**: the out-of-window `ToolError` for 2026 says explicitly that the window is 2016–2025 full years and 2026 is excluded.

## Task 9: UI alerts panel

In the existing `#hubs-panel` aside (`static/index.html`, `app.js`, `app.css`), add a "Recent alerts" section:
- fetch `GET /api/alerts?limit=10` on load
- each row: city, `prev → new`, a level badge (low/medium/high), relative time, and a `DEMO` badge when `demo`
- empty state: "No alerts yet. A forecast check runs daily."
- add a chip: "What's the near-term risk for Houston this week?"
- no token or demo button in the public UI

## Task 10: Tests (offline, fixtures only)

- `test_open_meteo.py`: `parse_forecast` on the recorded fixtures; a wrong unit or a length mismatch raises.
- `test_near_term_engine.py`:
  - the ramp endpoints
  - lead-time weighting
  - a single hazard, severe on day 0 → `high`
  - cap at 100
  - level boundaries
  - `None` handling
  - validator failures (`max_points < high`, wrong weight count)
- `test_near_term_service.py`: cache hit inside the TTL and refetch after it (injected `now`, counting fake fetch); unavailable → error.
- `test_alerts.py`:
  - first check = baseline only
  - Δ under the threshold → none
  - Δ at or over the threshold → alert
  - a level crossing with a small Δ → alert
  - a decrease → alert with its direction
  - webhook skipped, posted (MockTransport asserts the Slack body), or failed (recorded, the check still succeeds)
  - demo: `demo=1`, `[DEMO]` prefix, snapshots unchanged
- `test_api.py`: 503 with no token configured, 401 with a bad or missing token, 200 with a good token, a demo body, `GET /api/alerts` shape and `limit` cap.
- `test_tools.py` / `test_agent.py`:
  - `near_term_risk` with a fake service
  - a `near_term` cite passes in-loop grounding through the scripted fake LLM
  - a wrong `near_term` number is rejected
- `test_prompts` / `test_config`: the prompt contains the 2016–2025 and 2026 rule; `config/near_term.yaml` validates.
- `evals/checks.py` `_check_grounding`: `near_term` cites are re-checked against `ctx.near_term` (the same 1 h cache, so the values match within a run) instead of the score run. Add a test.
- `@pytest.mark.live` smoke test: `fetch_forecast` for one hub.

## Task 11: Eval cases

Add these to `evals/cases.yaml`. Near-term numbers are live, so the checks are on tools and wording, not values.

| id | question | expect | check |
|---|---|---|---|
| `nearterm-houston` | What's the near-term weather risk for Houston this week? | answered | `near_term_risk` with `hub_ids: [houston]`; mention "forecast" |
| `nearterm-alerts-houston` | Any alerts for Houston? | answered | `near_term_risk` with `[houston]` |
| `nearterm-rank-week` | Which hubs face the highest weather risk in the next 7 days? | answered | `near_term_risk` (null or all hubs) |
| `nearterm-vs-historical` | Is Chicago risky this week, and how does that compare to its long-term exposure? | answered | any of `near_term_risk` and `explain_score`/`rank_hubs`; mention "relative" |
| `history-2026` | How many snow days did Denver have in 2026? | answered or needs_clarification | mention "2025"; no guessed number (grounding) |
| `history-this-year` | What percentage of days this year in Houston had heavy rain? | answered or needs_clarification | mention "2016" |

- Category: a new `near_term` category, plus `history-2026` in `core_examples`.
- Update the header comment.

## Task 12: Docs

- **README**:
  - new section "Near-term risk and alerts": method in brief, `skyrisk alerts check [--demo HUB]`, the demo walkthrough with curl, and the GitHub Actions setup (secret `ALERT_TOKEN`, var `SKYRISK_URL`, note that GitHub disables cron after 60 days without repo activity)
  - env table: `ALERT_TOKEN`, `ALERT_WEBHOOK_URL`
  - API table: `POST /api/alerts/check`, `GET /api/alerts`
  - eval category list
- **DESIGN.md**:
  - new §12 "Near-term risk and alerts": the formula, the config table, why the score is absolute and capped, lead-time weights, the alert rule, a mermaid alert-flow diagram (GH cron → endpoint → service → snapshots/alerts → webhook), demo semantics, and why the tool uses live+cache
  - §1 architecture: add the forecast path
  - §3 tables: add the two new tables
  - §6: regenerate the prompt text
  - §8 tradeoffs, new rows:
    - SQLite is ephemeral on Render free, so after a restart the first check only sets a baseline and alert history is lost (production: Postgres)
    - GH cron vs an in-process scheduler
    - absolute vs relative scale
    - live+cache vs snapshot for the tool
  - §9 assumptions: the history window is full calendar years 2016–2025, and 2026 is intentionally excluded (incomplete year; fixed end date keeps runs reproducible). Update Scope: live forecasts are no longer out of scope.
  - §7: eval results once they are run
- **agent-os/product**:
  - `roadmap.md`: mark #1 and #4 done, with a pointer to §12
  - `tech-stack.md`: replace APScheduler with the GitHub Actions cron and add the Open-Meteo forecast API

## Task 13: Verify, eval, commit

1. `uv run pytest`: everything green and offline.
2. Manual local run:
   - start `ALERT_TOKEN=dev uv run skyrisk serve`
   - `curl -X POST …/api/alerts/check -H "Authorization: Bearer dev"`: baseline only
   - run it again: no alerts (nothing changed)
   - run with `{"demo_hub":"chicago"}`: one DEMO alert
   - `GET /api/alerts` shows it, and the panel shows it with the DEMO badge
   - without the header → 401; with `ALERT_TOKEN` unset → 503
   - with a Slack test webhook (or a local `nc -l` catcher) set in `ALERT_WEBHOOK_URL`, confirm the payload
3. In chat: "near-term risk for Houston?" and "snow days in Denver in 2026?".
4. **Paid evals.** Ask before running. Estimates:
   - new cases `--category near_term --case 'history-*' --repeat 3 --report-name nearterm`: about 18 runs, ~$0.15
   - the prompt and classifier changed, so the full gate `skyrisk eval --repeat 3` must run: about 42 cases × 3 = 126 runs, ~$0.90
   - the outage path `--simulate-outage anthropic --repeat 3`: ~$0.03
   - record the results in DESIGN §7
5. Commit on `main` in logical commits (engine+client, alerts+API, tool+prompt, UI, workflow+docs). **Do not push** until the user says so, because a push redeploys Render.

## Critical files

- **New:**
  - `config/near_term.yaml`
  - `src/skyrisk/nearterm/{__init__,engine,service,alerts}.py`
  - `.github/workflows/near-term-check.yml`
  - tests and fixtures as above
- **Modified:**
  - `src/skyrisk/{config,db,cli}.py`
  - `ingest/{open_meteo,http}.py`
  - `agent/{tools,prompts,guardrails,factory}.py`
  - `api/app.py`
  - `api/static/{index.html,app.js,app.css}`
  - `evals/checks.py`, `evals/cases.yaml`
  - `render.yaml`, `.env.example`
  - `README.md`, `docs/DESIGN.md`
  - `agent-os/product/{roadmap,tech-stack}.md`
