# Agent Layer — Shaping Notes

## Scope

Roadmap Phase 1, items 4–7 and 11:
- a tool-calling agent over the scoring core (rank, compare, explain, weather stats)
- an enforced Pydantic JSON schema between the LLM and the code
- layered guardrails
- a primary LLM plus a fallback LLM
- clear statements of assumptions and uncertainty

The interface is a CLI REPL (`skyrisk chat`). The FastAPI endpoint (item 8) and the eval runner (item 10) are later specs.

## Decisions

- **Primary model: Claude Sonnet 5 (`claude-sonnet-5`)**, as named in tech-stack.md. It is used through the Anthropic SDK Messages API with adaptive thinking, strict tools, and `output_config.format` for the final answer.
- **Fallback model: OpenAI `gpt-6-luna` through the Responses API.** Checked against OpenAI's docs on 2026-09-27:
  - It is OpenAI's cost-efficient tier: $0.10 / $0.50 per 1M input/output tokens, 1.05M context, 128K max output.
  - It supports function calling, structured outputs and streaming.
  - Chat Completions allows function calling only with `reasoning_effort: none`, so we use the Responses API.
  - A different provider means an Anthropic outage doesn't take the agent down.
- **Fallback trigger:** only provider-availability errors (connection errors, 429 after SDK retries, 5xx). A 4xx is a bug, so it is raised and does not fall back. On failure, the whole turn is replayed on the fallback provider from neutral history.
- **Provider-neutral core:** the agent loop only knows the `LLMProvider` protocol and neutral message types. Each adapter's translation code is pure and unit-tested.
- **The LLM never produces numbers on its own:**
  - Every score and stat comes from a tool.
  - `AgentAnswer.scores_cited` is checked against the tool results from the same turn (tolerance ±0.05).
  - A mismatch triggers one regeneration, then a safe fallback answer.
- **The headline example questions map to deterministic tools:**
  - "Midwest … winter" → `rank_hubs(hazard="winter", region="Midwest")`.
  - "Denver last year snowfall %" → `weather_stat(..., year=2025)`. **"Last year" means 2025**, the latest full year in the 2016–2025 window, not the calendar year before today. The agent states this assumption and the snow-day threshold (≥ 1 cm).
- **Guardrails, in layers:**
  1. Deterministic input checks (length, control characters, known injection patterns).
  2. A Haiku 4.5 classifier (`claude-haiku-4-5`) behind a `Classifier` interface, so it can be swapped for Jev later. **If it fails or times out, it is skipped. It never blocks the user.**
  3. A scoped system prompt, with tool results passed as data.
  4. An output grounding check.
- **Classifier false positives are tracked:** legitimate look-alike questions ("What's the *system* for scoring…", "Ignore Phoenix — …") are part of `evals/cases.yaml`.
- **Memory:** the in-process `Conversation` keeps neutral history, capped at N turns. The API spec will reuse it.

## Context

- **Visuals:** None
- **References:** the spec-1 code (scoring metrics, db loaders, CLI `show` queries). See references.md.
- **Product alignment:**
  - The mission says "the LLM never invents the scores", which is enforced by tools plus grounding.
  - Scores are relative, and NRI data is county-level. Tool results include these caveats and the answer schema requires a limitations section.
  - Guardrails, evals and the fallback model match the "production-ready" differentiator.

## Standards Applied

- backend/external-api-clients: LLM adapters use pure translate/parse functions with a thin call, and fail loud on schema mismatch.
- testing/no-live-network: scripted fake providers in pytest; real Claude/OpenAI calls only under `-m live`.
- testing/injectable-side-effects: providers, classifier, clock and log are injected.
