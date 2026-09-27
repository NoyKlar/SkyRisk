"""Wire real providers and the classifier from config + environment. Only the CLI calls this."""

from __future__ import annotations

import sqlite3
from collections.abc import Mapping
from pathlib import Path

import anthropic
import openai

from skyrisk.agent.core import Agent, Log
from skyrisk.agent.guardrails import Classifier, HaikuClassifier
from skyrisk.agent.prompts import build_system_prompt
from skyrisk.agent.providers.anthropic_provider import AnthropicProvider
from skyrisk.agent.providers.base import LLMProvider
from skyrisk.agent.providers.openai_provider import OpenAIProvider
from skyrisk.agent.tools import ToolContext
from skyrisk.config import AgentConfig, ModelConfig, load_agent_config, load_hubs, load_scoring_config


class AgentSetupError(RuntimeError):
    pass


def _provider(cfg: ModelConfig, env: Mapping[str, str], max_tokens: int) -> LLMProvider:
    if cfg.provider == "anthropic":
        return AnthropicProvider(anthropic.Anthropic(), cfg.model, cfg.effort, max_tokens)
    return OpenAIProvider(openai.OpenAI(api_key=env["OPENAI_API_KEY"]), cfg.model, cfg.effort, max_tokens)


def _has_key(cfg: ModelConfig, env: Mapping[str, str]) -> bool:
    return bool(env.get("ANTHROPIC_API_KEY" if cfg.provider == "anthropic" else "OPENAI_API_KEY"))


def build_agent(conn: sqlite3.Connection, config_dir: Path, env: Mapping[str, str],
                log: Log = print) -> tuple[Agent, AgentConfig]:
    config = load_agent_config(config_dir / "agent.yaml").with_env_overrides(env)
    registry = load_hubs(config_dir / "hubs.yaml")
    scoring = load_scoring_config(config_dir / "scoring.yaml")

    if not _has_key(config.primary, env):
        raise AgentSetupError(f"Missing API key for the primary provider ({config.primary.provider}).")
    providers = [_provider(config.primary, env, config.max_output_tokens)]
    if config.fallback is not None:
        if _has_key(config.fallback, env):
            providers.append(_provider(config.fallback, env, config.max_output_tokens))
        else:
            log(f"warning: no API key for the fallback provider ({config.fallback.provider}); "
                "running without a fallback")

    classifier: Classifier | None = None
    if config.classifier is not None and env.get("ANTHROPIC_API_KEY"):
        classifier = HaikuClassifier(anthropic.Anthropic(), config.classifier.model, config.classifier.timeout_s)

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
