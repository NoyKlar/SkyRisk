"""Deterministic scoring engine: min-max normalize, weight, rank. No I/O."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping

from pydantic import BaseModel

from skyrisk.config import ScoringConfig


class MetricScore(BaseModel):
    metric: str
    raw: float
    normalized: float  # 0–100 relative to the other hubs
    contribution: float  # points this metric adds to the overall score


class HazardScore(BaseModel):
    hazard: str
    weight: float
    sub_score: float  # 0–100
    metrics: list[MetricScore]


class HubScore(BaseModel):
    hub_id: str
    overall: float  # 0–100
    rank: int  # 1 = highest risk
    hazards: list[HazardScore]


class ScoreResult(BaseModel):
    config_version: str
    config_hash: str
    data_hash: str
    hubs: list[HubScore]  # ordered by rank
    inputs: dict[str, dict[str, float]]  # hub_id -> every computed metric, scored or not
    scored_metrics: list[str]
    notes: dict[str, dict[str, str]] = {}  # hub_id -> metric -> note


def min_max(values: Mapping[str, float]) -> dict[str, float]:
    """Scale values to 0–100 across hubs. If all are equal, every hub gets 0."""
    lo, hi = min(values.values()), max(values.values())
    if hi == lo:
        return {k: 0.0 for k in values}
    return {k: (v - lo) / (hi - lo) * 100.0 for k, v in values.items()}


def data_hash(metrics_by_hub: Mapping[str, Mapping[str, float]]) -> str:
    canonical = json.dumps(metrics_by_hub, sort_keys=True)
    return hashlib.sha256(canonical.encode()).hexdigest()


def score(
    metrics_by_hub: Mapping[str, Mapping[str, float]],
    config: ScoringConfig,
    notes: Mapping[str, Mapping[str, str]] | None = None,
) -> ScoreResult:
    if not metrics_by_hub:
        raise ValueError("no hubs to score")
    for hub_id, metrics in metrics_by_hub.items():
        missing = [m for m in config.metric_names if m not in metrics]
        if missing:
            raise ValueError(f"hub {hub_id} is missing metrics: {missing}")

    normalized = {
        m: min_max({hub: metrics[m] for hub, metrics in metrics_by_hub.items()})
        for m in config.metric_names
    }

    unranked: list[HubScore] = []
    for hub_id, metrics in metrics_by_hub.items():
        hazards: list[HazardScore] = []
        overall = 0.0
        for name, hazard in config.hazards.items():
            metric_scores = [
                MetricScore(
                    metric=m,
                    raw=metrics[m],
                    normalized=normalized[m][hub_id],
                    contribution=normalized[m][hub_id] * w * hazard.weight,
                )
                for m, w in hazard.metrics.items()
            ]
            sub = sum(normalized[m][hub_id] * w for m, w in hazard.metrics.items())
            overall += sub * hazard.weight
            hazards.append(
                HazardScore(hazard=name, weight=hazard.weight, sub_score=sub, metrics=metric_scores)
            )
        unranked.append(HubScore(hub_id=hub_id, overall=overall, rank=0, hazards=hazards))

    ordered = sorted(unranked, key=lambda h: (-h.overall, h.hub_id))
    ranked = [h.model_copy(update={"rank": i}) for i, h in enumerate(ordered, start=1)]

    return ScoreResult(
        config_version=config.version,
        config_hash=config.config_hash(),
        data_hash=data_hash(metrics_by_hub),
        hubs=ranked,
        inputs={hub: dict(metrics) for hub, metrics in metrics_by_hub.items()},
        scored_metrics=config.metric_names,
        notes={k: dict(v) for k, v in (notes or {}).items()},
    )
