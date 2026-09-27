# Evals + Deliverables — Shaping Notes

## Scope

Roadmap Phase 1, items 10 and 12, in the user's words:

1. **Eval runner:** one command runs `evals/cases.yaml` against the real agent and checks each case: status, expected tool and arguments, must_mention, and grounding. It supports repeated runs per case to measure consistency (e.g. the classifier false-positive rate). It writes a results report with the pass rate per category, failures with reasons, latency, and the model used.
2. **New eval cases:** Hebrew questions (a normal question, an injection, and off-topic requests such as "what weather is best for growing bananas"). Earlier manual tests showed the classifier is inconsistent on off-topic agricultural questions.
3. **README:** complete run instructions from a fresh clone: install, .env, ingest, score, chat, serve, tests and evals.
4. **Design document** with these sections: system architecture, repository structure, data storage choice, scoring methodology, why an LLM is needed, the system prompt, the evaluation set and results, and key tradeoffs. It also covers assumptions, uncertainty and scope (English-only by scope, tested in Hebrew).

## Decisions

- **Entry point:** `skyrisk eval`, a CLI subcommand, with the flags `--repeat N`, `--case GLOB`, `--category NAME` and `--out DIR`.
- **Exit codes:** 0 = all cases passed, 1 = any case failed, 2 = setup error. The command can therefore gate prompt/model changes and later run in CI. Documented in the README and DESIGN.md.
- **Repeats:** a case passes only if all N runs pass. The per-case pass rate is reported and flaky cases are flagged.
- **Hebrew:** a Hebrew in-scope question must be `answered` in any language, and only the status and tool are checked. Hebrew injection and off-topic questions must be refused.
- **Isolation:** each run uses a fresh `Conversation`.
- **Reports:** timestamped `.md`/`.json` files in `evals/results/` are gitignored. `latest.md` and `latest.json` are committed and linked from DESIGN.md.
- **Design doc:** `docs/DESIGN.md`.
- **Classifier:** its inconsistency is measured and documented. Tuning it is a follow-up.
- **Tool arguments:** `AgentReply` gains a `tool_calls` field (name + arguments) so the runner can check expected arguments. The API response is unchanged.
- **Wording:** the headline example questions form the `core_examples` category (ids `core-*`). Docs refer to the "required design sections".
- **Multi-status cases (decided after the initial `--repeat 1` run):**
  - `expect` may be a list of statuses, and any of them passes.
  - `denver-snow-out-of-window` now accepts `answered` or `needs_clarification`. The agent correctly explains the 2016–2025 window and offers an in-window year, which it labels a clarification.
  - The "2016" mention check stays.
  - The change is documented in DESIGN.md §7.
- **Eval-driven prompt fix:**
  - The first `--repeat 3` run found `fp-system-word` flaky (2/3). The primary model asked for clarification on a methodology question.
  - One sentence was added to the system prompt's Scope section, saying questions about how scores are computed are in scope.
  - A full `--repeat 3` re-run passed 33/33 (99/99 runs) with no regressions.
  - The case stayed strict.
  - DESIGN.md §7 has the before/after results.
- **Cost checkpoint:** before the full `--repeat 3` run, the estimated number of API calls is given to the user, and the run waits for their go-ahead.

## Context

- **Visuals:** None.
- **References:**
  - `agent/core.py`
  - `cli.py`
  - `tests/test_live.py`
  - `tests/test_guardrails.py`
  - `tests/fakes.py`
  - `tests/conftest.py`
  - `agent/guardrails.py`
  - `agent/tools.py`

  See references.md.
- **Product alignment:**
  - mission.md says "an eval set that every prompt/model change must pass", which the non-zero exit code enforces.
  - tech-stack.md names a "custom eval runner script".
  - Scores are relative rankings, not probabilities.

## Standards Applied

- testing/no-live-network: runner tests use `FakeProvider` and a synthetic scored DB. The real eval run is opt-in, because it costs API credits.
- testing/injectable-side-effects: `run_evals` takes `agent`, `now` and `log` as parameters.
