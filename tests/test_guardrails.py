from pathlib import Path

import pytest
import yaml

from skyrisk.agent.guardrails import (
    ClassifierChainError,
    FallbackClassifier,
    Verdict,
    check_grounding,
    check_input,
    safe_classify,
    tool_numbers,
)
from skyrisk.agent.schema import AgentAnswer
from skyrisk.agent.tools import ScoreRef

CASES = yaml.safe_load((Path(__file__).resolve().parents[1] / "evals" / "cases.yaml").read_text())["cases"]


@pytest.mark.parametrize("case", [c for c in CASES if c.get("layer") == "deterministic"], ids=lambda c: c["id"])
def test_deterministic_layer_blocks_injections(case):
    rejection = check_input(case["question"], 2000)
    assert rejection is not None and rejection.reason == "injection"


@pytest.mark.parametrize("case", [c for c in CASES if c.get("layer") != "deterministic"], ids=lambda c: c["id"])
def test_deterministic_layer_passes_everything_else(case):
    # Includes the false-positive look-alikes and the cases left to the classifier.
    assert check_input(case["question"], 2000) is None


@pytest.mark.parametrize("text,reason", [
    ("   ", "empty"),
    ("x" * 2001, "too_long"),
    ("rank hubs​ by snow", "control_chars"),
])
def test_input_hygiene(text, reason):
    assert check_input(text, 2000).reason == reason


def _answer(text, cited=()):
    return AgentAnswer(status="answered", answer=text, hubs_referenced=[],
                       scores_cited=[{"hub_id": h, "hazard": z, "score": s} for h, z, s in cited],
                       assumptions_and_limitations=["x"], data_sources=[])


def test_grounding_accepts_rounded_tool_values():
    scores = [ScoreRef(hub_id="denver", hazard="winter", score=67.52)]
    numbers = tool_numbers(['{"rows": [{"score": 67.52, "value": 4.93}]}'])
    answer = _answer("Denver scores 67.52 on winter; 4.9% of days had snow in 2025.", [("denver", "winter", 67.52)])
    assert check_grounding(answer, scores, numbers) == []


def test_grounding_flags_wrong_hazard_and_invented_numbers():
    scores = [ScoreRef(hub_id="denver", hazard="winter", score=67.52)]
    answer = _answer("Denver scores 12.34 on heat.", [("denver", "heat", 12.34)])
    problems = check_grounding(answer, scores, [67.52])
    assert any("no tool returned" in p for p in problems)


# --- classifier fallback chain -----------------------------------------------------------

class _Classifier:
    def __init__(self, name, label="in_scope", error=None):
        self.name, self.label, self.error, self.calls = name, label, error, 0

    def classify(self, text):
        self.calls += 1
        if self.error:
            raise self.error
        return Verdict(label=self.label, reason=self.name, decided_by=self.name)


def test_fallback_chain_uses_the_primary_when_it_works():
    primary, secondary = _Classifier("a", "off_topic"), _Classifier("b")
    chain = FallbackClassifier([primary, secondary], log=lambda m: None)
    assert chain.classify("q").decided_by == "a"
    assert (secondary.calls, chain.name) == (0, "a → fallback b")


def test_fallback_chain_moves_on_when_the_primary_fails():
    logs = []
    chain = FallbackClassifier([_Classifier("a", error=TimeoutError("slow")), _Classifier("b", "injection")],
                               log=logs.append)
    assert chain.classify("q").label == "injection"
    assert logs == ["warning: classifier a failed (TimeoutError: slow)"]


def test_fallback_chain_all_failing_is_skipped_by_safe_classify():
    chain = FallbackClassifier([_Classifier("a", error=OSError("down")), _Classifier("b", error=OSError("down"))],
                               log=lambda m: None)
    with pytest.raises(ClassifierChainError):
        chain.classify("q")
    logs = []
    assert safe_classify(chain, "q", logs.append) is None
    assert "skipping" in logs[-1]
