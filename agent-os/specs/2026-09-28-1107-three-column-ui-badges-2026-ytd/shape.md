# Three-column UI, Near-term Badges, 2026 YTD — Shaping Notes

## Scope

A UI and data follow-up to the near-term risk alerts spec:

1. **Layout.** Three columns: hubs list on the left, chat in the middle, and "Recent alerts" in its own right-side panel with the same width and style as the left one. On narrow screens the alerts panel drops below the chat.
2. **Near-term badges.** A public `GET /api/near-term` returns each hub's current near-term score and level. It reuses `NearTermService` and its 1 h cache. The left list shows a small low/medium/high pill after each hub name: green, amber or red, and "–" when the forecast is unavailable. The pill looks like the level pill in the alerts panel.
3. **2026 year-to-date in `weather_stat` only.** Stat questions for 2026 are answered from the Open-Meteo archive, up to its latest available date. They are clearly labelled as a partial year, with the exact date range. The historical risk score stays on the full years 2016–2025 and does not change. The system prompt, the history-2026 and history-this-year eval cases, README and DESIGN are updated to match. DESIGN gets a refresh-policy note: in production the score would be recomputed each January on a rolling 10 full years, versioned, and gated by evals.
4. **Workflow.** In `.github/workflows/near-term-check.yml`, replace `-f --retry-all-errors` with `--fail-with-body --retry-connrefused`. 400 and 401 then fail fast with the server's reason in the log, and cold starts are still retried.

## Decisions

- **Breakpoints.** At ≥1100px the page is a grid: `220px | chat (fluid, max 900px) | 220px`. Below 1100px it is one column: the hubs panel is hidden (the welcome "Hubs:" line stays) and the alerts panel sits below the chat.
- **Colors.** A new `--ok` green token is added. `.level.low` becomes green everywhere, including the alerts panel.
- **YTD year.** Only `window.end.year + 1` is allowed, and only while it is the current year according to the injected clock. This is the refresh policy expressed in code.
- **YTD `days_per_year`.** It returns the actual count of matching days to date, not an annualized figure, with a caveat.
- **YTD storage.** An in-memory per-hub cache with a 6 h TTL. Nothing is written to SQLite, so ingest and score runs are untouched.
- **Grounding.** YTD numbers are not risk scores and never go in `scores_cited`, so `_check_grounding` still holds.
- **Workflow shell.** The step gets `shell: bash`, which adds pipefail. Without it, the pipe into `tee` would hide curl's exit code.
- **Repo rules.** Commit on `main`, never push without an OK, and estimate cost before any paid eval run.

## Context

- **Visuals:** None. Follow the existing page styling.
- **References:** see references.md.
- **Product alignment:** a refinement of roadmap Phase 2 #4 (live forecast layer). `tech-stack.md` gets a note about the YTD fetch, which is used for stats only. The mission is unchanged: scores stay deterministic and full-year.

## Standards Applied

- **backend/external-api-clients:** the YTD fetch reuses `fetch_daily`/`parse_archive` through `get_json`, and fails loud.
- **testing/injectable-side-effects:** the YTD service takes injected `fetch` and `today`, and the near-term levels endpoint reuses the injected service.
- **testing/no-live-network:** a recorded archive fixture, and fakes for the service and API tests.
