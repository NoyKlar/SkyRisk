# Tech Stack

## Frontend

- Plain HTML/CSS/JS chat page served by FastAPI (no build step)

## Backend

- Python 3.12
- FastAPI, served by uvicorn (`skyrisk serve`)
- Pydantic v2
- httpx (calls to public data APIs)
- APScheduler (daily score recompute + alert webhook)

## Database

- SQLite — hubs, cached weather/hazard data, computed scores, score history for alerts

## LLM

- **Primary:** Anthropic Claude Sonnet 5 — strong tool use and structured JSON output; good balance of quality, speed and cost
- **Fallback:** OpenAI GPT — a different provider, so an outage at one provider doesn't take the agent down
- **Structured output:** Pydantic schemas enforced on every LLM response, with retry on validation failure

## Data Sources

- Open-Meteo historical weather API
- FEMA National Risk Index

## Other

- **Package manager:** uv
- **Testing:** pytest (scoring unit tests) + custom eval runner script
- **Hosting:** Render or Railway (single Python service)
- **Config:** `.env` for API keys, never committed
