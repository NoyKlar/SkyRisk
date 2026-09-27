# Spec 3: Chat API + Web UI (Roadmap Phase 1, items 8–9)

## Context

Spec 2 delivered the agent (`Agent.ask(conversation, question)`), but it is only reachable through the CLI REPL (`skyrisk chat`). This spec puts it behind a FastAPI REST API with per-session conversation memory, so follow-up questions work. It also adds a plain chat web page that talks only to that API.

Outcome:
- `uv run skyrisk serve` starts one Python service that serves the chat page at `/` and the JSON API under `/api`.
- Analysts can ask follow-ups, reset the conversation, and see each answer's limitations.
- Each answer has a small footer showing the model that served it and the tools it used.

**Shaping decisions**
- **Reuse, don't rebuild.** The API wraps the existing `Agent` and `Conversation` (`src/skyrisk/agent/core.py`) and `build_agent` (`src/skyrisk/agent/factory.py`). There are no changes to the agent logic.
- **Memory is an in-memory `SessionStore`:**
  - It maps `session_id` to a `Conversation`, with an idle TTL (default 1 h) and a cap on the number of sessions (default 500, evicting the least recently used).
  - It is lost on restart, which is acceptable for a single-process MVP.
  - Each session has a lock, so turns in the same session run in order.
  - Session ids are server-generated (`secrets.token_urlsafe`). An unknown or expired id silently starts a new session, and the response sets `session_reset: true`.
- **Concurrency:** the endpoints are sync `def`, so FastAPI runs them in its threadpool. The shared SQLite connection is opened with `check_same_thread=False`. This is safe because `sqlite3.threadsafety == 3` (serialized) and tools only read.
- **Response contract:**
  - Every agent outcome returns HTTP 200 with its `status`: answered, refused_*, needs_clarification, or error.
  - Invalid requests return 422. Unexpected exceptions return 500 with a generic message, and the details are logged.
  - `warnings` and `guardrail` details are **logged server-side, not returned**, so provider error strings don't leak.
- **UI design:**
  - Plain HTML/CSS/JS with no build step, per tech-stack.md.
  - Professional and uncluttered: dark blue background and white text.
  - Each answer shows a "Limitations" list and a muted footer with the model and the tools used.
  - Text is rendered only with `textContent`; there is no `innerHTML` for model output.
- **Deploy-ready but not deployed:** add `GET /api/health`. `skyrisk serve` reads `--host`/`--port`, falling back to the `HOST`/`PORT` env vars and then `127.0.0.1:8000`. Actual deployment is a later spec.
- **Standards:** `testing/no-live-network` and `testing/injectable-side-effects`. No visuals beyond the design notes above.

---

## Task 1: Save spec documentation

Create `agent-os/specs/2026-09-27-1719-chat-api-web-ui/`:
- **plan.md**: this plan.
- **shape.md**: scope (items 8–9), the decisions above, context (visuals: none, only the design notes; references; product alignment with tech-stack.md, where Render/Railway is the single-service target), and the standards applied.
- **standards.md**: the full text of `testing/no-live-network` and `testing/injectable-side-effects`.
- **references.md**:
  - `cli.py` `_print_reply` is the layout of each reply: text, limitations, and a footer with served_by and tools.
  - `cli.py` `_chat` is the conversation lifecycle and `/reset`.
  - `agent/core.py` `AgentReply`/`Conversation`.
  - `agent/factory.py` `build_agent`.
  - `tests/fakes.py` `FakeProvider` and `tests/conftest.py` `build_scored_db` are the test harness.

## Task 2: Dependencies and a threaded DB connection

- `pyproject.toml`: add `fastapi` and `uvicorn` via `uv add`. `httpx`, which `TestClient` needs, is already present.
- `src/skyrisk/db.py`: change `connect(path, *, check_same_thread: bool = True)` to pass the flag through to `sqlite3.connect`.
- `tests/conftest.py`: `build_scored_db(check_same_thread=True)` gets the same flag, so API tests can use the DB from TestClient worker threads.

## Task 3: Session store — `src/skyrisk/api/sessions.py`

- `SessionStore(max_turns, ttl_s=3600, max_sessions=500, now=time.monotonic)`. The clock is injected.
- `get_or_create(session_id: str | None) -> tuple[str, Session, bool]` returns the id, the session, and whether it was new or reset. It sweeps expired sessions, evicts the least recently used session over the cap, and updates `last_used`.
- `reset(session_id) -> bool` clears the session's conversation.
- `Session` is a dataclass holding `conversation: Conversation` and `lock: threading.Lock`. The dict is protected by a store-level lock.

## Task 4: API app — `src/skyrisk/api/app.py`

`create_app(agent, sessions: SessionStore, *, log=print) -> FastAPI`. Both the agent and the store are injected, and the app never builds providers itself.

- Pydantic models:
  - `ChatRequest{message: str (1..10_000 chars), session_id: str | None}`. The agent's own `max_input_chars` check still produces the friendly message.
  - `ChatResponse{session_id, session_reset, status, answer, limitations, hubs, scores_cited, served_by, tools_used}`.
