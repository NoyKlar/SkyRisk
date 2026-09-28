"""Near-term scores from a live forecast, with a per-hub in-memory cache.

The agent tool and the alert check both go through this service. The cache keeps chat questions from
refetching the forecast on every turn; the alert check always asks for a fresh forecast.
"""

from __future__ import annotations

import threading
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

import httpx
from pydantic import ValidationError

from skyrisk.config import Hub, HubRegistry, NearTermConfig
from skyrisk.models import WeatherDay
from skyrisk.nearterm.engine import NearTermScore, score_forecast

Fetch = Callable[[Hub], list[WeatherDay]]


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
