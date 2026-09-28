"""Near-term scores from a live forecast, with a per-hub in-memory cache.

The agent tool and the alert check both go through this service. The cache keeps chat questions from
refetching the forecast on every turn; the alert check always asks for a fresh forecast.
"""

from __future__ import annotations

import threading
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone

import httpx
from pydantic import BaseModel, ValidationError

from skyrisk.config import Hub, HubRegistry, Level, NearTermConfig
from skyrisk.models import WeatherDay
from skyrisk.nearterm.engine import NearTermScore, score_forecast

Fetch = Callable[[Hub], list[WeatherDay]]
LEVELS_WORKERS = 4  # a cold cache fetches all hubs; a few at a time keeps it quick and polite to Open-Meteo


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


class NearTermUnavailable(Exception):
    """The forecast could not be fetched or parsed; the message is safe to show."""


@dataclass(frozen=True)
class HubNearTerm:
    hub_id: str
    fetched_at: datetime
    days: list[WeatherDay]
    result: NearTermScore


class HubLevel(BaseModel):
    """One hub's current near-term level for the hubs list; score and level are None when unavailable."""
    hub_id: str
    score: float | None
    level: Level | None
    forecast_start: date | None = None
    forecast_end: date | None = None
    error: str | None = None


class NearTermService:
    def __init__(self, registry: HubRegistry, cfg: NearTermConfig, fetch: Fetch, *,
                 now: Callable[[], datetime] = utc_now) -> None:
        self.registry = registry
        self.cfg = cfg
        self._fetch = fetch
        self._now = now
        self._cache: dict[str, tuple[datetime, list[WeatherDay]]] = {}
        self._lock = threading.Lock()

    def now(self) -> datetime:
        return self._now()

    def score(self, hub_id: str, *, fresh: bool = False) -> HubNearTerm:
        fetched_at, days = self._forecast(hub_id, fresh=fresh)
        return HubNearTerm(hub_id, fetched_at, days, score_forecast(days, self.cfg))

    def score_all(self, *, fresh: bool = False) -> list[HubNearTerm]:
        return [self.score(h.id, fresh=fresh) for h in self.registry.hubs]

    def levels(self) -> list[HubLevel]:
        """Every hub's level from the cache (never fresh), in registry order; a failing hub gets an error."""
        def one(hub: Hub) -> HubLevel:
            try:
                r = self.score(hub.id).result
            except NearTermUnavailable as e:
                return HubLevel(hub_id=hub.id, score=None, level=None, error=str(e))
            return HubLevel(hub_id=hub.id, score=r.score, level=r.level,
                            forecast_start=r.forecast_start, forecast_end=r.forecast_end)

        with ThreadPoolExecutor(max_workers=LEVELS_WORKERS) as pool:
            return list(pool.map(one, self.registry.hubs))

    def _forecast(self, hub_id: str, *, fresh: bool) -> tuple[datetime, list[WeatherDay]]:
        hub = self.registry.get(hub_id)
        now = self._now()
        with self._lock:
            cached = self._cache.get(hub_id)
        if cached and not fresh and now - cached[0] < timedelta(seconds=self.cfg.cache_ttl_s):
            return cached
        try:
            days = self._fetch(hub)
        except (httpx.HTTPError, ValidationError, ValueError) as e:
            raise NearTermUnavailable(f"The forecast for {hub_id} is unavailable right now ({type(e).__name__}).") from e
        if not days:
            raise NearTermUnavailable(f"The forecast for {hub_id} came back empty.")
        with self._lock:
            self._cache[hub_id] = (now, days)
        return now, days
