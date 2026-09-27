# Spec 6: Render Deployment + Chat Rate Limits (Roadmap Phase 2, item 6)

## Context

A deployed app makes the demo easier to access. SkyRisk currently runs only locally: `data/skyrisk.db` is gitignored and built by `skyrisk ingest` + `skyrisk score`, and API keys come from `.env`. Putting the chat on a public URL also exposes paid LLM calls, because each question costs about 1 Haiku call and 2–4 Sonnet calls.

**Outcome:**
- The app runs as a single free Render web service.
- The database is rebuilt during every build.
- Secrets live only in the Render dashboard.
- POST /api/chat is protected by a per-IP limit and a global daily cap, and the user sees a friendly message when either is hit.
- The README and DESIGN.md document deployment and how to add or remove a hub.

**Shaping decisions:**
- **Render facts (checked in the current Render docs):**
  - uv is used automatically when `uv.lock` is present. It is committed.
  - Python is taken from `.python-version`, which is already `3.12`.
  - Free services spin down after 15 minutes idle and take about a minute to cold-start.
  - The disk is ephemeral and there are no persistent disks.
  - The service must bind `0.0.0.0:$PORT` (default 10000).
- **Database in the build:** it is built during the build and ships with the deploy.
  - Ingest needs network access but no keys. It takes about 2+ minutes because of the 10 s pause between Open-Meteo requests.
  - If ingest fails, the build fails and Render keeps the previous deploy.
- **Limits:**
  - 20 questions per IP per hour, as a sliding window.
  - 100 questions per day globally, resetting at 00:00 UTC.
  - Both are configurable under `rate_limit:` in `config/agent.yaml`.
  - The counters are in memory, so they reset on a restart or spin-down. This is documented as a tradeoff.
- **Client IP:** the first `X-Forwarded-For` entry, falling back to `request.client.host`.
  - That header can be spoofed, so the per-IP limit only provides fairness. The global cap is the real protection for credits.
  - The README also recommends setting a spend limit in the Anthropic and OpenAI consoles.
- **What counts:** every chat request that passes validation counts, including ones that end up refused, because a refusal still makes a classifier call.
  - A request rejected by the per-IP limit does not use up the global quota.
- **Roadmap:** Deployment stays in Phase 2 with a "done" note.
- **Standards:** `testing/injectable-side-effects` (the limiter takes an injected clock) and `testing/no-live-network`.
- **Git:** commit on main. Pushing is needed before Render can see `render.yaml`, so I'll confirm with you before pushing.

---

## Task 1: Save spec documentation

Create `agent-os/specs/2026-09-27-2205-render-deployment/` in the same format as `2026-09-27-1827-evals-deliverables/`:
- **plan.md:** this plan.
- **shape.md:** scope in your words (5 items), the decisions above, and context.
  - Visuals: none.
  - Product alignment: roadmap Phase 2 #6.
- **standards.md:** the full text of `agent-os/standards/testing/injectable-side-effects.md` and `testing/no-live-network.md`.
- **references.md:** `SessionStore` (the injectable clock, lock and pruning pattern), `tests/test_api.py::_client`, and `cli._serve` together with `factory.build_agent`, which returns `(agent, config)`.

## Task 2: Rate-limit config

- In `src/skyrisk/config.py`, add `RateLimitConfig(BaseModel)` with:
  - `per_ip_per_hour: int = Field(20, ge=1)`
  - `global_per_day: int = Field(100, ge=1)`
- Add `rate_limit: RateLimitConfig = RateLimitConfig()` to `AgentConfig`. The default keeps existing configs and fixtures valid.
- Add to `config/agent.yaml`:
  ```yaml
  rate_limit:            # POST /api/chat on the public deploy
    per_ip_per_hour: 20
    global_per_day: 100
  ```

## Task 3: `RateLimiter`: new file `src/skyrisk/api/ratelimit.py`

- **Constructor:** `RateLimiter(per_ip_per_hour, global_per_day, *, now=time.time)`, with `from_config(cfg)`.
- **`check(ip) -> Limited | None`:** runs under a `threading.Lock`.
  1. Prune that IP's deque of timestamps older than 3600 s. If the deque is full, return `Limited("ip", retry_after=oldest+3600-now)`.
  2. Compute the UTC date key from `now()`. When the date changes, reset the global count. If the count is ≥ the cap, return `Limited("global", retry_after=seconds to the next UTC midnight)`.
  3. Otherwise record the request (append the timestamp, increment the count) and return None.
- **Memory bound:** when the IP map grows past 10,000 entries, sweep out the IPs whose deques are empty or fully expired.
- **`Limited`:** a small dataclass with `scope: Literal["ip","global"]`, `retry_after: int` (at least 1), and a `message` property:
  - For the IP scope: "You've reached the limit of 20 questions per hour. Please try again in about N minutes."
  - For the global scope: "SkyRisk has reached its daily question limit. Please try again after 00:00 UTC."

## Task 4: Wire into the API and CLI

- **`src/skyrisk/api/app.py`:**
  - `create_app(..., limiter: RateLimiter | None = None)`. None means no limit, which keeps the existing tests unchanged.
  - The `chat` handler gains `request: Request`. Add a helper `client_ip(request)`: the first entry of `X-Forwarded-For` (stripped), else `request.client.host`, else `"unknown"`.
  - When `limiter.check(ip)` returns `Limited`:
    - Log `rate limit (<scope>) for <ip>`.
    - Return a `JSONResponse({"detail": msg}, 429, headers={"Retry-After": str(n)})`.
    - Don't call the agent or touch the session.
