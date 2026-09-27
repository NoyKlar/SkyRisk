# Standards for Data + Scoring Core

No standards are defined yet (`agent-os/standards/index.yml` is empty).

After this spec is implemented, run `/discover-standards` to capture conventions established here, e.g.:
- Pydantic models for all external data and config
- Pure scoring functions with no I/O
- Plain `sqlite3` with an idempotent schema
- No live network in tests
