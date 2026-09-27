# References for Agent Layer

## Similar Implementations

### Scoring metrics

- **Location:** `src/skyrisk/scoring/metrics.py`
- **Relevance:** `pct_days` / `days_per_year` back the `weather_stat` tool
- **Key patterns:** null days are left out of the denominator, and thresholds come from `scoring.yaml`

### Storage loaders and score runs

- **Location:** `src/skyrisk/db.py`
- **Relevance:** `load_weather`, `load_nri`, `latest_run_id`, and the `hub_scores` / `hazard_scores` / `metric_values` / `hub_metrics` tables are what the tools read
- **Key patterns:** plain `sqlite3`, and every query is scoped to the latest `run_id`

### CLI explanation queries

- **Location:** `src/skyrisk/cli.py` (`_show`)
- **Relevance:** `explain_score` needs the same queries, which move into shared tool code
- **Key patterns:** raw → normalized → points, notes, and NRI `*_RISKS` shown as reference only

### External API client pattern

- **Location:** `src/skyrisk/ingest/`
- **Relevance:** the LLM adapters follow the same pure-parse/thin-call split

## External Docs

- Anthropic Messages API: tool use (manual loop), strict tools, structured outputs (`output_config.format`), adaptive thinking; the Python SDK (`anthropic`)
- OpenAI Responses API function calling: https://developers.openai.com/api/docs/guides/function-calling
- OpenAI `gpt-6-luna` model page: https://developers.openai.com/api/docs/models/gpt-6-luna
