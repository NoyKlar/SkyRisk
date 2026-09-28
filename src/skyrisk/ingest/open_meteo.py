"""Open-Meteo clients: historical weather (archive API) and the daily forecast (forecast API)."""

from __future__ import annotations

from datetime import date

import httpx
from pydantic import BaseModel, model_validator

from skyrisk.config import Hub
from skyrisk.ingest.http import get_json
from skyrisk.models import WeatherDay

ARCHIVE_URL = "https://archive-api.open-meteo.com/v1/archive"
FORECAST_URL = "https://api.open-meteo.com/v1/forecast"
DAILY_VARS = (
    "snowfall_sum",
    "temperature_2m_max",
    "temperature_2m_min",
    "precipitation_sum",
    "wind_gusts_10m_max",
)
EXPECTED_UNITS = {
    "snowfall_sum": "cm",
    "temperature_2m_max": "°C",
    "temperature_2m_min": "°C",
    "precipitation_sum": "mm",
    "wind_gusts_10m_max": "km/h",
}


class _Daily(BaseModel):
    time: list[date]
    snowfall_sum: list[float | None]
    temperature_2m_max: list[float | None]
    temperature_2m_min: list[float | None]
    precipitation_sum: list[float | None]
    wind_gusts_10m_max: list[float | None]

    @model_validator(mode="after")
    def _equal_lengths(self) -> _Daily:
        n = len(self.time)
        for var in DAILY_VARS:
            if len(getattr(self, var)) != n:
                raise ValueError(f"daily.{var} length does not match daily.time")
        return self


class _ArchiveResponse(BaseModel):
    daily_units: dict[str, str]
    daily: _Daily

    @model_validator(mode="after")
    def _units(self) -> _ArchiveResponse:
        for var, unit in EXPECTED_UNITS.items():
            if self.daily_units.get(var) != unit:
                raise ValueError(f"unexpected unit for {var}: {self.daily_units.get(var)!r}")
        return self


def parse_archive(payload: dict) -> list[WeatherDay]:
    d = _ArchiveResponse.model_validate(payload).daily
    return [
        WeatherDay(
            date=d.time[i],
            snowfall_cm=d.snowfall_sum[i],
            temp_max_c=d.temperature_2m_max[i],
            temp_min_c=d.temperature_2m_min[i],
            precip_mm=d.precipitation_sum[i],
            wind_gust_max_kmh=d.wind_gusts_10m_max[i],
        )
        for i in range(len(d.time))
    ]


def parse_forecast(payload: dict) -> list[WeatherDay]:
    """The forecast API returns the same daily shape and units as the archive."""
    return parse_archive(payload)


def fetch_daily(hub: Hub, start: date, end: date, client: httpx.Client) -> list[WeatherDay]:
    params = {
        "latitude": hub.lat,
        "longitude": hub.lon,
        "start_date": start.isoformat(),
        "end_date": end.isoformat(),
        "daily": ",".join(DAILY_VARS),
        "timezone": "auto",
    }
    return parse_archive(get_json(client, ARCHIVE_URL, params))


def fetch_forecast(hub: Hub, client: httpx.Client, *, days: int = 7, retries: int = 1) -> list[WeatherDay]:
    """Daily forecast from today (hub-local) for `days` days. Few retries: callers are interactive."""
    params = {
        "latitude": hub.lat,
        "longitude": hub.lon,
        "daily": ",".join(DAILY_VARS),
        "timezone": "auto",
        "forecast_days": days,
    }
    return parse_forecast(get_json(client, FORECAST_URL, params, retries=retries))
