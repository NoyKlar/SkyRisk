# Chat API + Web UI — Shaping Notes

## Scope

Roadmap Phase 1, items 8–9:
- a FastAPI REST API that exposes the existing agent, with per-session conversation memory for follow-up questions
- a simple chat web page that talks only to that API

User's design brief: professional and uncluttered, with a dark blue background and white text. Each answer shows its limitations section and a small footer with the model that served it and the tools it used.

## Decisions

- **Reuse the spec-2 agent unchanged.** The API wraps `Agent.ask(conversation, question)`, `Conversation` and `build_agent`.
- **Memory is an in-memory `SessionStore`:**
  - It maps a session id to a `Conversation`, with a 1 h idle TTL and at most 500 sessions (LRU eviction).
  - It is lost on restart, which is acceptable for a single-process MVP. SQLite persistence was considered and rejected as unnecessary for now.
  - Ids are server-generated. An unknown or expired id starts a new session, flagged with `session_reset: true`.
  - A per-session lock keeps the turns in a session in order.
- **Concurrency:** the routes are sync, so they run in FastAPI's threadpool. The shared SQLite connection uses `check_same_thread=False`, which is safe because `sqlite3.threadsafety == 3` and tools only read.
- **HTTP contract:**
  - Every agent outcome returns 200 with a `status` field.
  - Invalid requests return 422. Unexpected exceptions return a generic 500.
  - `warnings` and `guardrail` details are logged server-side and never returned, so provider error text doesn't leak.
- **UI:**
  - Plain HTML/CSS/JS served by FastAPI, with no build step.
  - Model output is rendered only with `textContent`.
  - `dir="auto"` is set on the bubbles and the input, so Hebrew and other RTL text renders correctly.
- **Example questions** in the empty state are fixed by the user:
  - "Which hubs in the Midwest are most exposed to winter disruption?"
  - "Compare Miami and Houston in terms of hurricane and flood exposure."
  - "What percentage of days in Denver last year had snowfall?"
  - "Why is the Dallas hub's weather disruption risk high?"
- **Deploy-ready, not deployed:** `GET /api/health`, and `skyrisk serve` reads `--host`/`--port` or `HOST`/`PORT`. The actual deployment (roadmap Phase 2, item 6) is a later spec.

## Context

- **Visuals:** None. The design notes above are the brief.
- **References:**
  - `cli.py` `_print_reply` and `_chat`
  - `agent/core.py`
  - `agent/factory.py`
  - `tests/fakes.py`
  - `tests/conftest.py`

  See references.md.
- **Product alignment:**
  - tech-stack.md specifies a plain HTML/CSS/JS page served by FastAPI and a single Python service on Render/Railway.
  - The mission's rule that the LLM never invents scores is kept, because the API only relays the grounded `AgentReply`.

## Standards Applied

- testing/no-live-network: API tests use `FakeProvider` through FastAPI's `TestClient`. There are no real LLM calls.
- testing/injectable-side-effects: the agent, the session store (and its clock) and `log` are injected into `create_app`.
