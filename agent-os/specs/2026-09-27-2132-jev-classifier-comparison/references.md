# References for Jev Classifier + Comparison

## Similar Implementations

### Haiku classifier
- **Location:** `src/skyrisk/agent/guardrails.py` (`Verdict`, `Classifier`, `HaikuClassifier`, `safe_classify`)
- **Relevance:** Jev implements the same `Classifier` protocol. `safe_classify` already fails open.
- **Key patterns:** the question is data, not instructions; the timeout is short and there are no retries.

### HTTP retry helper
- **Location:** `src/skyrisk/ingest/http.py` (`get_json`)
- **Relevance:** `post_json` shares the same retry loop.

### Eval runner + report
- **Location:** `src/skyrisk/evals/runner.py`, `src/skyrisk/evals/report.py`
- **Relevance:** The classifier benchmark mirrors the case filtering, repeat/aggregate, percentile latency and the `<stamp>` + `latest` report writing.

### Factory wiring
- **Location:** `src/skyrisk/agent/factory.py`
- **Relevance:** Builds classifiers from config + env keys, dropping any whose key is missing, with a warning.

### MockTransport tests
- **Location:** `tests/test_ingest.py` (`_fake_api`)
- **Relevance:** The pattern for testing the Jev client offline.