- **`src/skyrisk/cli.py`, `_serve`:** pass `limiter=RateLimiter.from_config(config.rate_limit)`.
- **`src/skyrisk/api/static/app.js`:** in the `!res.ok` branch, on 429 show `(await res.json()).detail`, with a generic fallback if the JSON can't be parsed.

## Task 5: Tests (offline, injected clock, no monkeypatching)

- **New `tests/test_ratelimit.py`:**
  - Allows N requests per IP, then limits the next one with the right `retry_after`.
  - The window slides: after the clock advances 3600 s, requests are allowed again.
  - Different IPs are independent.
  - The global cap applies across IPs and resets at the UTC date change. `retry_after` is the time to midnight.
  - A per-IP rejection doesn't use up the global quota.
  - The sweep bounds the size of the IP map.
- **`tests/test_api.py`:**
  - `_client` gains a `limiter=None` param.
  - Tests cover:
    - A 429 with its `detail` and a `Retry-After` header.
    - The agent is not called (the FakeProvider's call count doesn't change).
    - Different `X-Forwarded-For` values are counted separately.
    - The global cap returns 429 for a new IP.
    - With no limiter, behaviour is unchanged.
- **`tests/test_config.py`:**
  - `agent.yaml` loads with `rate_limit` 20/100.
  - A config without the section gets the defaults.
  - Zero is rejected.

## Task 6: `render.yaml` blueprint (repo root)

```yaml
services:
  - type: web
    name: skyrisk
    runtime: python
    plan: free
    buildCommand: uv sync --frozen --no-dev && uv run --no-sync skyrisk ingest && uv run --no-sync skyrisk score
    startCommand: uv run --no-sync skyrisk serve --host 0.0.0.0 --port $PORT
    healthCheckPath: /api/health
    autoDeployTrigger: commit
    envVars:
      - key: ANTHROPIC_API_KEY
        sync: false
      - key: OPENAI_API_KEY
        sync: false
```

- Python comes from `.python-version` (3.12).
- Before writing the file, confirm that `--no-dev` and `uv run --no-sync` work with the committed lockfile. Simulate the build in a fresh clone (see Verification).

## Task 7: README

- **New `## 9. Deployment (Render)` section**, before `## Project layout`:
  - Steps: push to GitHub, then New → Blueprint → select the repo, then enter the two keys when prompted.
  - What the build does: installs deps, ingests (about 2–3 minutes), and scores. The data is rebuilt on every deploy, and a failed ingest keeps the previous deploy running.
  - Env vars, plus the optional `SKYRISK_*_MODEL` overrides.
  - Free-tier behaviour:
    - Spin-down after 15 minutes idle, then a cold start of about 30–60 s.
    - Chat memory and rate-limit counters reset on a restart.
  - The rate limits, how to change them in `agent.yaml`, and the recommendation to set provider spend limits.
- **New `## 10. Adding or removing a hub` section:**
  1. Edit `config/hubs.yaml` (the fields and validation rules).
  2. Run `uv run skyrisk ingest`, then `uv run skyrisk score`.
  3. Scores are relative, so every hub's score may shift.
  4. Eval cases that name hubs or rankings may need updating. Rerun `skyrisk eval`.
  5. On Render, the next deploy picks it up automatically.
  - Check how `ingest` treats a removed hub. If rows for a removed hub linger, document it, or note `--refresh`.

## Task 8: DESIGN.md and roadmap

- **DESIGN.md:**
  - Add `## 11. Deployment` covering:
    - The architecture: one Render web service, the DB built at build time, secrets in the dashboard.
    - The tradeoffs:
      - Free tier and cold starts.
      - An ephemeral disk, so the DB is rebuilt and sessions and counters are in memory.
      - A single instance, which in-memory state requires.
      - The spoofable `X-Forwarded-For` compared with the global cap.
      - Build-time dependence on Open-Meteo and FEMA.
    - What would change at scale: Postgres or Redis for sessions and counters, and a scheduled ingest.
  - Remove "deployment" from §9's out-of-scope list and add a pointer to §11.
  - Add a one-line mention in §1 architecture and in §8 tradeoffs if it fits.
- **`agent-os/product/roadmap.md`:** mark Phase 2 #6 Deployment as done, and link DESIGN §11. Reword its motivation to "a deployed app makes the demo easier to access".
- **Reword references to the original brief** in `agent-os/specs/2026-09-27-1616-agent-layer/plan.md` and `shape.md` to "the headline example questions", matching the `core_examples` wording.

## Verification

1. Run `uv run pytest`. It should all pass, offline. A repo-wide grep should confirm the reworded brief references are gone.
2. **Local limit check:** temporarily set `per_ip_per_hour: 2`, run `PORT=10001 uv run skyrisk serve --host 0.0.0.0`, and send `curl` requests three times.
   - The third request should return 429 with `Retry-After`.
   - The UI should show the friendly message.
   - Revert the setting afterwards.
3. **Build simulation:** in a fresh clone in the scratchpad directory, run the exact `buildCommand` and time it. Then start the app with the `startCommand` and a `PORT` value, and `curl /api/health` and `/api/hubs`.
4. **Deploy:** after committing on main and getting your OK to push, you connect the Blueprint in Render and enter the keys. Then check:
   - The build logs.
   - `https://<service>.onrender.com/api/health`.
   - A real chat question.
   - The server log line showing the client IP from `X-Forwarded-For`, to confirm the IP detection works behind Render's proxy.
