# No Live Network in Tests

Default `pytest` never calls Open-Meteo, FEMA or any external API.

- **Parsing tests** use real responses recorded with curl into
  `tests/fixtures/<source>_<subject>.json`
  - Trim to the smallest useful range (5 days, 1 county, only our outFields)
  - Never hand-edit values; re-record when API version/fields change
- **Pipeline/cache tests** build synthetic payloads in code and serve
  them via `httpx.MockTransport` (see `_fake_api` in test_ingest.py)
- **Live smoke tests** are marked `@pytest.mark.live` and skipped by default;
  run with `uv run pytest -m live`. Keep them few and cheap (rate limits).

```python
client = httpx.Client(transport=httpx.MockTransport(handler))
```
