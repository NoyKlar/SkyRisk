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
