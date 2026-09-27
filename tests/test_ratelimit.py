from datetime import UTC, datetime

from skyrisk.api.ratelimit import MAX_TRACKED_IPS, RateLimiter


class Clock:
    def __init__(self, t: float) -> None:
        self.t = t

    def __call__(self) -> float:
        return self.t


NOON = datetime(2026, 9, 27, 12, 0, tzinfo=UTC).timestamp()


def _limiter(per_ip=3, per_day=100, t=NOON):
    clock = Clock(t)
    return RateLimiter(per_ip, per_day, now=clock), clock


def test_allows_limit_then_refuses_with_retry_after():
    limiter, clock = _limiter(per_ip=3)
    for i in range(3):
        clock.t = NOON + i * 60
        assert limiter.check("1.1.1.1") is None
    limited = limiter.check("1.1.1.1")
    assert limited.scope == "ip"
    assert limited.retry_after == 3600 - 120  # the oldest hit (at NOON) expires in 58 minutes
    assert "3 questions per hour" in limited.message and "58 minutes" in limited.message


def test_window_slides():
    limiter, clock = _limiter(per_ip=2)
    assert limiter.check("a") is None
    clock.t += 1800
    assert limiter.check("a") is None
    assert limiter.check("a") is not None
    clock.t = NOON + 3600  # the first hit has expired, the second has not
    assert limiter.check("a") is None
    assert limiter.check("a") is not None


def test_ips_are_independent():
    limiter, _ = _limiter(per_ip=1)
    assert limiter.check("a") is None
    assert limiter.check("a") is not None
    assert limiter.check("b") is None


def test_global_cap_across_ips_until_utc_midnight():
    limiter, clock = _limiter(per_day=2)
    assert limiter.check("a") is None
    assert limiter.check("b") is None
    limited = limiter.check("c")
    assert limited.scope == "global"
    assert limited.retry_after == 12 * 3600
    assert "00:00 UTC" in limited.message
    clock.t = NOON + 12 * 3600  # 00:00 UTC the next day
    assert limiter.check("c") is None


def test_ip_rejection_does_not_consume_global_quota():
    limiter, _ = _limiter(per_ip=1, per_day=2)
    assert limiter.check("a") is None
    for _ in range(5):
        assert limiter.check("a").scope == "ip"
    assert limiter.check("b") is None
    assert limiter.check("c").scope == "global"


def test_retry_after_is_at_least_one_second():
    limiter, clock = _limiter(per_ip=1)
    assert limiter.check("a") is None
    clock.t += 3599.9
    limited = limiter.check("a")
    assert limited.retry_after == 1
    assert "limit of 1 question per hour" in limited.message and "about 1 minute." in limited.message


def test_sweep_bounds_tracked_ips():
    limiter, clock = _limiter(per_ip=5, per_day=10 * MAX_TRACKED_IPS)
    for i in range(MAX_TRACKED_IPS):
        limiter.check(f"10.0.{i // 256}.{i % 256}")
    clock.t += 3600
    limiter.check("fresh-1")  # map exceeds the bound, so expired IPs are swept
    assert len(limiter) == 1
