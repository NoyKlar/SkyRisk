# Injectable Side Effects

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
