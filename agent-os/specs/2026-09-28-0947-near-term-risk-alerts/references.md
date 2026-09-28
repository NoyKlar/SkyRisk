# References for Near-Term Risk Alerts

## Similar Implementations

### Open-Meteo archive client

- **Location:** `src/skyrisk/ingest/open_meteo.py`
- **Relevance:** the forecast API returns the same daily variables and units
- **Key patterns:** `parse_*` / `fetch_*` split; `_ArchiveResponse` pins `EXPECTED_UNITS` and equal lengths. The forecast parser reuses it.

### HTTP helpers

- **Location:** `src/skyrisk/ingest/http.py`
- **Relevance:** all HTTP goes through `_with_retries`. The webhook needs a POST that doesn't parse JSON, because Slack replies with `ok`.

### Agent tools

- **Location:** `src/skyrisk/agent/tools.py`
- **Relevance:** `near_term_risk` follows the `ToolSpec` pattern. `ToolResult.scores()` feeds in-loop grounding, and caveats travel with every number.

### Config models

- **Location:** `src/skyrisk/config.py`, `config/scoring.yaml`
- **Relevance:** `NearTermConfig` mirrors `ScoringConfig`: versioned YAML, Pydantic validators, `config_hash()`.

### Eval grounding re-check

- **Location:** `src/skyrisk/evals/checks.py` (`_check_grounding`)
- **Relevance:** it compares cited scores against the score run. `near_term` cites need their own source, the cached service.

### API + chat page

- **Location:** `src/skyrisk/api/app.py`, `src/skyrisk/api/static/`
- **Relevance:** route style, the injected `log`, the `#hubs-panel` aside where the alerts list goes

### Previous spec

- **Location:** `agent-os/specs/2026-09-27-2132-jev-classifier-comparison/`
- **Relevance:** spec format, and the cost-gate practice for paid eval runs
