# SkyRisk eval results

**3/3 cases passed** (3 runs per case; a case passes only if every run passes)

- Run at: 2026-09-28T11:28:50+03:00
- Primary model: `anthropic:claude-sonnet-5`, fallback: `openai:gpt-6-luna`, classifier: `anthropic:claude-haiku-4-5`
- Score run: 7 (scoring config v1.2)

## By category

| Category | Cases passed | Runs passed |
|---|---|---|
| core_examples | 3/3 | 9/9 (100%) |

## Guardrails

| Metric | Value |
|---|---|
| False-positive rate (in-scope runs refused) | 0/9 (0%) |
| False positives by layer | none |
| Miss rate (off-topic / injection runs answered) | 0/0 (n/a) |
| Error replies (unverified answer, unusable response, no provider) | 0 |
| Grounding failures (cited score differs from the DB) | 0 |

## Failures

None.

## Flaky cases

None.

## Latency

All runs: p50 10.3s, p95 13.1s, max 13.1s.

| Case | Status (per run) | Mean latency | Tools |
|---|---|---|---|
| `history-2026` | answered, answered, answered | 11.5s | weather_stat |
| `history-this-year` | answered, answered, answered | 11.4s | weather_stat |
| `history-2026-score` | answered, answered, answered | 9.5s | rank_hubs, weather_stat |

## Models used

| Served by | Runs |
|---|---|
| anthropic:claude-sonnet-5 | 9 |

## Cost

Estimated **$0.083** for all runs ($0.0092 per run), from recorded token usage and `src/skyrisk/evals/pricing.py`. Model calls: claude-haiku-4-5: 9, claude-sonnet-5: 18.
