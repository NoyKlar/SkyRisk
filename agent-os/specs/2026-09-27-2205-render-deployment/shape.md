# Render Deployment + Chat Rate Limits — Shaping Notes

## Scope

Roadmap Phase 2, item 6. In the user's words:

1. **render.yaml blueprint:**
   - Python 3.12, free tier, single web service.
   - The build installs deps with uv and runs `skyrisk ingest` + `skyrisk score`, so the SQLite DB ships with the deploy. The free-tier disk is ephemeral.
   - Start command: `skyrisk serve --host 0.0.0.0 --port $PORT`.
   - Health check: `/api/health`.
2. **Secrets:** ANTHROPIC_API_KEY and OPENAI_API_KEY are set in the Render dashboard (`sync: false` in render.yaml) and never committed.
3. **Protect API credits on a public URL:**
   - A per-IP rate limit on POST /api/chat (20 questions/hour).
   - A global daily cap.
   - A friendly message when either is exceeded.
   - Configurable in agent.yaml, with tests.
4. **README:**
   - A "Deployment" section: Render steps, env vars, a cold start of about 30–60 s after idle, and the data rebuilt on each deploy.
   - An "Adding or removing a hub" section: edit config/hubs.yaml, run ingest + score. Scores are relative, so every hub may shift, and evals may need updating.
5. **DESIGN.md:** deployment architecture and tradeoffs (free tier, cold starts, ephemeral disk, rate limiting).

## Decisions

- **Render facts** (checked in the Render docs on 2026-09-27):
  - uv is used automatically when `uv.lock` is present.
  - Python comes from `.python-version` (3.12).
  - Free services spin down after 15 minutes idle.
  - The disk is ephemeral and there are no persistent disks.
  - The service must bind `0.0.0.0:$PORT`.
- **DB built at build time:**
  - Ingest needs network access but no keys, and takes about 2+ minutes.
  - A failed ingest fails the build, and Render keeps the previous deploy.
- **Limits:**
  - 20 questions per IP per hour, as a sliding window.
  - **100 questions per day** globally, resetting at 00:00 UTC.
  - Both are set in `agent.yaml` under `rate_limit:`.
  - They are counted in memory, so the counters reset on a restart or spin-down.
- **Client IP:** the first `X-Forwarded-For` entry, else the socket peer.
  - The header can be spoofed, so the per-IP limit only provides fairness. The global cap is the credit guard.
  - The README recommends provider spend limits.
- **What counts:** every validated chat request counts, including refusals (they still make a classifier call).
  - A per-IP rejection doesn't use up the global quota.
- **Roadmap:** Deployment stays in Phase 2, marked done.
- **Wording:** the motivation is written as "a deployed app makes the demo easier to access". Older docs that referred to the original brief were reworded to match: the roadmap entry and the agent-layer spec now say "headline example questions".
- **Spec numbering:** Spec 6 (spec 5 is the deferred Jev classifier).

## Context

- **Visuals:** None.
- **References:**
  - `SessionStore`: the injectable clock and lock pattern.
  - `tests/test_api.py::_client`.
  - `cli._serve` together with `factory.build_agent`.
- **Product alignment:** roadmap Phase 2 #6 (stretch goal).

## Standards Applied

- testing/injectable-side-effects: the limiter takes an injected `now` clock, and the tests never monkeypatch time.
- testing/no-live-network: the rate-limit and API tests are offline. The deploy is verified manually.
