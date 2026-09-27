# SkyRisk eval results

**3/3 cases passed** (3 runs per case; a case passes only if every run passes)

- Run at: 2026-09-28T00:59:37+03:00
- Primary model: `anthropic:claude-sonnet-5`, fallback: `openai:gpt-6-luna`, classifier: `anthropic:claude-haiku-4-5`
- Score run: 7 (scoring config v1.2)

## By category

| Category | Cases passed | Runs passed |
|---|---|---|
| follow_up | 3/3 | 9/9 (100%) |

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

All runs: p50 13.9s, p95 24.3s, max 24.3s.

| Case | Status (per run) | Mean latency | Tools |
|---|---|---|---|
| `followup-dallas-heat` | answered, answered, answered | 13.6s | explain_score, weather_stat |
| `followup-midwest-snow-last-year` | answered, answered, answered | 11.6s | weather_stat, list_hubs |
| `he-followup-dallas-heat` | answered, answered, answered | 20.9s | explain_score, weather_stat |

## Models used

| Served by | Runs |
|---|---|
| anthropic:claude-sonnet-5 | 9 |

## Cost

Estimated **$0.336** for all runs ($0.0373 per run), from recorded token usage and `src/skyrisk/evals/pricing.py`. Model calls: claude-haiku-4-5: 18, claude-sonnet-5: 50.
