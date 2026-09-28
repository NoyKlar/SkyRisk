# Three-column UI, near-term hub badges, 2026 year-to-date weather stats

## Context

This is a follow-up to the near-term risk alerts spec (`agent-os/specs/2026-09-28-0947-near-term-risk-alerts/`). It covers four things.

**1. Layout.** "Recent alerts" currently sits under the hubs list in a fixed left panel, and that panel only appears at ≥1388px. On narrower screens neither the hubs nor the alerts are visible once the chat starts. The new layout has three columns:
- hubs on the left
- chat in the middle
- alerts in a right panel with the same width and style as the left one

On narrow screens the alerts panel drops below the chat.

**2. Near-term badges.** Analysts can't see this week's risk per hub without asking. A new public `GET /api/near-term` returns each hub's current near-term score and level. It reuses `NearTermService` and its 1 h cache. Each hub name in the left list gets a small pill after it.

**3. 2026 year-to-date stats.** "How many snow days in Denver in 2026?" is refused today. After this change `weather_stat` answers it:
- with partial-year data from the Open-Meteo archive, up to the latest date available
- labelled as a partial year with the exact date range

The historical risk score stays on the full years 2016–2025 and does not change.

**4. Workflow fix.** In `near-term-check.yml`, curl should fail fast on 400/401 and show the server's reason in the log, while still retrying cold starts.

**Decisions made while shaping**
- **Breakpoints.**
  - ≥1100px: CSS grid `220px | chat (fluid, max 900px) | 220px`.
  - <1100px: one column. The hubs panel is hidden; the welcome "Hubs:" line stays as it is today. The alerts panel sits below the chat and the page scrolls to reach it.
- **Badges.** A small pill after the hub name, styled like the alerts-panel level pill: green for low, amber for medium, red for high. It shows "–" when the forecast is unavailable. A new `--ok` green token makes `.level.low` green everywhere, so the alerts panel matches.
- **YTD scope.** The only extra year allowed is `window.end.year + 1`, and only when it equals the current year from the injected clock. This rule is the refresh policy in code: once 2027 starts, 2026 is refused until the window rolls.
- **YTD `days_per_year`.** For the partial year it returns the actual count of matching days to date, not an annualized figure, with a caveat. `pct_days` is unchanged.
- **YTD storage.** An in-memory per-hub cache with a 6 h TTL, modelled on `NearTermService`. Nothing is written to SQLite, so the historical `weather` table, the ingest completeness check and score runs are untouched.
- **Grounding.** YTD stats are counts and percentages, not risk scores. The prompt says never to put them in `scores_cited`, so `_check_grounding` (`evals/checks.py:75`) is unaffected. A YTD hazard score is never computed.
- **Standards.** `backend/external-api-clients`, `testing/injectable-side-effects`, `testing/no-live-network`.
- **Repo rules.** Commit on `main`. Never push without an explicit OK (Render auto-deploys). Estimate cost before any paid eval run.

---

## Task 1: Save spec documentation

Create `agent-os/specs/2026-09-28-1107-three-column-ui-badges-2026-ytd/`, following the format of `2026-09-28-0947-near-term-risk-alerts/`:
- **plan.md**: this plan
- **shape.md**:
  - scope: the user's brief, including the workflow curl change
  - the decisions above
  - context: visuals none; product alignment is a refinement of roadmap Phase 2 #4; the tech-stack note
- **standards.md**: the full text of the three standards
- **references.md**:
  - the near-term alerts spec
  - `api/app.py` alerts routes (`:118-147`)
  - `static/app.js` `renderAlerts`/`renderHubs` and `app.css` `.level` pills
  - `agent/tools.py` `weather_stat` (`:375-462`)
  - `nearterm/service.py` (cache and `NearTermUnavailable`)
  - `ingest/open_meteo.py` (`fetch_daily`/`parse_archive` split)
  - `evals/checks.py` `_check_grounding`

## Task 2: Three-column layout (`static/index.html`, `app.css`, `app.js`)

**Markup.** Move `section#alerts` out of `#hubs-panel` into a new `aside#alerts-panel.alerts-panel`. Wrap the page in a layout container: hubs aside, then `#app`, then the alerts aside.

**CSS.**
- Replace the `position: fixed` / `@media (min-width:1388px)` block (`app.css:39-43`, `104-117`) with a grid at `@media (min-width:1100px)`:
  - `grid-template-columns: 220px minmax(0, 900px) 220px`, centered, full height
  - each side panel scrolls on its own
- Mirror the left panel's gradient divider on the right panel's left edge.
- Below 1100px:
  - hubs panel `display:none`
  - `#app` at `height: 100dvh`
  - alerts panel as a normal block below it, same padding and style
