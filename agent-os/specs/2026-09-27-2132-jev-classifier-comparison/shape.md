> **Status: IN PROGRESS (re-opened 2026-09-27 with an official TypeSafe key in `JEV_API_KEY`).** The original plan below is being implemented as written, with these changes: the key-verification call doubles as the recorded fixture, the default classifier stays `haiku` (Jev is the fallback), and nothing is pushed without the user's OK, because Render auto-deploys on push.

# Jev Classifier + Measured Comparison — Shaping Notes

## Scope

Roadmap Phase 2, item 3. The user's requirements:

1. Add a `JevClassifier` (TypeSafe AI's Jev decision model, endpoint `POST /v1/systemone`) behind the existing `Classifier` interface, with no changes to the agent. Verify the request/response shape and pricing from Jev's official docs. API key from `JEV_API_KEY` in `.env`.
2. Ask Jev two typed questions in one call: an `in_scope` noul question (the yes/no criteria explicitly exclude farming, gardening, recipes and general weather trivia) and an `injection` noul question. Map the probabilities to in_scope / off_topic / injection with configurable thresholds.
3. Uncertainty band: when the in_scope probability falls inside a middle band (e.g. 0.4–0.6), escalate that question to the Haiku classifier. Jev alone decides the confident cases.
4. Select the classifier in config (`haiku | jev`) with a fallback chain. If the primary fails or times out, use the other. If both fail, skip them and rely on the deterministic layers (never block the user).
5. Compare the classifiers: run the guardrail eval categories (injection, off_topic, false_positive, hebrew) with each one at `--repeat 3`. Report accuracy (false-positive and miss rates), classifier latency and estimated cost side by side.
6. Choose the default classifier from the measured results. Document the comparison, the decision and the reasoning in DESIGN.md.
7. Before any paid run, give the estimated number of API calls and wait for the user's go-ahead.

## Decisions

- **Direct TypeSafe API**: `https://api.typesafe.ai/v1/systemone`, with the model pinned to `jev-1.13.0`. The user's `typesafe/jev-1.13` is the OpenRouter slug. Base URL and model are configurable.
- Verified request/response shape (docs.typesafe.ai, 2026-09-27): noul questions use `instructions` + `criteria: {true, false}`. Answers come back as `answers.<key>.noul` (a probability), with `usage.input_tokens`. Pricing is $0.042/M input tokens and output is free. English is Jev's primary training language, so the Hebrew results are reported separately.
- **Two comparison modes**:
  - A classifier-only benchmark (`skyrisk eval-classifier`, no Sonnet) makes the decision.
  - A full-agent `skyrisk eval` over the guardrail categories then confirms the chosen default.
- **Decision order**:
  1. False-positive rate (blocking a real user is the worst outcome)
  2. Miss rate (regex, the scoped prompt and grounding back it up)
  3. Latency and cost
- **Fail open everywhere**:
  - A band case with no working escalation is treated as in_scope.
  - If every classifier fails, the question is skipped with the "classifier skipped" warning.
- **Cost gate**: every paid run is preceded by a dry-run call estimate and waits for the user's go-ahead.
- `agent/core.py` stays unchanged. `Verdict` gains `decided_by` and `usage` for accounting only.

## Context

- **Visuals:** None
- **References:** `HaikuClassifier`/`safe_classify` (guardrails.py), `ingest/http.get_json`, `evals/runner.py` + `report.py`, `agent/factory.py`, `tests/test_ingest.py`
- **Product alignment:** Roadmap Phase 2 #3 ("Jev classifier for fast intent routing and guardrail checks, optional, if it fits"). The measured comparison decides whether it fits.

## Standards Applied

- backend/external-api-clients: build/parse split for the Jev client, fail-loud Pydantic parsing, pinned field names, and HTTP through the shared retry helper
- testing/injectable-side-effects: the httpx client, clock and log are injected into the classifier and the benchmark
- testing/no-live-network: a recorded Jev fixture plus MockTransport; the live smoke test runs only under `-m live`
