# Standards for Agent Layer

The following standards apply to this work.

---

## backend/external-api-clients


One module per source in `src/skyrisk/ingest/`, split into:

- `parse_*(payload: dict) -> Model` — pure; all validation lives here
- `fetch_*(..., client) -> Model` — only builds params, calls
  `get_json`, returns `parse_*(...)`

```python
def fetch_county(hub, client):
    return parse_query(get_json(client, URL, params))
```

- All HTTP goes through `ingest/http.get_json` (retries 429/5xx and
  transport errors, honors Retry-After, backoff capped at 60s). No raw
  `client.get` elsewhere.
- Validate with Pydantic. Fail loud: wrong units, missing fields or
  unexpected shape raise (e.g. `NriLookupError`) — never coerce.
- Exception: documented per-value nulls (missing weather day, null
  `CFLD_AFREQ`) pass through as `None`; defaulting happens downstream
  with a note, never in the client.
- Pin field names/versions as module constants (`OUT_FIELDS`,
  `EXPECTED_UNITS`) and verify against the live API when changing them.
- Tests target `parse_*` with recorded fixtures (see
  testing/no-live-network).

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