- `.alerts-line` in the welcome card becomes redundant because the panel is always visible. Remove it along with its JS in `renderAlerts`. Keep `.hubs-line` for narrow screens.
- Update the comment that explains the breakpoint math.

**JS.** `renderAlerts` targets the new panel and un-hides it. The alerts fetch is unchanged.

## Task 3: `GET /api/near-term` endpoint

**Service** (`nearterm/service.py`):
- Add `levels(self) -> list[HubLevel]`. It calls `self.score(h.id)` using the cache, never `fresh`.
- Fetch across hubs with a small `ThreadPoolExecutor(max_workers=4)`, so a cold cache with 13 hubs doesn't stack up 10 s timeouts.
- Catch `NearTermUnavailable` per hub and return `score=None, level=None, error=str(e)`.
- `HubLevel` is a pydantic model: `hub_id, score: float|None, level: Level|None, forecast_start, forecast_end, error`.

**Route** (`api/app.py`):
- `GET /api/near-term` is public and returns `NearTermLevels{hubs: [...], cached_ttl_s}`.
- It returns 503 `{"detail": ...}` when `ctx.near_term is None`, the same pattern as the check route.
- Outbound calls are capped at about 13 per hour by the 1 h cache, so no extra rate limit is needed. Note this in DESIGN.

**Tests** (`tests/test_api.py`, `tests/test_near_term.py`):
- use `fake_service` and the fake `Clock` that already exist
- one hub fails and the others still come back
- a second call inside the TTL does no fetch
- 503 when the service is not configured

## Task 4: Hub badges in the left list (`app.js`, `app.css`)

- In `renderHubs`, append `span.level.nt-badge[data-hub=id]` with the text "–" after each hub button.
- Then `fetch("/api/near-term")` and fill each badge:
  - text is the level (low / medium / high)
  - class is `level <level>`
  - `title` is `Near-term ${score} (${level}), ${forecast_start}–${forecast_end}`
- On error, or when `level` is null, keep "–" with title "Forecast unavailable". A failure never blocks the hubs list.
- CSS:
  - add `--ok` (green) to `:root`
  - `.level.low` uses `--ok` (Task 2 note: this also turns the alerts low pill green)
  - `.nt-badge` gets a small left margin
  - `.hub` stays clickable and the badge is not part of the button

## Task 5: 2026 year-to-date in `weather_stat`

**Fetch** (`ingest/open_meteo.py`):
- Add a `retries` kwarg to `fetch_daily`, like `fetch_forecast`. The call is interactive, so use `retries=1`.
- Add a pure helper `trim_trailing_missing(days) -> list[WeatherDay]` that drops trailing days where every variable is `None`.
- First step of this task: probe the live archive once with `end_date=today` using curl. The standard requires verifying against the live API.
  - If it returns trailing nulls, request up to today and trim.
  - If it returns 400 out-of-range, request up to `today - ARCHIVE_LAG_DAYS` (a module constant) and trim.
- Record the response as `tests/fixtures/open_meteo_archive_denver_ytd.json`, trimmed to the last ~10 days.

**Service**: new `src/skyrisk/history/ytd.py` with `YtdService(registry, window, fetch, *, today=utc_today)`, modelled on `NearTermService`:
- per-hub cache with `YTD_CACHE_TTL_S = 6 * 3600`
- lock
- `YtdUnavailable` for failures and empty results
- `year()` returns `window.end.year + 1` only if that equals `today().year`, otherwise `None`
- `days(hub_id)` returns `(start=Jan 1, data_through, days)`

Build it in `cli.py` next to `_near_term_service`, for the commands in `NEAR_TERM_COMMANDS`, using the forecast client with its 10 s timeout. Add `ToolContext.ytd: YtdService | None = None`.

**Tool** (`agent/tools.py` `weather_stat`):
- If `args.year == ctx.ytd.year()`, load days from `ctx.ytd`; otherwise keep the existing window logic.
- The error message for other out-of-window years still names the window, and adds that `{end+1}` is available as year-to-date.
- `period_end` is `data_through`.
- `WeatherStatResult` gets `partial_year: bool = False`.
- For `days_per_year` in a partial year, the value is the matching-day count. Add a caveat: "Actual count of matching days to date, not annualized."
- Months filter with no data yet: raise `ToolError("No data yet for months ... in 2026 (data through YYYY-MM-DD).")`
- Add the caveat: "2026 is a partial year: 2026-01-01 to {data_through}, the latest date available in the Open-Meteo archive (recent days may be revised). Not comparable to full-year figures and not part of the risk score, which uses full years 2016–2025."
- Update the tool description at `:608-613`.

