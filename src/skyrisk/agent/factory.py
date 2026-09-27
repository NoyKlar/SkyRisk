"""Wire real providers and the classifier from config + environment. Only the CLI calls this."""

from __future__ import annotations

import sqlite3
from collections.abc import Mapping
from pathlib import Path

import anthropic
import httpx
import openai

from skyrisk.agent.core import Agent, Log
from skyrisk.agent.guardrails import Classifier, FallbackClassifier, HaikuClassifier
from skyrisk.agent.jev import JevClassifier
from skyrisk.agent.prompts import build_system_prompt
from skyrisk.agent.providers.anthropic_provider import AnthropicProvider
from skyrisk.agent.providers.base import LLMProvider, ProviderUnavailable
from skyrisk.agent.providers.openai_provider import OpenAIProvider
from skyrisk.agent.tools import ToolContext
from skyrisk.config import (
    AgentConfig,
    ClassifierConfig,
    ClassifierName,
    ModelConfig,
    load_agent_config,
    load_hubs,
    load_scoring_config,
)

CLASSIFIER_KEYS: dict[ClassifierName, str] = {"haiku": "ANTHROPIC_API_KEY", "jev": "JEV_API_KEY"}
CLASSIFIER_VENDORS: dict[ClassifierName, str] = {"haiku": "anthropic", "jev": "typesafe"}
OUTAGE_VENDORS = ("anthropic", "openai")  # vendors whose outage can be simulated (--simulate-outage)


class AgentSetupError(RuntimeError):
    pass


class _DownProvider:
    """Stands in for a provider during a simulated outage: every turn fails as unavailable."""

    def __init__(self, name: str) -> None:
        self.name = name

    def start_turn(self, *args: object, **kwargs: object) -> None:
        raise ProviderUnavailable(f"{self.name}: simulated outage")


class _DownClassifier:
    """Stands in for a classifier during a simulated outage: every call fails, so the chain moves on."""

    def __init__(self, name: str) -> None:
        self.name = name

    def classify(self, text: str) -> None:
        raise ConnectionError("simulated outage")


def _provider(cfg: ModelConfig, env: Mapping[str, str], max_tokens: int) -> LLMProvider:
    if cfg.provider == "anthropic":
        return AnthropicProvider(anthropic.Anthropic(), cfg.model, cfg.effort, max_tokens)
    return OpenAIProvider(openai.OpenAI(api_key=env["OPENAI_API_KEY"]), cfg.model, cfg.effort, max_tokens)


def _has_key(cfg: ModelConfig, env: Mapping[str, str]) -> bool:
    return bool(env.get("ANTHROPIC_API_KEY" if cfg.provider == "anthropic" else "OPENAI_API_KEY"))


def build_agent(conn: sqlite3.Connection, config_dir: Path, env: Mapping[str, str],
                log: Log = print, *, simulate_outage: str | None = None) -> tuple[Agent, AgentConfig]:
    """`simulate_outage` names a vendor (e.g. "anthropic") whose models all fail on every call, so the
    agent takes its real outage path. For evals and manual testing only."""
    config = load_agent_config(config_dir / "agent.yaml").with_env_overrides(env)
    registry = load_hubs(config_dir / "hubs.yaml")
    scoring = load_scoring_config(config_dir / "scoring.yaml")

    def provider_or_down(cfg: ModelConfig) -> LLMProvider:
        if cfg.provider == simulate_outage:
            return _DownProvider(f"{cfg.provider}:{cfg.model}")
        return _provider(cfg, env, config.max_output_tokens)

    if config.primary.provider != simulate_outage and not _has_key(config.primary, env):
        raise AgentSetupError(f"Missing API key for the primary provider ({config.primary.provider}).")
    providers = [provider_or_down(config.primary)]
    if config.fallback is not None:
        if config.fallback.provider == simulate_outage or _has_key(config.fallback, env):
            providers.append(provider_or_down(config.fallback))
        else:
            log(f"warning: no API key for the fallback provider ({config.fallback.provider}); "
                "running without a fallback")
    if all(isinstance(p, _DownProvider) for p in providers):
        raise AgentSetupError(f"With a simulated {simulate_outage} outage, no configured provider can answer "
                              "(set up the fallback provider and its API key).")

    classifier = build_classifier(config.classifier, env, log, down=simulate_outage) if config.classifier else None

    agent = Agent(
        ToolContext(conn, registry, scoring),
        providers,
        build_system_prompt(registry, scoring),
        classifier=classifier,
        max_tool_rounds=config.max_tool_rounds,
        max_input_chars=config.max_input_chars,
        log=log,
    )
    return agent, config


def build_classifiers(cfg: ClassifierConfig, env: Mapping[str, str], log: Log = print, *,
                      down: str | None = None) -> dict[ClassifierName, Classifier]:
    """Every configured classifier that has an API key. Jev escalates its uncertain band to Haiku when available.

    A classifier whose vendor is `down` (simulated outage) is built as a stand-in that always fails.
    """
    built: dict[ClassifierName, Classifier] = {}
    if CLASSIFIER_VENDORS["haiku"] == down:
        built["haiku"] = _DownClassifier(f"anthropic:{cfg.haiku.model}")
    elif env.get(CLASSIFIER_KEYS["haiku"]):
        built["haiku"] = HaikuClassifier(anthropic.Anthropic(), cfg.haiku.model, cfg.timeout_s)
    if cfg.jev is not None and CLASSIFIER_VENDORS["jev"] == down:
        built["jev"] = _DownClassifier(f"jev:{cfg.jev.model}")
    elif cfg.jev is not None and (key := env.get(CLASSIFIER_KEYS["jev"])):
        built["jev"] = JevClassifier(httpx.Client(timeout=cfg.timeout_s), key, cfg.jev,
                                     escalate=built.get("haiku"), log=log)
    return built


def build_classifier(cfg: ClassifierConfig, env: Mapping[str, str], log: Log = print, *,
                     down: str | None = None) -> Classifier | None:
    """The configured chain [primary, fallback], dropping (with a warning) any classifier without an API key."""
    available = build_classifiers(cfg, env, log, down=down)
    chain = []
    for name in cfg.order:
        if name in available:
            chain.append(available[name])
        else:
            log(f"warning: no {CLASSIFIER_KEYS[name]} for the {name} classifier; running without it")
    if not chain:
        return None
    return chain[0] if len(chain) == 1 else FallbackClassifier(chain, log)
