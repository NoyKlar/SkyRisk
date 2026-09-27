# References for Evals + Deliverables

## Similar Implementations

### Agent core

- **Location:** `src/skyrisk/agent/core.py` (`Agent.ask`, `AgentReply`, `_run_tools`, `_TurnState`)
- **Relevance:** the runner calls `Agent.ask` once per case run and checks the `AgentReply` it gets back.
- **Key patterns:**
  - `guardrail` records which layer refused (`input: …` or `classifier: …`).
  - `served_by` names the model.
  - `tool_calls` (added in this spec) carries the tool arguments.

### CLI

- **Location:** `src/skyrisk/cli.py` (`_build_agent`, `_chat`, `_log`)
- **Relevance:** `skyrisk eval` follows the same pattern as the other subcommands: `.env` loading, `build_agent`, and a setup error that exits non-zero.

### Live tests and guardrail tests

- **Location:** `tests/test_live.py`, `tests/test_guardrails.py`
- **Relevance:** both already parametrize over `evals/cases.yaml` using `layer`.
- **Key patterns:** new cases flow into these tests automatically.

### Test harness

- **Location:** `tests/fakes.py`, `tests/conftest.py` (`build_scored_db`, `tool_ctx`)
- **Relevance:** scripted `FakeProvider` steps and a synthetic scored DB for offline runner tests.

### Grounding and score lookup

- **Location:** `src/skyrisk/agent/guardrails.py` (`check_grounding`, `GROUNDING_TOLERANCE`), `src/skyrisk/agent/tools.py` (`_latest_run`, `_hazard_scores`)
- **Relevance:** the eval grounding check compares `scores_cited` with the latest score run in the DB, using the same tolerance.
