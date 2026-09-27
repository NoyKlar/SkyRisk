# Product Mission

## Problem

A US logistics company runs regional distribution hubs. Severe weather (winter storms, hurricanes, floods, heat, tornadoes) shuts hubs down, delays shipments and costs money. Each year the company can fund resilience upgrades for only a handful of hubs, but analysts lack a consistent, data-driven way to compare weather exposure across hubs. Today that decision relies on scattered sources and gut feeling, so it's hard to justify, reproduce or explain.

SkyRisk gives analysts a conversational agent that pulls public weather/hazard data, scores every hub with transparent deterministic logic, and explains why a hub ranks where it does, so investment decisions are defensible and repeatable.

## Target Users

Risk and operations analysts at the logistics company who evaluate hub weather exposure and prepare resilience-investment recommendations.

## Solution

- **The LLM never invents scores.** It handles conversation, question understanding and explanation; all hub risk scores come from transparent, deterministic code using public weather/hazard data, so every ranking is reproducible and explainable.
- **Enforced contract.** The LLM and code communicate through an enforced JSON schema.
- **Production-ready from day one:**
  - Guardrails against prompt injection and off-topic use
  - An eval set that every prompt/model change must pass
  - Primary + fallback model, so the agent stays up if one provider fails

## Scope & Assumptions

- **In scope:** ~10–15 US hubs, historical weather exposure + FEMA hazard data, analyst-facing chat agent
- **Out of scope (MVP):** live forecasts, financial impact modeling, non-US hubs, multi-user auth
- **Assumptions:** historical exposure is a reasonable proxy for future risk; public data (Open-Meteo, FEMA NRI) is accurate enough for relative ranking, not absolute prediction
- **Uncertainty:** scores are relative rankings between hubs, not probabilities of shutdown; the agent states data limits in its answers
