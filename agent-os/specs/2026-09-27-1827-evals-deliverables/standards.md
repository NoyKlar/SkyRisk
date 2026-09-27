# Standards for Evals + Deliverables

The following standards apply to this work.

---

## testing/no-live-network


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

---

## testing/injectable-side-effects


Anything slow, noisy, networked or time-dependent is a keyword
parameter with the real implementation as default:

```python
def ingest(conn, registry, config, client: httpx.Client, *,
           log: Log = print,
           sleep: Callable[[float], None] = time.sleep) -> None: ...
```

| Effect | Param | Test value |
|---|---|---|
| HTTP | `client` | `httpx.Client(transport=MockTransport(...))` |
| Waiting | `sleep` | `lambda s: None` or `waits.append` to assert |
| Output | `log` | `lambda m: None` |
| Clock | `now` | fixed datetime (add when a test needs it) |

- Never monkeypatch `time.sleep`, `print`, `datetime` or httpx in tests
- Callers (CLI) own real resources: create the `httpx.Client` there
  and pass it down; library code never constructs one
- Tests must run in seconds even when exercising retries/pacing
