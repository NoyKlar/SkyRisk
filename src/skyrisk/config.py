"""Pydantic models and loaders for the hub registry and scoring configuration."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from datetime import date
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, Field, model_validator

# Contiguous-US bounding box, used as a sanity check on hub coordinates.
US_LAT = (24.0, 50.0)
US_LON = (-125.0, -66.0)
WEIGHT_TOLERANCE = 1e-9

Region = Literal["Northeast", "Southeast", "Midwest", "South", "West"]


class Hub(BaseModel):
    id: str = Field(pattern=r"^[a-z0-9-]+$")
    name: str
    city: str
    state: str = Field(pattern=r"^[A-Z]{2}$")
    lat: float = Field(ge=US_LAT[0], le=US_LAT[1])
    lon: float = Field(ge=US_LON[0], le=US_LON[1])
    region: Region


class HubRegistry(BaseModel):
    hubs: list[Hub] = Field(min_length=1)

    @model_validator(mode="after")
    def _unique_ids(self) -> HubRegistry:
        ids = [h.id for h in self.hubs]
        dupes = sorted({i for i in ids if ids.count(i) > 1})
        if dupes:
            raise ValueError(f"duplicate hub ids: {dupes}")
        return self

    def get(self, hub_id: str) -> Hub:
        for hub in self.hubs:
            if hub.id == hub_id:
                return hub
        raise KeyError(f"unknown hub id: {hub_id}")


class Window(BaseModel):
    start: date
    end: date

    @model_validator(mode="after")
    def _ordered(self) -> Window:
        if self.end < self.start:
            raise ValueError("window end must not be before start")
        return self

    @property
    def num_days(self) -> int:
        return (self.end - self.start).days + 1


class Thresholds(BaseModel):
    snow_day_cm: float
    heavy_snow_cm: float
    extreme_heat_c: float
    extreme_cold_c: float
    heavy_rain_mm: float
    high_wind_kmh: float


class Hazard(BaseModel):
    weight: float = Field(ge=0, le=1)
    metrics: dict[str, float] = Field(min_length=1)

    @model_validator(mode="after")
    def _metric_weights(self) -> Hazard:
        if any(w < 0 for w in self.metrics.values()):
            raise ValueError("metric weights must be non-negative")
        total = sum(self.metrics.values())
        if abs(total - 1.0) > WEIGHT_TOLERANCE:
            raise ValueError(f"metric weights must sum to 1, got {total}")
        return self


class ScoringConfig(BaseModel):
    version: str
    window: Window
    thresholds: Thresholds
    # Minimum county area used when normalizing NRI frequencies by area, so a few
    # events in a small county do not produce an extreme per-area rate.
    nri_area_floor_sqmi: float = Field(gt=0)
    hazards: dict[str, Hazard] = Field(min_length=1)

    @model_validator(mode="after")
    def _hazard_weights(self) -> ScoringConfig:
        total = sum(h.weight for h in self.hazards.values())
        if abs(total - 1.0) > WEIGHT_TOLERANCE:
            raise ValueError(f"hazard weights must sum to 1, got {total}")
        return self

    @property
    def metric_names(self) -> list[str]:
        return sorted({m for h in self.hazards.values() for m in h.metrics})

    def config_hash(self) -> str:
        canonical = json.dumps(self.model_dump(mode="json"), sort_keys=True)
        return hashlib.sha256(canonical.encode()).hexdigest()


ForecastVariable = Literal["snowfall_cm", "temp_max_c", "temp_min_c", "precip_mm", "wind_gust_max_kmh"]
Level = Literal["low", "medium", "high"]


class NearTermHazard(BaseModel):
    variable: ForecastVariable
    watch: float
    severe: float
    max_points: float = Field(gt=0, le=100)

    @model_validator(mode="after")
    def _ramp(self) -> NearTermHazard:
        if self.severe <= self.watch:
            raise ValueError(f"severe ({self.severe}) must be above watch ({self.watch})")
        return self


class Levels(BaseModel):
    medium: float = Field(gt=0, lt=100)
    high: float = Field(gt=0, le=100)

    @model_validator(mode="after")
    def _ordered(self) -> Levels:
        if self.high <= self.medium:
            raise ValueError("levels.high must be above levels.medium")
        return self


class AlertRules(BaseModel):
    change_threshold: float = Field(gt=0, le=100)


class DemoScenario(BaseModel):
    day_offset: int = Field(ge=0)
    values: dict[ForecastVariable, float] = Field(min_length=1)


class NearTermConfig(BaseModel):
    version: str
    forecast_days: int = Field(ge=1, le=16)
    lead_time_weights: list[float]
    hazards: dict[str, NearTermHazard] = Field(min_length=1)
    levels: Levels
    alerts: AlertRules
    cache_ttl_s: float = Field(gt=0)
    demo: DemoScenario

    @model_validator(mode="after")
    def _consistent(self) -> NearTermConfig:
        if len(self.lead_time_weights) != self.forecast_days:
            raise ValueError(f"lead_time_weights needs {self.forecast_days} entries, got {len(self.lead_time_weights)}")
        if any(not 0 < w <= 1 for w in self.lead_time_weights):
            raise ValueError("lead_time_weights must be in (0, 1]")
        weak = sorted(n for n, h in self.hazards.items() if h.max_points < self.levels.high)
        if weak:  # one hazard at full severity must be able to reach "high" on its own
            raise ValueError(f"max_points below levels.high ({self.levels.high}) for: {weak}")
        if self.demo.day_offset >= self.forecast_days:
            raise ValueError("demo.day_offset must be inside the forecast window")
        return self

    def level_for(self, score: float) -> Level:
        if score >= self.levels.high:
            return "high"
        return "medium" if score >= self.levels.medium else "low"

    def config_hash(self) -> str:
        canonical = json.dumps(self.model_dump(mode="json"), sort_keys=True)
        return hashlib.sha256(canonical.encode()).hexdigest()


class ModelConfig(BaseModel):
    provider: Literal["anthropic", "openai"]
    model: str
    effort: Literal["low", "medium", "high"] = "medium"
    timeout_s: float = Field(default=30.0, gt=0)  # per model call; on timeout the turn moves to the fallback


ClassifierName = Literal["haiku", "jev"]
CLASSIFIER_NAMES: tuple[ClassifierName, ...] = ("haiku", "jev")


class HaikuClassifierConfig(BaseModel):
    model: str


class JevClassifierConfig(BaseModel):
    model: str
    base_url: str
    injection_threshold: float = Field(ge=0, le=1)
    # An in_scope probability inside [low, high] is too uncertain for Jev alone; it escalates to Haiku.
    uncertain_band: tuple[float, float]

    @model_validator(mode="after")
    def _band(self) -> JevClassifierConfig:
        low, high = self.uncertain_band
        if not 0 <= low < high <= 1:
            raise ValueError(f"uncertain_band must satisfy 0 <= low < high <= 1, got {list(self.uncertain_band)}")
        return self


class ClassifierConfig(BaseModel):
    primary: ClassifierName
    fallback: ClassifierName | None = None
    timeout_s: float = Field(gt=0)
    haiku: HaikuClassifierConfig
    jev: JevClassifierConfig | None = None

    @model_validator(mode="after")
    def _chain(self) -> ClassifierConfig:
        if self.primary == self.fallback:
            raise ValueError("classifier fallback must differ from primary")
        if "jev" in self.order and self.jev is None:
            raise ValueError("classifier 'jev' is selected but has no jev settings")
        return self

    @property
    def order(self) -> list[ClassifierName]:
        return [self.primary] + ([self.fallback] if self.fallback else [])


class RateLimitConfig(BaseModel):
    per_ip_per_hour: int = Field(default=20, ge=1)
    global_per_day: int = Field(default=100, ge=1)


class AgentConfig(BaseModel):
    primary: ModelConfig
    fallback: ModelConfig | None = None
    classifier: ClassifierConfig | None = None
    max_tool_rounds: int = Field(ge=1)
    max_input_chars: int = Field(ge=1)
    max_history_turns: int = Field(ge=1)
    max_output_tokens: int = Field(ge=1024)
    rate_limit: RateLimitConfig = RateLimitConfig()

    def with_env_overrides(self, env: Mapping[str, str]) -> AgentConfig:
        data = self.model_dump()
        if model := env.get("SKYRISK_PRIMARY_MODEL"):
            data["primary"]["model"] = model
        if (model := env.get("SKYRISK_FALLBACK_MODEL")) and data["fallback"]:
            data["fallback"]["model"] = model
        if (model := env.get("SKYRISK_CLASSIFIER_MODEL")) and data["classifier"]:
            data["classifier"]["haiku"]["model"] = model
        if (name := env.get("SKYRISK_CLASSIFIER")) and data["classifier"]:
            if name not in CLASSIFIER_NAMES:
                raise ValueError(f"SKYRISK_CLASSIFIER must be one of {list(CLASSIFIER_NAMES)}, got {name!r}")
            c = data["classifier"]
            if name != c["primary"]:  # the chosen classifier leads; the previous primary becomes its fallback
                c["primary"], c["fallback"] = name, c["primary"]
        return AgentConfig.model_validate(data)


def load_hubs(path: Path) -> HubRegistry:
    return HubRegistry.model_validate(yaml.safe_load(path.read_text()))


def load_scoring_config(path: Path) -> ScoringConfig:
    return ScoringConfig.model_validate(yaml.safe_load(path.read_text()))


def load_near_term_config(path: Path) -> NearTermConfig:
    return NearTermConfig.model_validate(yaml.safe_load(path.read_text()))


def load_agent_config(path: Path) -> AgentConfig:
    return AgentConfig.model_validate(yaml.safe_load(path.read_text()))
