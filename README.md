# SkyRisk

SkyRisk scores the weather risk of US logistics hubs and answers analysts' questions about it through a conversational agent. It has two separate scores per hub:
- **historical exposure**: where to invest, from 2016–2025 history and FEMA NRI, relative across hubs
- **near-term risk**: what's coming this week, from the 7-day forecast, absolute 0–100 with low/medium/high alerts

Every score comes from deterministic code over public data (Open-Meteo, FEMA NRI). The LLM only interprets the question, calls tools, and explains the results.

Architecture, methodology and tradeoffs are in **[docs/DESIGN.md](docs/DESIGN.md)**.

## 1. Prerequisites

- Python 3.12
- [uv](https://docs.astral.sh/uv/) (`curl -LsSf https://astral.sh/uv/install.sh | sh`)
- An Anthropic API key, needed for the chat, the API and the evals. An OpenAI API key is optional; it enables the fallback model.

## 2. Install

```bash
git clone <repo-url> skyrisk && cd skyrisk
uv sync                      # creates .venv and installs dependencies from uv.lock
```

## 3. Configure `.env`

```bash
cp .env.example .env
```

| Variable | Required | Used for |
|---|---|---|
| `ANTHROPIC_API_KEY` | yes (for the agent) | primary model (Claude Sonnet 5) and the Haiku guardrail classifier |
| `OPENAI_API_KEY` | no | fallback model (`gpt-6-luna`). Without it, the agent runs with no fallback. |
| `JEV_API_KEY` | no | TypeSafe Jev classifier. Not used by default; only for re-testing with `SKYRISK_CLASSIFIER=jev` or `skyrisk eval-classifier`. |
| `SKYRISK_PRIMARY_MODEL`, `SKYRISK_FALLBACK_MODEL`, `SKYRISK_CLASSIFIER_MODEL` | no | override the model ids in `config/agent.yaml` (`SKYRISK_CLASSIFIER_MODEL` is the Haiku classifier model) |
| `SKYRISK_CLASSIFIER` | no | `haiku` (default: Haiku alone, skipped if it fails) or `jev` (opt-in re-testing: Jev first, Haiku as its fallback) |
| `ALERT_TOKEN` | no | secret that guards `POST /api/alerts/check`. Unset = the check is disabled (`503`). |
| `ALERT_WEBHOOK_URL` | no | Slack-compatible incoming webhook for alerts. Unset = alerts are only stored and listed. |

`.env` is gitignored. Ingest, score, show and the offline tests need no keys.

## 4. Load data and score

```bash
uv run skyrisk ingest        # fetch Open-Meteo daily weather for the full years 2016–2025 + FEMA NRI county data into data/skyrisk.db
uv run skyrisk score         # compute, store and print a ranked score run
uv run skyrisk show chicago  # explain one hub's latest scores, metric by metric
```

The historical window is fixed at full calendar years **2016–2025**. 2026 is intentionally excluded because it is not a complete year, and the agent says so instead of guessing (the near-term forecast covers "this week"). `ingest` caches everything in SQLite, so re-running it is instant. `--refresh` re-fetches the data, and `--hub ID` limits the run to one hub. \
Hubs, weights and thresholds live in `config/hubs.yaml` and `config/scoring.yaml`.

## 5. Chat in the terminal

```bash
uv run skyrisk chat          # interactive; /reset clears memory, /quit exits
uv run skyrisk chat -q "Which hubs in the Midwest are most exposed to winter disruption?"
```

## 6. Web chat and JSON API

```bash
uv run skyrisk serve                              # http://127.0.0.1:8000
uv run skyrisk serve --host 0.0.0.0 --port 8080   # or set HOST / PORT
```

The page at `/` is plain HTML/CSS/JS served by the same process. It talks only to the JSON API:

| Method | Path | Purpose |
|---|---|---|
| `POST` | `/api/chat` | `{"message", "session_id"?}` → answer, limitations, scores cited, model and tools used, `session_id` |
| `DELETE` | `/api/sessions/{id}` | clear a conversation's memory |
| `GET` | `/api/hubs` | the hubs you can ask about (id, name, city, state, region) |
| `POST` | `/api/alerts/check` | recompute near-term scores and create alerts. Needs `Authorization: Bearer $ALERT_TOKEN`; body `{"demo_hub": "chicago"}` for a demo (see [section 7](#7-near-term-risk-and-alerts)) |
| `GET` | `/api/alerts` | recent alerts (`?limit=` up to 50, `?hub_id=`) and the time of the last check; shown in the chat page's side panel |
| `GET` | `/api/health` | liveness check |
| `GET` | `/api/docs` | OpenAPI docs |

`POST /api/chat` is rate limited under `rate_limit:` in `config/agent.yaml`: 20 questions per IP per hour and 100 per day in total. Past a limit it returns `429` with a friendly `detail` message and a `Retry-After` header (see [Deployment](#10-deployment-render)).

Omit `session_id` on the first message, and send the returned id back with follow-ups. \
Conversation memory lives in the server process. A session expires after 1 hour idle, at most 500 are kept, and all of them are lost on restart. If you send an expired id, a new session starts and the reply has `"session_reset": true`.

## 7. Near-term risk and alerts

**The score.** For each hub, `skyrisk` fetches the Open-Meteo 7-day forecast and scores four hazards: snowfall, wind gusts, precipitation and extreme heat.
- Each day's value ramps from 0 at a `watch` threshold to full severity at a `severe` threshold, weighted down for later days.
- The worst day per hazard earns up to `max_points`, and the score is the sum, capped at 100: **low** < 35 ≤ **medium** < 65 ≤ **high**.
- One hazard at full severity tomorrow reaches "high" on its own.
- Thresholds and weights live in [`config/near_term.yaml`](config/near_term.yaml) (versioned). Details are in `docs/DESIGN.md` §12.
- It is an absolute forecast severity, **not** comparable with the relative historical scores.

**Alerts.** A check scores every hub, compares each score with the hub's last stored snapshot, and creates an alert when the score moves by 20 or more (`alerts.change_threshold`) or the level changes, in either direction.
- New alerts are stored in SQLite and sent as **one** Slack-compatible message to `ALERT_WEBHOOK_URL`, if it is set.
- A hub with no snapshot yet, e.g. after a restart, only gets a baseline.

```bash
uv run skyrisk alerts check                 # run a check locally (no token needed)
uv run skyrisk alerts check --demo chicago  # demo: simulate a storm for Chicago
uv run skyrisk alerts list                  # recent alerts
```

**Demo mode.** A demo check merges a scripted storm (`demo:` in `config/near_term.yaml`: 30 cm snow and 95 km/h gusts tomorrow) into one hub's real forecast and scores it with the same engine.
- The alert compares that result with the hub's live score, is marked `demo` (`[DEMO]` in the webhook, a DEMO badge in the page), and saves **no** snapshot, so the next real check is unaffected.
- On the deployed service:
  ```bash
  curl -X POST https://skyrisk.onrender.com/api/alerts/check \
    -H "Authorization: Bearer $ALERT_TOKEN" -H "Content-Type: application/json" -d '{"demo_hub": "chicago"}'
  ```
- Or run the GitHub workflow manually with a `demo_hub` input. The alert then appears in the page's "Recent alerts" panel.

**Daily schedule (GitHub Actions).** Render's free tier sleeps, so there is no in-process scheduler. [`.github/workflows/near-term-check.yml`](.github/workflows/near-term-check.yml) calls the endpoint at 11:00 UTC every day, and its retries ride out the cold start. Setup:
1. Generate a token: `python -c "import secrets; print(secrets.token_urlsafe(32))"`.
2. Set it as `ALERT_TOKEN` in the Render dashboard (and optionally `ALERT_WEBHOOK_URL`).
3. In GitHub → Settings → Secrets and variables → Actions:
   - add the secret `ALERT_TOKEN` (the same value)
   - optionally, the variable `SKYRISK_URL` (default `https://skyrisk.onrender.com`)
4. Run the workflow once from the Actions tab to set the first baseline.

GitHub pauses scheduled workflows after 60 days without repository activity; re-enable it in the Actions tab.

**Known limitation: ephemeral storage.** On Render's free tier the SQLite file is rebuilt on every deploy, and anything written at runtime is lost on a restart or spin-down. After a restart:
- the first check only sets a new baseline, so a change that happened across the restart is not alerted
- the alert list starts empty

Production would keep snapshots and alerts in Postgres (`docs/DESIGN.md` §8, §12).

**In chat.** The agent's `near_term_risk` tool answers "What's the near-term risk for Houston?" and "Any alerts for Houston?". It fetches the live forecast, cached for 1 hour. It never writes snapshots, so chat traffic can't move the alert baseline.

## 8. Tests

```bash
uv run pytest                # offline: scoring, ingest parsing, near-term engine + alerts, agent loop, guardrails, API, eval runner (no network, no keys)
uv run pytest -m live        # live smoke tests against Open-Meteo (archive + forecast), FEMA, Claude and OpenAI (uses API credits)
```

## 9. Evals

`skyrisk eval` runs every case in [`evals/cases.yaml`](evals/cases.yaml) against the **real agent**. The cases cover core examples, normal questions, follow-ups, near-term forecast and alert questions, injections, off-topic requests, guardrail false-positive look-alikes, and Hebrew. Each run is checked for:
- status
- the expected tool and arguments
- required mentions and hub order
- grounding: every cited score must match the latest score run in the DB
- which guardrail layer decided

```bash
uv run skyrisk eval                        # every case once
uv run skyrisk eval --repeat 3             # 3 runs per case; a case passes only if all 3 pass (flaky cases are flagged)
uv run skyrisk eval --category hebrew      # filter by category (repeatable)
uv run skyrisk eval --category follow_up --report-name followup   # a subset, reported to followup-latest.* instead of latest.*
uv run skyrisk eval --case 'offtopic-*'    # filter by case id glob (repeatable)
```

**Reports.** Each run writes `evals/results/<timestamp>.md` and `.json`, which are gitignored. It also overwrites `evals/results/latest.md` and `latest.json`, which are committed. The report shows:
- the pass rate per category
- guardrail false-positive and miss rates
- every failure with its reasons
- flaky cases
- latency (p50/p95/max)
- the models that served each run

**Exit codes: use it as a gate.**

| Code | Meaning |
|---|---|
| `0` | every case passed |
| `1` | at least one case failed |
| `2` | setup error (missing API key, no score run, no matching cases) |

Run `uv run skyrisk eval --repeat 3` before merging any change to the system prompt, tools, guardrails or model ids, and treat a non-zero exit as a blocker. The command can run unchanged as a CI step, given the API key as a secret and a scored database.

**Classifier benchmark.** `skyrisk eval-classifier` calls the guardrail classifiers directly, with no agent model, on the `injection`, `off_topic`, `false_positive` and `hebrew` cases. It compares them side by side: false-positive and miss rates, per-category accuracy, escalations, latency, and cost per 1k questions. Measured result: Jev was much faster and cheaper, but it wrongly refused 43% of legitimate look-alike questions. So the default is Haiku alone, with no Jev fallback: if Haiku fails, the check is skipped, which never blocks a real user. Details are in `docs/DESIGN.md` §7.

**Multi-turn cases.** A case may list `prior_turns`. They are asked first in the same conversation, and only the last reply is checked (see `evals/cases.yaml`). The `follow_up` category (3 cases, one in Hebrew) tests conversation memory. The latest run passed 9/9 for $0.34 (`docs/DESIGN.md` §7).

**Outage path.** `uv run skyrisk eval --simulate-outage anthropic --repeat 3` makes every Anthropic model fail on every call. The agent then runs exactly as it would during an Anthropic outage: the classifier is skipped and OpenAI answers. It needs only `OPENAI_API_KEY`. Reports go to `evals/results/anthropic-outage-*` (`anthropic-outage-latest.*` is committed). The full set costs about $0.02 with `gpt-6-luna`. The latest result is 33/33 cases on both the normal and the outage path (before/after in `docs/DESIGN.md` §7). `skyrisk chat --simulate-outage anthropic` does the same for manual testing.

```bash
uv run skyrisk eval-classifier --repeat 3 --dry-run   # print the call and cost estimate, make no calls
uv run skyrisk eval-classifier --repeat 3             # both classifiers (needs ANTHROPIC_API_KEY and JEV_API_KEY)
uv run skyrisk eval-classifier --classifier jev --category normal
```

It writes `evals/results/classifier-<timestamp>.*` (gitignored) and `classifier-latest.md`/`.json` (committed). A `--repeat 3` run of both classifiers makes 126–189 calls and costs about $0.05–0.11.

**Cost.** Deterministic injection cases make no model calls. Every other run makes one Haiku classifier call. Runs that reach the agent also make roughly 2–4 Sonnet calls: tool rounds plus the final answer. A full `--repeat 3` run costs about 85 Haiku calls and 100–250 Sonnet calls.

## 10. Deployment (Render)

[`render.yaml`](render.yaml) is a Render Blueprint for a single **free** web service.

**Steps**
1. Push the repo to GitHub.
2. In Render, choose **New → Blueprint** and select the repo.
3. When prompted, enter `ANTHROPIC_API_KEY` (required), `OPENAI_API_KEY` (optional, for the fallback), and `ALERT_TOKEN` / `ALERT_WEBHOOK_URL` (optional, for alerts; see [section 7](#7-near-term-risk-and-alerts)).
   - These are `sync: false` in `render.yaml`, so they live only in the Render dashboard and are never committed.
   - The `SKYRISK_*_MODEL` overrides from [section 3](#3-configure-env) can be added there too.
4. Wait for the first build, then open the service URL (the live demo is at <https://skyrisk.onrender.com>).
   - Render checks `/api/health`.
   - Every push to the default branch redeploys.

**What the build does**
- The free-tier disk is ephemeral, so the build creates the database itself: `uv sync --frozen --no-dev`, then `skyrisk ingest`, then `skyrisk score`. The SQLite file ships with the deploy.
- The data is rebuilt from Open-Meteo and FEMA on every deploy.
  - This takes about 5–8 minutes, mostly paced Open-Meteo requests, and it counts against Render's monthly build minutes.
  - If ingest fails (e.g. an upstream API is down), the build fails and Render keeps serving the previous deploy.
- The service starts with `uv run --no-sync skyrisk serve --host 0.0.0.0 --port $PORT`. Python 3.12 comes from `.python-version`.

**Free-tier behaviour**
- After 15 minutes without traffic the service spins down. The next request waits through a **cold start of about 30–60 s**.
- A restart or spin-down clears everything held in memory (chat sessions, where the page starts a new conversation and says so, and the rate-limit counters) and everything written to disk at runtime (near-term snapshots and alerts).

**Protecting API credits**
- The public URL lets anyone reach paid model calls. Each question costs one Haiku call, plus roughly 2–4 Sonnet calls if it reaches the agent.
- `POST /api/chat` therefore has two limits. Change them in `config/agent.yaml` and redeploy:
  ```yaml
  rate_limit:
    per_ip_per_hour: 20   # sliding one-hour window per client IP (from X-Forwarded-For)
    global_per_day: 100   # all clients together; resets at 00:00 UTC
  ```
- Every validated question counts, including refused ones, because they still make a classifier call.
- The client IP header can be spoofed, so treat the per-IP limit as fairness and the daily cap as the real bound.
- The counters are in memory, so a restart resets them. Also set a monthly spend limit in the Anthropic (and OpenAI) console.

## 11. Adding or removing a hub

1. Edit [`config/hubs.yaml`](config/hubs.yaml). Each entry needs:
   - `id`: lowercase letters, digits and hyphens; must be unique
   - `name`, `city`
   - `state`: 2-letter code
   - `lat`, `lon`: must fall within the contiguous US
   - `region`: `Northeast`, `Southeast`, `Midwest`, `South` or `West`
   `uv run pytest tests/test_config.py` validates the file.
2. Fetch data for new hubs and rescore:
   ```bash
   uv run skyrisk ingest     # fetches only hubs that aren't cached yet
   uv run skyrisk score
   ```
   A removed hub's old rows stay in the local DB but are ignored, because scoring, tools and the system prompt read `config/hubs.yaml`. A Render build always starts from an empty DB.
3. **Scores are relative.** Every metric is min-max scaled across the hubs (least exposed = 0, most exposed = 100) before weighting. Adding or removing any hub can shift every hub's scores and ranks, not just the new one's.
4. **No code changes are needed.** Everything that depends on the hub count reads it from `config/hubs.yaml`:
   - the system prompt and the tools' relative-score caveat
   - `rank_hubs` / `weather_stat`, which have no fixed upper bound on the number of hubs
   - the chat page's welcome line (via `GET /api/hubs`)
   - the tests
   For example, adding Seattle as a 14th hub worked end to end:
   - ingest took about 3 s for the one new hub
   - the page showed "14 US hubs"
   - the agent ranked all 14 and answered a Seattle snow question
   - every other hub's scores shifted slightly
5. **Evals.** Cases in `evals/cases.yaml` that name hubs, regions or an expected ranking order may need updating. For example, `unknown-hub` asks about Seattle and expects `needs_clarification`. It fails once Seattle is a real hub, so change it to a city you don't serve. Rerun `uv run skyrisk eval --repeat 3`.
6. On Render, push the change. The next deploy ingests and scores the new hub list.

## Project layout

```
config/        hubs, scoring weights/thresholds (versioned), near-term forecast thresholds (versioned), agent/model settings
evals/         cases.yaml + results/latest.{md,json}, results/classifier-latest.{md,json}
src/skyrisk/   ingest/ (API clients), scoring/ (pure engine), nearterm/ (forecast engine, cached service, alert check),
               agent/ (tools, guardrails, Jev classifier, providers, loop),
               api/ (FastAPI + static chat page), evals/ (runner, checks, report, classifier benchmark), cli.py
tests/         offline tests with recorded fixtures and a scripted fake LLM provider
docs/          DESIGN.md
render.yaml    Render Blueprint (build: ingest + score; start: serve)
.github/       workflows/near-term-check.yml: daily alert check (cron)
agent-os/      product mission/roadmap, standards, and one spec folder per feature
```