- Routes:
  - `POST /api/chat`: get or create the session, take `session.lock`, call `agent.ask(session.conversation, message)`, log any warnings or guardrail, and map the result to `ChatResponse`.
  - `DELETE /api/sessions/{session_id}`: returns 204. It is idempotent.
  - `GET /api/health` returns `{"status": "ok"}`.
  - `GET /` serves `static/index.html`. `/static` is mounted with `StaticFiles` from `Path(__file__).parent / "static"`. Hatch includes it in the wheel automatically.
- Exception handler: log the unexpected error and return 500 `{"detail": "Internal error"}`.

## Task 5: Chat web page — `src/skyrisk/api/static/{index.html, app.css, app.js}`

- **Layout:**
  - A header with "SkyRisk", a one-line subtitle ("Weather-risk exposure for our distribution hubs"), and a "New conversation" button.
  - A scrolling message thread with `aria-live="polite"`.
  - A bottom composer: a textarea plus a Send button. Enter sends and Shift+Enter adds a newline.
  - Max width about 760px, centered, with 16px gutters on mobile.
- **Theme:** CSS tokens on `:root`:
  - Navy background (about `#0b1f3a`), a slightly lighter surface for assistant bubbles, and white text.
  - Muted blue-grey for the footer and limitations text, and one accent color for the Send button and focus rings.
  - The system font stack.
- **Assistant message:**
  - The answer text, with line breaks preserved (`white-space: pre-wrap`).
  - A "Limitations" heading and bulleted list, when present.
  - A footer in small muted text: `served_by · tools: a, b` (tools de-duplicated, as in `_print_reply`), or "no model call" when `served_by` is null.
  - Refusals and errors use a subtle neutral or amber left border.
- **Empty state:** 4 example-question chips with exactly this text. Clicking a chip sends its question.
  - "Which hubs in the Midwest are most exposed to winter disruption?"
  - "Compare Miami and Houston in terms of hurricane and flood exposure."
  - "What percentage of days in Denver last year had snowfall?"
  - "Why is the Dallas hub's weather disruption risk high?"
- **Text direction:** set `dir="auto"` on every message bubble (user and assistant, including the limitations items) and on the composer textarea, so non-English text such as Hebrew renders in the correct direction.
- **Behavior:**
  - While a request is pending, show a "Thinking…" placeholder and disable Send.
  - Store `session_id` in `sessionStorage`, wrapped in try/catch. If `session_reset` is true, show a small "Earlier context expired, starting fresh" note.
  - "New conversation" calls DELETE, clears the thread, and drops the stored id.
  - On network or 5xx failure, show an inline error with a retry hint.

## Task 6: `skyrisk serve` CLI command — `src/skyrisk/cli.py`

- Add a `serve` subparser with `--host` (default `$HOST` or `127.0.0.1`) and `--port` (default `$PORT` or `8000`).
- `main` opens the connection with `check_same_thread=False` for `serve`.
- `_serve`:
  - `load_dotenv()`, then `build_agent`, reusing the same `AgentSetupError` message as `_chat`.
  - Build `SessionStore(max_turns=config.max_history_turns)` and `create_app(...)`.
  - Call `uvicorn.run(app, host, port)`.
  - Share `_chat`'s colored `log` function by lifting it to module level.

## Task 7: Tests — `tests/test_api.py` (offline; FakeProvider + TestClient)

- `POST /api/chat` returns a new `session_id` and the full reply fields: `served_by="fake:primary"`, the `tools_used` list, and `limitations`.
- A follow-up with the same `session_id`: `FakeProvider.histories[-1]` contains the first turn.
- Two sessions are isolated: the second session's history is empty.
- An unknown `session_id` gives a new id and `session_reset: true`.
- After `DELETE /api/sessions/{id}`, the next turn sees empty history. Deleting an unknown id also returns 204.
- An injection-pattern message gives `refused_injection`, and the provider is never called.
- All providers unavailable gives HTTP 200 with `status="error"` and `UNAVAILABLE_MESSAGE`. The response has no `warnings` key, and the log captured them.
- An empty message gives 422.
- `GET /` returns HTML containing the app root, and `/static/app.js` returns 200.
- `GET /api/health` returns ok.
- `SessionStore` unit tests: TTL expiry with an injected clock, and LRU eviction at `max_sessions`.

## Task 8: Docs

- `README.md`: add a "Web chat" section covering `uv run skyrisk serve` and opening http://127.0.0.1:8000, the API endpoints table, and a note that memory is in-process.
- `agent-os/product/tech-stack.md`: mention uvicorn.

---

## Verification

1. `uv run pytest`: all offline tests pass, including the new `test_api.py`.
2. `uv run skyrisk serve`, then:
   - `curl localhost:8000/api/health`
   - `curl -X POST localhost:8000/api/chat -H 'content-type: application/json' -d '{"message":"Which hubs in the Midwest are most exposed to winter disruption?"}'`
   - Re-post a follow-up ("And which of those had the most snow days last year?") with the returned `session_id` and confirm it resolves "those" from context.
3. Open http://127.0.0.1:8000 in a browser and check:
   - the dark blue and white theme
   - the 4 example chips
   - a Hebrew question renders right-to-left in both the input and the bubbles
   - the limitations list and the model/tools footer on each answer
   - a refusal for an off-topic question
   - the "New conversation" reset
   - the layout at phone width
