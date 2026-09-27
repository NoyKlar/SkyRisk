# External API Clients

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
