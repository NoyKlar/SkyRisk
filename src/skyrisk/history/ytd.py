"""Current-year year-to-date daily weather from the Open-Meteo archive, with a per-hub in-memory cache.

Only weather_stat uses it. The historical risk score stays on the full years of the scoring window, and
nothing here is written to SQLite, so ingest and score runs are untouched. The one extra year allowed is
the year right after the window, and only while it is the current year: once the calendar moves on, the
window must roll forward (see docs/DESIGN.md, refresh policy) before that year can be asked about again.
"""

from __future__ import annotations

import threading
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, datetime, timedelta

import httpx
from pydantic import ValidationError

from skyrisk.config import Hub, HubRegistry, Window
from skyrisk.ingest.open_meteo import trim_trailing_missing
from skyrisk.models import WeatherDay
from skyrisk.nearterm.service import utc_now

Fetch = Callable[[Hub, date, date], list[WeatherDay]]
YTD_CACHE_TTL_S = 6 * 3600
# The archive serves up to today, but today (hub-local) is still in progress. Two days back from the UTC
# date is a complete local day at every US hub (UTC-10 at the most).
COMPLETE_DAY_LAG = 2


class YtdUnavailable(Exception):
    """Year-to-date data could not be fetched or is empty; the message is safe to show."""


@dataclass(frozen=True)
class HubYtd:
    hub_id: str
    year: int
    start: date
    data_through: date
    days: list[WeatherDay]


class YtdService:
    def __init__(self, registry: HubRegistry, window: Window, fetch: Fetch, *,
                 now: Callable[[], datetime] = utc_now, ttl_s: float = YTD_CACHE_TTL_S) -> None:
        self.registry = registry
        self.window = window
        self._fetch = fetch
        self._now = now
        self._ttl = timedelta(seconds=ttl_s)
        self._cache: dict[str, tuple[datetime, HubYtd]] = {}
        self._lock = threading.Lock()

    def year(self) -> int | None:
        """The year answered as year-to-date: the one after the window, while it is the current year."""
        year = self.window.end.year + 1
        return year if self._now().date().year == year else None

    def data(self, hub_id: str) -> HubYtd:
        year = self.year()
        if year is None:
            raise YtdUnavailable("No year-to-date data is served: the current year is not the year after "
                                 f"the {self.window.start.year}-{self.window.end.year} window.")
        hub = self.registry.get(hub_id)
        now = self._now()
        with self._lock:
            cached = self._cache.get(hub_id)
        if cached and cached[1].year == year and now - cached[0] < self._ttl:
            return cached[1]
        start, end = date(year, 1, 1), now.date() - timedelta(days=COMPLETE_DAY_LAG)
        if end < start:
            raise YtdUnavailable(f"No complete day of {year} is available yet.")
        try:
            days = trim_trailing_missing(self._fetch(hub, start, end))
        except (httpx.HTTPError, ValidationError, ValueError) as e:
            raise YtdUnavailable(f"{year} year-to-date data for {hub_id} is unavailable right now "
                                 f"({type(e).__name__}).") from e
        if not days:
            raise YtdUnavailable(f"{year} year-to-date data for {hub_id} came back empty.")
        result = HubYtd(hub_id, year, start, days[-1].date, days)
        with self._lock:
            self._cache[hub_id] = (now, result)
        return result
