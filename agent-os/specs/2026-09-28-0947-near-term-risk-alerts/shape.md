# Near-Term Risk Alerts — Shaping Notes

## Scope

Roadmap Phase 2, items 1 (scheduled risk alerts) and 4 (live forecast layer). The user's requirements:

1. Add a second, separate score per hub: a **near-term risk** score (0–100), computed deterministically from the Open-Meteo 7-day forecast (snowfall, wind gusts, precipitation, extreme heat). Thresholds and weights live in versioned YAML config. The historical exposure score is unchanged. Historical says where to invest; near-term says what's coming this week.
2. **Alerting:**
   - Store a snapshot of each hub's near-term score in SQLite.
   - `POST /api/alerts/check` recomputes every score and compares it with the last snapshot. It creates an alert when a hub's score changes by more than a configurable threshold or crosses a level (low/medium/high).
   - Alerts are saved and optionally POSTed to an outgoing webhook: `ALERT_WEBHOOK_URL`, Slack-compatible JSON, skipped if unset.
   - The endpoint is protected by a secret token (`ALERT_TOKEN`), because the site is public.
3. **Scheduling:** a GitHub Actions cron workflow calls the endpoint once a day. Render's free tier sleeps, so there is no in-process scheduler.
4. **Demo mode:** simulates a score change so an alert can be shown live.
5. A `GET` endpoint for recent alerts, shown in a small panel in the chat UI.
6. An agent tool, so the agent can answer "what's the near-term risk / any alerts for Houston?".
7. The system prompt states that historical data covers the full calendar years 2016–2025 only. For 2026 the agent must say so instead of guessing.
8. Tests use recorded forecast fixtures and no live network. Add eval cases for near-term questions and for the "what about 2026" case.
9. README and design doc: the near-term methodology, the alert flow, and the assumption that the historical window is 2016–2025 with 2026 intentionally excluded.
10. A known tradeoff to document: SQLite on Render's free tier is ephemeral, so after a restart the first check only sets a new baseline. Production would use Postgres.

## Decisions

- **The agent tool reads a live forecast with a 1 h in-memory cache.** It never writes alert snapshots, so chat traffic can't move the alert baseline, and it works right after a restart. The eval grounding check re-checks `near_term` cites through the same cached service.
- **Score math uses capped points.**
  - Each hazard has a linear ramp between a `watch` and a `severe` threshold, multiplied by a lead-time weight (day 0 = 1.0, decaying to 0.6).
  - The maximum over the 7 days, times `max_points`, gives the hazard's points; the score is the sum, capped at 100.
  - Levels: <35 low, 35–65 medium, ≥65 high.
  - The user's rule: a single hazard at full severity on day 1 must reach "high" on its own, so one blizzard can trigger a high alert. The config validator enforces `max_points ≥ levels.high` for every hazard.
- **The near-term score is absolute, not relative across hubs.** Alerts track change over time, and relative scaling would make one hub's storm move every other hub's score. The prompt forbids adding, averaging or ranking it together with historical scores.
- **Alert rule:** fire when `|Δ| ≥ change_threshold` (20) or on any level crossing, in either direction. No previous snapshot means baseline only. Each check sends one webhook message.
- **Demo:** a scripted storm is merged into one hub's real forecast and scored by the same engine. It is compared with that hub's live score, flagged `demo`, prefixed `[DEMO]`, and writes no snapshots, so the real baseline is untouched. It is triggered through the token-protected endpoint, the CLI, or `workflow_dispatch`; the public UI has no demo button.
- **Auth:** `Authorization: Bearer <ALERT_TOKEN>`, compared with `hmac.compare_digest`. If the token is unset the endpoint answers `503` (disabled), never open.
- **Scheduling:** a GitHub Actions cron (`0 11 * * *`) whose curl retries ride out the cold start. This replaces APScheduler from `tech-stack.md`.
- **Repo rules:** commit on `main`, no push without an explicit OK (Render auto-deploys), and give a cost estimate and wait for a go-ahead before paid eval runs.

## Context

- **Visuals:** None. The alerts list goes in the existing hubs side panel.
- **References:** `ingest/open_meteo.py`, `ingest/http.py`, `agent/tools.py`, `config.py`, `evals/checks.py`, `api/app.py`, `api/static/` (see references.md)
- **Product alignment:** roadmap Phase 2 #1 + #4. APScheduler is replaced by a GitHub Actions cron because the free tier sleeps.

## Standards Applied

- backend/external-api-clients: the new `parse_forecast` / `fetch_forecast`, and a `post` helper for webhooks
- testing/injectable-side-effects: `fetch`, `now`, `notify` and `client` are parameters; no monkeypatching
- testing/no-live-network: recorded forecast fixtures, MockTransport for the webhook, one `-m live` smoke test