**System prompt** (`agent/prompts.py` `## Time`):
- Historical risk scores use full years {start}–{end} only.
- For {end+1} or "this year", weather_stat returns year-to-date statistics. Always call them partial, and give the exact date range from the tool.
- Never compute or imply a {end+1} risk score or rank.
- YTD counts and percentages are not risk scores and never go in `scores_cited`.
- "Last year" is still {end}.

**Tests** (`tests/test_tools.py`, `tests/test_ingest.py`, new `tests/test_ytd.py`):
- 2026 is answered via a fake `YtdService`, with `partial_year`, `period_end` and caveats set
- 2027 and 2014 are still refused
- `days_per_year` returns the raw count
- the months-with-no-data error
- `year()` is `None` when the clock is in 2027
- parse and trim use the recorded fixture
- the cache TTL
- update `test_tools.py:63` and the prompt assertion at `test_near_term.py:267`

## Task 6: Eval cases (`evals/cases.yaml`)

**Changed cases**
- **`history-2026`**
  - expect `answered`
  - `expect_tool: {name: weather_stat, arguments: {hub_ids: [denver], stat: snow_day, year: 2026}}`
  - `must_mention: ["2026-01-01", "partial"]`
- **`history-this-year`**
  - expect `answered`
  - `expect_tool` weather_stat `{hub_ids: [houston], stat: heavy_rain, year: 2026}`
  - `must_mention: ["2026-01-01"]`

**New case**
- **`history-2026-score`**: "What is Denver's 2026 risk score?"
  - expect `[answered, needs_clarification]`
  - `must_mention: ["2025"]`
  - the historical score covers full years only
  - grounding catches any invented score

**Header comment.** Update it to say 2026 appears as year-to-date in `weather_stat` only.

**Running it.** Run offline pytest first. Then estimate the API calls for `uv run skyrisk eval --case 'history-*'` (about 6 cases, fewer than 20 model calls) and get an OK before running it. After that, the full suite on the normal and outage paths, again with an estimate first.

## Task 7: Workflow curl fix (`.github/workflows/near-term-check.yml`)

- Replace `-fsS ... --retry-all-errors` with `-sS --fail-with-body --retry 4 --retry-delay 30 --retry-connrefused --max-time 180`.
- Add `shell: bash` to the step. That turns on `-o pipefail`, so curl's exit code survives the pipe into `tee`; the default `bash -e` doesn't catch it.
- Guard the python summary so it only runs on success.
- Update the comment: 408, 429 and 5xx cold starts are retried; connection refused is retried; 400 and 401 fail at once with the body in the log.

## Task 8: Docs

- **README**:
  - `:4`, `:50`: 2026 YTD stats only; the score stays 2016–2025
  - §7: the `/api/near-term` endpoint and hub badges
  - layout note
- **docs/DESIGN.md**:
  - §4 `:168`: YTD is separate from the score
  - §6: the prompt quote
  - §7: the "Near-term and 2026 cases" table and the results
  - §9 `:621-625`: assumptions
  - §12: the endpoint, cache and rate reasoning
  - new **"Refresh policy"** subsection in §4: in production the historical score would be recomputed each January on a rolling 10 full years, versioned (scoring config version and hash plus data hash, as already stored per run), and gated by the eval suite before deploy. Until then, the YTD year is `window.end + 1` and is refused once the calendar moves past it.
- **agent-os/product**:
  - `tech-stack.md`: add "Open-Meteo archive, current-year year-to-date for weather_stat only (not scored)"
  - `roadmap.md` #4: add a note about the badges and the YTD stats

## Verification

1. `uv run pytest` is all green, with no live network.
2. `uv run skyrisk serve`, then in a browser:
   - at 1440px and 1150px: three columns
   - at 900px and 390px: chat full height, then alerts below; hubs hidden; "Hubs:" line visible
   - badges fill in, green, amber or red; with the network blocked (or `ctx.near_term=None`) they show "–"
   - `curl localhost:8000/api/near-term` returns 13 hubs
3. `uv run skyrisk chat`:
   - "How many snow days did Denver have in 2026?" gives a partial-year answer with the exact dates
   - "Denver 2026 risk score?" is refused, pointing to 2016–2025
4. Evals: `--case 'history-*'`, then the full run on both paths, each after a cost estimate and an OK. Record the results in `evals/results/latest.*` and DESIGN §7.
5. Workflow: check with `actionlint` if it's available. Real verification needs a `workflow_dispatch` after a push, which requires the user's OK.
6. Commit on `main` in logical commits (ui, api, ytd, evals, workflow, docs). Do not push.
