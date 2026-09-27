"""Model prices for eval cost estimates, in USD per million tokens.

Sources, checked 2026-09-27:
- Anthropic: API price list (claude-api skill model table, cached 2026-06-24). Cache reads are 0.1x the
  input price and 5-minute cache writes 1.25x.
- OpenAI gpt-6-luna: developers.openai.com/api/docs/pricing, standard tier, short context.
- TypeSafe Jev: docs.typesafe.ai pricing; output tokens are free.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass

from skyrisk.agent.providers.base import CallUsage


@dataclass(frozen=True)
class Price:
    input: float
    output: float
    cache_read: float
    cache_write: float


PRICES: dict[str, Price] = {
    "claude-sonnet-5": Price(input=2.00, output=10.00, cache_read=0.20, cache_write=2.50),
    "claude-haiku-4-5": Price(input=1.00, output=5.00, cache_read=0.10, cache_write=1.25),
    "gpt-6-luna": Price(input=0.10, output=0.50, cache_read=0.01, cache_write=0.125),
    "jev-1.13.0": Price(input=0.042, output=0.0, cache_read=0.042, cache_write=0.042),
}


def call_cost(u: CallUsage) -> float:
    """USD for one call. Raises KeyError for a model with no price."""
    p = PRICES[u.model]
    return (u.input_tokens * p.input + u.output_tokens * p.output
            + u.cache_read_tokens * p.cache_read + u.cache_write_tokens * p.cache_write) / 1_000_000


def total_cost(usage: Iterable[CallUsage]) -> tuple[float, list[str]]:
    """USD for the priced calls, and the sorted list of models that have no price (not included)."""
    total, unpriced = 0.0, set()
    for u in usage:
        if u.model in PRICES:
            total += call_cost(u)
        else:
            unpriced.add(u.model)
    return total, sorted(unpriced)
