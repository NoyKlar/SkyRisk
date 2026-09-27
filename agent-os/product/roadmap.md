# Product Roadmap

## Phase 1: MVP

1. **Hub registry** — ~10–15 fixed US hubs (name, city, lat/lon, region) in a local config/DB
2. **Data ingestion from public APIs**, cached locally:
   - Open-Meteo historical weather (snow, extreme heat/cold, heavy rain, wind)
   - FEMA National Risk Index (hurricane, flood, tornado, winter storm hazard)
3. **Deterministic scoring engine** — per-hazard sub-scores + weighted overall risk score per hub, fully in code, reproducible
4. **Agent with tools** — LLM understands the question, calls tools (rank hubs, compare hubs, explain score, weather stats like "% of snow days"), and explains results in plain language
5. **Enforced JSON schema** between LLM and code (validated with Pydantic)
6. **Guardrails** — block prompt injection and off-topic requests; agent stays in scope
7. **Model fallback** — primary + fallback LLM
8. **REST API (FastAPI)** exposing the agent, with conversation memory for follow-up questions
9. **Simple chat web UI** that talks to the API
10. **Eval set** (normal + adversarial questions) with a one-command runner
11. **Clear communication** of assumptions, uncertainty and data limitations in answers
12. **Deliverables** — README with run instructions + design/architecture doc (architecture, repo structure, data storage, scoring methodology, why an LLM, system prompt, eval results, tradeoffs)

## Phase 2: Post-Launch

1. **Scheduled risk alerts** *(stretch goal — may be pulled into MVP if time allows; APScheduler + score-history groundwork is already in the stack)* — daily job recomputes hub scores and sends a webhook/email alert when a hub's risk score changes beyond a threshold
2. **Voice input/output** in the chat UI
3. **Jev classifier** (TypeSafe AI, released Sept 2026) for fast intent routing and guardrail checks *(done and measured, not used by default: `JevClassifier` plus the `skyrisk eval-classifier` benchmark. Jev was faster and cheaper but wrongly refused 43% of legitimate look-alikes. Never wrongly blocking a user comes first, so the classifier is Haiku alone, with no fallback. Jev is opt-in via `SKYRISK_CLASSIFIER=jev` for re-testing. The OpenAI outage path is measured too: with `--simulate-outage anthropic`, 33/33 after a score-tampering prompt rule and a 30 s provider timeout. See `docs/DESIGN.md` §7 and §10)*
4. **Live forecast layer** — short-term risk (next 7 days) on top of historical exposure
5. **Cost/impact weighting** — factor hub revenue and shipment volume into prioritization
6. **Deployment** — to a public URL *(done: a single free Render web service with per-IP and daily chat limits; a deployed app makes the demo easier to access. See `docs/DESIGN.md` §11, Deployment)*
7. **Executive summary report export** for leadership
