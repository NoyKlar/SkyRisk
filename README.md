# SkyRisk

Weather-risk scoring for US logistics hubs.

## Quick start

```bash
uv sync
uv run skyrisk ingest        # fetch Open-Meteo + FEMA NRI data into data/skyrisk.db
uv run skyrisk score         # compute and store a ranked score run
uv run skyrisk show chicago  # explain one hub's latest scores
uv run pytest                # offline tests (no network)
```

## Agent chat

```bash
cp .env.example .env         # add ANTHROPIC_API_KEY (primary) and OPENAI_API_KEY (fallback)
uv run skyrisk chat          # interactive; /reset clears memory, /quit exits
uv run skyrisk chat -q "Which hubs in the Midwest are most exposed to winter disruption?"
uv run pytest -m live        # live smoke tests against Claude / OpenAI (uses API credits)
```

Claude Sonnet 5 is the primary model and OpenAI `gpt-6-luna` is the fallback (see `config/agent.yaml`). \
Every score and statistic in an answer comes from deterministic tools over the scoring database.

## Web chat

```bash
uv run skyrisk serve                     # http://127.0.0.1:8000
uv run skyrisk serve --host 0.0.0.0 --port 8080   # or set HOST / PORT
```

The page at `/` is plain HTML/CSS/JS served by the same process. It talks only to the JSON API:

| Method | Path | Purpose |
|---|---|---|
| `POST` | `/api/chat` | `{"message", "session_id"?}` → answer, limitations, scores cited, model and tools used, `session_id` |
| `DELETE` | `/api/sessions/{id}` | clear a conversation's memory |
| `GET` | `/api/hubs` | the hubs you can ask about (id, name, city, state, region) |
| `GET` | `/api/health` | liveness check |
| `GET` | `/api/docs` | OpenAPI docs |

Omit `session_id` on the first message and send back the returned id for follow-ups. \
Conversation memory lives in the server process: a session expires after 1 hour idle (at most 500 are kept), \
and all sessions are lost on restart. An expired id starts a new session and the reply has `"session_reset": true`.
