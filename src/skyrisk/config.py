"""Pydantic models and loaders for the hub registry and scoring configuration."""

from __future__ import annotations

import hashlib
import json
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


def load_hubs(path: Path) -> HubRegistry:
    return HubRegistry.model_validate(yaml.safe_load(path.read_text()))


def load_scoring_config(path: Path) -> ScoringConfig:
    return ScoringConfig.model_validate(yaml.safe_load(path.read_text()))
