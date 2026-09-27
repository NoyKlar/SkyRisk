"""Live smoke tests against the real providers. Opt-in: `uv run pytest -m live` (needs API keys in .env)."""

import os
from pathlib import Path

import anthropic
import openai
import pytest
import yaml
from dotenv import load_dotenv

from fakes import FakeProvider
from skyrisk.agent.core import Agent, Conversation
from skyrisk.agent.guardrails import HaikuClassifier
from skyrisk.agent.prompts import build_system_prompt
from skyrisk.agent.providers.anthropic_provider import AnthropicProvider
from skyrisk.agent.providers.openai_provider import OpenAIProvider
from skyrisk.config import load_agent_config

pytestmark = pytest.mark.live
ROOT = Path(__file__).resolve().parents[1]
load_dotenv(ROOT / ".env")
CONFIG = load_agent_config(ROOT / "config" / "agent.yaml").with_env_overrides(os.environ)
CASES = yaml.safe_load((ROOT / "evals" / "cases.yaml").read_text())["cases"]

needs_anthropic = pytest.mark.skipif(not os.environ.get("ANTHROPIC_API_KEY"), reason="ANTHROPIC_API_KEY not set")
needs_openai = pytest.mark.skipif(not os.environ.get("OPENAI_API_KEY"), reason="OPENAI_API_KEY not set")


def _agent(tool_ctx, providers):
    return Agent(tool_ctx, providers, build_system_prompt(tool_ctx.registry, tool_ctx.scoring))


def _assert_grounded_answer(reply):
    assert reply.status == "answered", reply
    assert "rank_hubs" in reply.tools_used
    assert reply.limitations


@needs_anthropic
def test_claude_turn(tool_ctx):
    provider = AnthropicProvider(anthropic.Anthropic(), CONFIG.primary.model, CONFIG.primary.effort)
    reply = _agent(tool_ctx, [provider]).ask(
        Conversation(), "Which hubs in the Midwest are most exposed to winter disruption?")
    _assert_grounded_answer(reply)
    assert reply.served_by == f"anthropic:{CONFIG.primary.model}"


@needs_openai
def test_openai_fallback_turn(tool_ctx):
    fallback = OpenAIProvider(openai.OpenAI(), CONFIG.fallback.model, CONFIG.fallback.effort)
    reply = _agent(tool_ctx, [FakeProvider(unavailable=True), fallback]).ask(
        Conversation(), "Which hubs in the Midwest are most exposed to winter disruption?")
    _assert_grounded_answer(reply)
    assert reply.served_by == f"openai:{CONFIG.fallback.model}"


@needs_anthropic
@pytest.mark.parametrize("case", [c for c in CASES if c.get("layer") in ("none", "classifier")],
                         ids=lambda c: c["id"])
def test_classifier_on_eval_cases(case):
    classifier = HaikuClassifier(anthropic.Anthropic(), CONFIG.classifier.model, CONFIG.classifier.timeout_s)
    verdict = classifier.classify(case["question"])
    expected = {"answered": "in_scope", "refused_off_topic": "off_topic", "refused_injection": "injection"}
    assert verdict.label == expected[case["expect"]], verdict.reason
