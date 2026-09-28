"""Pure near-term scoring: a hub's daily forecast + config -> an absolute 0-100 score. No I/O."""

from __future__ import annotations

from datetime import date

from pydantic import BaseModel

from skyrisk.config import Level, NearTermConfig
from skyrisk.models import WeatherDay

SCORE_DECIMALS = 2


class HazardForecast(BaseModel):
    hazard: str
    variable: str
    peak_value: float | None  # the day's raw value that produced the worst weighted severity
    peak_date: date | None
    severity: float  # 0-1, after the lead-time weight
    points: float


class NearTermScore(BaseModel):
    score: float
    level: Level
    forecast_start: date
    forecast_end: date
    hazards: list[HazardForecast]
    notes: list[str]


def day_severity(value: float, watch: float, severe: float) -> float:
    """0 at or below `watch`, 1 at or above `severe`, linear in between."""
    return min(1.0, max(0.0, (value - watch) / (severe - watch)))


def score_forecast(days: list[WeatherDay], cfg: NearTermConfig) -> NearTermScore:
    if not days:
        raise ValueError("forecast has no days")
    days = sorted(days, key=lambda d: d.date)[: cfg.forecast_days]
    hazards = []
    notes = []
    for name, hz in cfg.hazards.items():
        best = (0.0, None, None)  # (severity, value, date)
        missing = 0
        for i, day in enumerate(days):
            value = getattr(day, hz.variable)
            if value is None:
                missing += 1
                continue
            sev = day_severity(value, hz.watch, hz.severe) * cfg.lead_time_weights[i]
            if sev > best[0] or best[1] is None:
                best = (sev, value, day.date)
        if missing:
            notes.append(f"{name}: {missing} forecast day(s) had no {hz.variable} value and count as no risk.")
        severity, value, when = best
        hazards.append(HazardForecast(
            hazard=name, variable=hz.variable, peak_value=value, peak_date=when,
            severity=round(severity, 4), points=round(hz.max_points * severity, SCORE_DECIMALS),
        ))
    score = round(min(100.0, sum(h.points for h in hazards)), SCORE_DECIMALS)
    return NearTermScore(
        score=score, level=cfg.level_for(score), forecast_start=days[0].date, forecast_end=days[-1].date,
        hazards=hazards, notes=notes,
    )


def apply_demo(days: list[WeatherDay], cfg: NearTermConfig) -> list[WeatherDay]:
    """The forecast with the configured demo storm merged into day `demo.day_offset` (max of real and demo)."""
    days = sorted(days, key=lambda d: d.date)
    out = list(days)
    i = min(cfg.demo.day_offset, len(days) - 1)
    patch = {var: max(v, getattr(days[i], var) or v) for var, v in cfg.demo.values.items()}
    out[i] = days[i].model_copy(update=patch)
    return out
