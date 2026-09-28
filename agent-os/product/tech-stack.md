# Tech Stack

## Frontend

- Plain HTML/CSS/JS chat page served by FastAPI (no build step)

## Backend

- Python 3.12
- FastAPI, served by uvicorn (`skyrisk serve`)
- Pydantic v2
- httpx (calls to public data APIs)
- GitHub Actions cron (daily near-term alert check via a token-protected endpoint; the free Render service sleeps, so there is no in-process scheduler)

## Database

- SQLite — hubs, cached weather/hazard data, computed scores, near-term snapshots and alerts (ephemeral on Render's free tier; production would use Postgres)

## LLM

- **Primary:** Anthropic Claude Sonnet 5 — strong tool use and structured JSON output; good balance of quality, speed and cost
- **Fallback:** OpenAI GPT — a different provider, so an outage at one provider doesn't take the agent down
- **Structured output:** Pydantic schemas enforced on every LLM response, with retry on validation failure

## Data Sources

- Open-Meteo historical weather API (full years 2016–2025)
- Open-Meteo archive, current-year year-to-date for `weather_stat` only (partial year, cached in memory, not scored)
- Open-Meteo forecast API (7-day daily forecast, near-term risk)
- FEMA National Risk Index

## Other

- **Package manager:** uv
- **Testing:** pytest (scoring unit tests) + custom eval runner script
- **Hosting:** Render (single free Python web service)
- **Config:** `.env` for API keys, never committed
