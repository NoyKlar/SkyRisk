"""Pure functions turning raw weather/hazard records into scoring metrics."""

from __future__ import annotations

from collections.abc import Callable, Sequence

from skyrisk.config import Thresholds
from skyrisk.models import NRI_AREA_NORMALIZED, NRI_HAZARDS, NriCounty, WeatherDay

DAYS_PER_YEAR = 365.25

Predicate = Callable[[float], bool]


def _valid_values(rows: Sequence[WeatherDay], field: str) -> list[float]:
    return [v for r in rows if (v := getattr(r, field)) is not None]


def pct_days(rows: Sequence[WeatherDay], field: str, predicate: Predicate) -> float:
    """Percent of days with data for `field` on which `predicate` holds (0–100)."""
    values = _valid_values(rows, field)
    if not values:
        raise ValueError(f"no non-null values for {field}")
    return 100.0 * sum(1 for v in values if predicate(v)) / len(values)


def days_per_year(rows: Sequence[WeatherDay], field: str, predicate: Predicate) -> float:
    """Average matching days per year, annualized over days that have data."""
    return pct_days(rows, field, predicate) / 100.0 * DAYS_PER_YEAR


def weather_metrics(rows: Sequence[WeatherDay], t: Thresholds) -> dict[str, float]:
    return {
        "snow_days": days_per_year(rows, "snowfall_cm", lambda v: v >= t.snow_day_cm),
        "heavy_snow_days": days_per_year(rows, "snowfall_cm", lambda v: v >= t.heavy_snow_cm),
        "extreme_heat_days": days_per_year(rows, "temp_max_c", lambda v: v >= t.extreme_heat_c),
        "extreme_cold_days": days_per_year(rows, "temp_min_c", lambda v: v <= t.extreme_cold_c),
        "heavy_rain_days": days_per_year(rows, "precip_mm", lambda v: v >= t.heavy_rain_mm),
        "high_wind_days": days_per_year(rows, "wind_gust_max_kmh", lambda v: v >= t.high_wind_kmh),
    }


def nri_metric_name(hazard: str) -> str:
    if hazard in NRI_AREA_NORMALIZED:
        return f"nri_{hazard.lower()}_afreq_per_1k_sqmi"
    return f"nri_{hazard.lower()}_afreq"


def nri_metrics(
    county: NriCounty, area_floor_sqmi: float
) -> tuple[dict[str, float], dict[str, str]]:
    """NRI annualized frequencies as metrics. Nulls score as 0 and get a note.

    Hazards in NRI_AREA_NORMALIZED are divided by max(county area, area_floor_sqmi)
    and reported per 1,000 sq mi, so large counties are not ranked higher just for
    being large and small counties are not inflated by a handful of events.
    """
    if county.area_sqmi <= 0:
        raise ValueError(f"invalid area for {county.county_name} County: {county.area_sqmi}")
    metrics: dict[str, float] = {}
    notes: dict[str, str] = {}
    for h in NRI_HAZARDS:
        name = nri_metric_name(h)
        value = county.afreq.get(h.lower())
        if value is None:
            notes[name] = f"NRI {h}_AFREQ is null for {county.county_name} County; scored as 0"
            value = 0.0
        if h in NRI_AREA_NORMALIZED:
            value = value / max(county.area_sqmi, area_floor_sqmi) * 1000.0
        metrics[name] = float(value)
    return metrics, notes
