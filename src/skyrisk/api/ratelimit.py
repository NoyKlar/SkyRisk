"""In-process limits on chat requests: a sliding hourly window per client IP plus a global daily cap.

Counters live in memory, so they reset when the process restarts (on Render's free tier, after a spin-down).
The per-IP limit is for fairness; the global cap is what bounds API spend.
"""

from __future__ import annotations

import math
import threading
import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Literal

from skyrisk.config import RateLimitConfig

WINDOW_S = 3600.0
MAX_TRACKED_IPS = 10_000  # sweep expired entries once the map grows past this


@dataclass(frozen=True)
class Limited:
    scope: Literal["ip", "global"]
    retry_after: int  # seconds, at least 1
    limit: int

    @property
    def message(self) -> str:
        if self.scope == "ip":
            minutes = max(1, math.ceil(self.retry_after / 60))
            return (f"You've reached the limit of {_plural(self.limit, 'question')} per hour. "
                    f"Please try again in about {_plural(minutes, 'minute')}.")
        return "SkyRisk has reached its daily question limit. Please try again after 00:00 UTC."


class RateLimiter:
    def __init__(self, per_ip_per_hour: int, global_per_day: int, *,
                 now: Callable[[], float] = time.time) -> None:
        self._per_ip = per_ip_per_hour
        self._per_day = global_per_day
        self._now = now
        self._hits: dict[str, deque[float]] = {}
        self._day: str | None = None
        self._day_count = 0
        self._lock = threading.Lock()

    @classmethod
    def from_config(cls, config: RateLimitConfig) -> RateLimiter:
        return cls(config.per_ip_per_hour, config.global_per_day)

    def check(self, ip: str) -> Limited | None:
        """Record a request from `ip`, or return why it is refused (a refused request is not recorded)."""
        with self._lock:
            now = self._now()
            hits = self._hits.setdefault(ip, deque())
            _expire(hits, now)
            if len(hits) >= self._per_ip:
                return Limited("ip", _at_least_1(hits[0] + WINDOW_S - now), self._per_ip)

            day = datetime.fromtimestamp(now, UTC)
            if day.date().isoformat() != self._day:
                self._day, self._day_count = day.date().isoformat(), 0
            if self._day_count >= self._per_day:
                midnight = datetime.combine(day.date() + timedelta(days=1), datetime.min.time(), UTC)
                return Limited("global", _at_least_1(midnight.timestamp() - now), self._per_day)

            hits.append(now)
            self._day_count += 1
            if len(self._hits) > MAX_TRACKED_IPS:
                self._sweep(now)
            return None

    def __len__(self) -> int:
        return len(self._hits)

    def _sweep(self, now: float) -> None:
        for ip in list(self._hits):
            _expire(self._hits[ip], now)
            if not self._hits[ip]:
                del self._hits[ip]


def _expire(hits: deque[float], now: float) -> None:
    while hits and now - hits[0] >= WINDOW_S:
        hits.popleft()


def _at_least_1(seconds: float) -> int:
    return max(1, math.ceil(seconds))


def _plural(n: int, word: str) -> str:
    return f"{n} {word}" if n == 1 else f"{n} {word}s"
