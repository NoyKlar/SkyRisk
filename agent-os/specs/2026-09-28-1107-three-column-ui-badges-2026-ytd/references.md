# References for Three-column UI, Near-term Badges, 2026 YTD

## Similar Implementations

### Near-term risk alerts spec
- **Location:** `agent-os/specs/2026-09-28-0947-near-term-risk-alerts/`
- **Relevance:** the previous step: the near-term score, the alerts and the side panel.
- **Key patterns:** plan and shape format; how the service, API, UI, evals and docs were split.

### Alerts API and side panel
- **Location:** `src/skyrisk/api/app.py` (alerts routes), `src/skyrisk/api/static/{index.html,app.js,app.css}`
- **Relevance:** the new endpoint and panel follow the same patterns.
- **Key patterns:** public GET routes returning pydantic models; `{"detail": ...}` errors; `renderAlerts`/`renderHubs`; the `.level.{low,medium,high}` pills; silent `.catch` because the panels are a convenience.

### NearTermService
- **Location:** `src/skyrisk/nearterm/service.py`
- **Relevance:** the badges reuse it, and `YtdService` copies its shape.
- **Key patterns:** injected `fetch` and `now`; a per-hub TTL cache behind a lock; failures become a safe `*Unavailable` message and are never cached.

### weather_stat tool
- **Location:** `src/skyrisk/agent/tools.py` (`weather_stat`)
- **Relevance:** the tool gains a YTD branch.
- **Key patterns:** checks against the runtime window, `ToolError` messages, caveats, `period_start` and `period_end`.

### Open-Meteo archive client
- **Location:** `src/skyrisk/ingest/open_meteo.py`
- **Relevance:** the YTD fetch reuses `fetch_daily` and `parse_archive`.
- **Key patterns:** the parse/fetch split, `EXPECTED_UNITS` pinning, and the `retries` kwarg for interactive calls.

### Grounding check
- **Location:** `src/skyrisk/evals/checks.py` (`_check_grounding`)
- **Relevance:** YTD numbers must never enter `scores_cited`, so grounding stays valid.
