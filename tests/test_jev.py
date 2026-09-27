import json

import httpx
import pytest

from skyrisk.agent.guardrails import CallUsage, Verdict
from skyrisk.agent.jev import JevClassifier, JevResponseError, build_request, parse_response
from skyrisk.config import JevClassifierConfig

CFG = JevClassifierConfig(model="jev-1.13.0", base_url="https://jev.test", injection_threshold=0.5,
                          uncertain_band=(0.4, 0.6))


def _payload(in_scope, injection, model="jev-1.13.0"):
    return {"model": model,
            "answers": {"in_scope": {"type": "noul", "noul": in_scope},
                        "injection": {"type": "noul", "noul": injection}},
            "usage": {"input_tokens": 500, "output_tokens": 40}}


def _client(handler):
    return httpx.Client(transport=httpx.MockTransport(handler))


def _serving(payload, seen=None):
    def handler(request):
        if seen is not None:
            seen.append(request)
        return httpx.Response(200, json=payload)
    return _client(handler)


class _Escalation:
    name = "fake:haiku"

    def __init__(self, label="off_topic", error=None):
        self.label, self.error, self.calls = label, error, 0

    def classify(self, text):
        self.calls += 1
        if self.error:
            raise self.error
        return Verdict(label=self.label, reason="haiku says so", decided_by="haiku",
                       usage=[CallUsage(model="claude-haiku-4-5", input_tokens=600, output_tokens=50)])


def test_request_matches_the_recorded_call(fixtures):
    sent = json.loads((fixtures / "jev_in_scope_request.json").read_text())
    assert build_request("Which hub has the most snow days?", CFG) == sent


def test_recorded_response_parses(fixtures):
    a = parse_response(json.loads((fixtures / "jev_in_scope.json").read_text()), "jev-1.13.0")
    assert (a.in_scope, a.injection) == (0.92, 0.03)
    assert a.usage == CallUsage(model="jev-1.13.0", input_tokens=499, output_tokens=39)


@pytest.mark.parametrize("payload", [
    {},
    {**_payload(0.9, 0.1), "answers": {"in_scope": {"type": "noul", "noul": 0.9}}},  # missing injection
    _payload(1.2, 0.1),                                                              # probability out of range
    {**_payload(0.9, 0.1), "answers": {"in_scope": {"type": "bool", "bool": True},
                                       "injection": {"type": "noul", "noul": 0.1}}},  # wrong answer type
    _payload(0.9, 0.1, model="jev-2.0.0"),                                           # unpinned model
])
def test_malformed_responses_fail_loud(payload):
    with pytest.raises(JevResponseError):
        parse_response(payload, "jev-1.13.0")


def test_call_sends_key_and_question_as_state():
    seen = []
    v = JevClassifier(_serving(_payload(0.92, 0.03), seen), "secret", CFG).classify("Snow in Denver?")
    assert seen[0].url == "https://jev.test/v1/systemone"
    assert seen[0].headers["Authorization"] == "Bearer secret"
    assert json.loads(seen[0].content)["state"] == "Snow in Denver?"
    assert (v.label, v.decided_by) == ("in_scope", "jev")
    assert v.reason == "jev in_scope=0.92 injection=0.03"


@pytest.mark.parametrize("in_scope, injection, label", [
    (0.95, 0.70, "injection"),  # injection wins even when in scope
    (0.60, 0.10, "in_scope"),   # band edges are confident
    (0.40, 0.10, "off_topic"),
    (0.05, 0.49, "off_topic"),
])
def test_confident_cases_are_decided_by_jev(in_scope, injection, label):
    escalate = _Escalation()
    v = JevClassifier(_serving(_payload(in_scope, injection)), "k", CFG, escalate=escalate).classify("q")
    assert (v.label, v.decided_by, escalate.calls) == (label, "jev", 0)


def test_uncertain_band_escalates_and_combines_usage():
    escalate = _Escalation(label="off_topic")
    v = JevClassifier(_serving(_payload(0.5, 0.1)), "k", CFG, escalate=escalate).classify("q")
    assert (v.label, v.decided_by, escalate.calls) == ("off_topic", "jev→haiku", 1)
    assert [u.model for u in v.usage] == ["jev-1.13.0", "claude-haiku-4-5"]
    assert v.reason.startswith("jev in_scope=0.50")


@pytest.mark.parametrize("escalate", [None, _Escalation(error=TimeoutError("slow"))])
def test_uncertain_without_working_escalation_is_in_scope(escalate):
    logs = []
    v = JevClassifier(_serving(_payload(0.5, 0.1)), "k", CFG, escalate=escalate, log=logs.append).classify("q")
    assert (v.label, v.decided_by) == ("in_scope", "jev(band, no escalation)")
    assert bool(logs) == (escalate is not None)


def test_http_errors_and_timeouts_raise_without_retry():
    calls = {"n": 0}

    def unavailable(request):
        calls["n"] += 1
        return httpx.Response(529)

    with pytest.raises(httpx.HTTPStatusError):
        JevClassifier(_client(unavailable), "k", CFG).classify("q")
    assert calls["n"] == 1

    def timeout(request):
        raise httpx.ReadTimeout("slow", request=request)

    with pytest.raises(httpx.ReadTimeout):
        JevClassifier(_client(timeout), "k", CFG).classify("q")


# --- factory wiring ---------------------------------------------------------------------

def _classifier_config(primary="haiku", fallback="jev"):
    from skyrisk.config import ClassifierConfig

    return ClassifierConfig(primary=primary, fallback=fallback, timeout_s=5.0, haiku={"model": "claude-haiku-4-5"},
                            jev=CFG)


def test_factory_builds_the_configured_chain():
    from skyrisk.agent.factory import build_classifier

    env = {"ANTHROPIC_API_KEY": "a", "JEV_API_KEY": "j"}
    chain = build_classifier(_classifier_config(), env, log=lambda m: None)
    assert chain.name == "anthropic:claude-haiku-4-5 → fallback jev:jev-1.13.0 (band → anthropic:claude-haiku-4-5)"
    chain = build_classifier(_classifier_config("jev", "haiku"), env, log=lambda m: None)
    assert chain.name.startswith("jev:jev-1.13.0 (band → anthropic:")


def test_factory_drops_classifiers_without_a_key():
    from skyrisk.agent.factory import build_classifier

    logs = []
    chain = build_classifier(_classifier_config(), {"ANTHROPIC_API_KEY": "a"}, log=logs.append)
    assert chain.name == "anthropic:claude-haiku-4-5"
    assert logs == ["warning: no JEV_API_KEY for the jev classifier; running without it"]
    assert build_classifier(_classifier_config(), {}, log=lambda m: None) is None
