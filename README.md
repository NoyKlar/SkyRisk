# SkyRisk

SkyRisk scores the weather risk of US logistics hubs and answers analysts' questions about it through a conversational agent. Every score comes from deterministic code over public data (Open-Meteo, FEMA NRI). The LLM only interprets the question, calls tools, and explains the results.

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
| `SKYRISK_PRIMARY_MODEL`, `SKYRISK_FALLBACK_MODEL`, `SKYRISK_CLASSIFIER_MODEL` | no | override the model ids in `config/agent.yaml` |

`.env` is gitignored. Ingest, score, show and the offline tests need no keys.

## 4. Load data and score

```bash
uv run skyrisk ingest        # fetch Open-Meteo 2016–2025 daily weather + FEMA NRI county data into data/skyrisk.db
uv run skyrisk score         # compute, store and print a ranked score run
uv run skyrisk show chicago  # explain one hub's latest scores, metric by metric
```

`ingest` caches everything in SQLite, so re-running it is instant. `--refresh` re-fetches the data, and `--hub ID` limits the run to one hub. \
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
| `GET` | `/api/health` | liveness check |
| `GET` | `/api/docs` | OpenAPI docs |

Omit `session_id` on the first message, and send the returned id back with follow-ups. \
Conversation memory lives in the server process. A session expires after 1 hour idle, at most 500 are kept, and all of them are lost on restart. If you send an expired id, a new session starts and the reply has `"session_reset": true`.

## 7. Tests

```bash
uv run pytest                # offline: scoring, ingest parsing, agent loop, guardrails, API, eval runner (no network, no keys)
uv run pytest -m live        # live smoke tests against Open-Meteo, FEMA, Claude and OpenAI (uses API credits)
```

## 8. Evals

`skyrisk eval` runs every case in [`evals/cases.yaml`](evals/cases.yaml) against the **real agent**. The cases cover core examples, normal questions, injections, off-topic requests, guardrail false-positive look-alikes, and Hebrew. Each run is checked for:
- status
- the expected tool and arguments
- required mentions and hub order
- grounding: every cited score must match the latest score run in the DB
- which guardrail layer decided

```bash
uv run skyrisk eval                        # every case once
uv run skyrisk eval --repeat 3             # 3 runs per case; a case passes only if all 3 pass (flaky cases are flagged)
uv run skyrisk eval --category hebrew      # filter by category (repeatable)
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

**Cost.** Deterministic injection cases make no model calls. Every other run makes one Haiku classifier call. Runs that reach the agent also make roughly 2–4 Sonnet calls: tool rounds plus the final answer. A full `--repeat 3` run costs about 85 Haiku calls and 100–250 Sonnet calls.

## Project layout

```
config/        hubs, scoring weights/thresholds (versioned), agent/model settings
evals/         cases.yaml + results/latest.{md,json}
src/skyrisk/   ingest/ (API clients), scoring/ (pure engine), agent/ (tools, guardrails, providers, loop),
               api/ (FastAPI + static chat page), evals/ (runner, checks, report), cli.py
tests/         offline tests with recorded fixtures and a scripted fake LLM provider
docs/          DESIGN.md
agent-os/      product mission/roadmap, standards, and one spec folder per feature
```
