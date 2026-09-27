# SkyRisk

Weather-risk scoring for US logistics hubs.

## Quick start

```bash
uv sync
uv run skyrisk ingest        # fetch Open-Meteo + FEMA NRI data into data/skyrisk.db
uv run skyrisk score         # compute and store a ranked score run
uv run skyrisk show chicago  # explain one hub's latest scores
uv run pytest
```
