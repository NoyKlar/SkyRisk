# References for Render Deployment + Chat Rate Limits

## Similar Implementations

### Session store

- **Location:** `src/skyrisk/api/sessions.py` (`SessionStore`)
- **Relevance:** in-process state with the same lifetime and concurrency concerns as the rate limiter.
- **Key patterns:**
  - An injectable `now` clock.
  - A global `threading.Lock`.
  - A sweep that bounds memory.

### API test client

- **Location:** `tests/test_api.py` (`_client`, `_chat`)
- **Relevance:** the API tests build a real `Agent` with `FakeProvider`s and inject collaborators into `create_app`. The limiter is injected the same way.

### Serve command

- **Location:** `src/skyrisk/cli.py` (`_serve`), `src/skyrisk/agent/factory.py` (`build_agent`)
- **Relevance:**
  - `build_agent` returns `(agent, config)`, so `_serve` can build the limiter from `config.rate_limit`.
  - `serve` already honours `$HOST` and `$PORT`.
