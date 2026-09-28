import dataclasses
from datetime import date, datetime, timedelta, timezone

import httpx
import pytest

from skyrisk.agent.tools import ToolError, WeatherStatInput, weather_stat
from skyrisk.history.ytd import YtdService, YtdUnavailable
from skyrisk.models import WeatherDay
from test_near_term import REGISTRY, Clock

NOW = datetime(2026, 9, 28, 12, 0, tzinfo=timezone.utc)
THROUGH = date(2026, 9, 26)  # two days back: the latest complete local day at every US hub


def ytd_days(end: date = THROUGH, *, snow_every: int = 10, gap_after: date | None = None) -> list[WeatherDay]:
    """Jan 1 to `end`, snowing every `snow_every`th day; days after `gap_after` are all null (not yet available)."""
    days = []
    for k in range((end - date(2026, 1, 1)).days + 1):
        d = date(2026, 1, 1) + timedelta(days=k)
        if gap_after and d > gap_after:
            days.append(WeatherDay(date=d, snowfall_cm=None, temp_max_c=None, temp_min_c=None,
                                   precip_mm=None, wind_gust_max_kmh=None))
            continue
        days.append(WeatherDay(date=d, snowfall_cm=2.0 if k % snow_every == 0 else 0.0, temp_max_c=20.0,
                               temp_min_c=5.0, precip_mm=1.0, wind_gust_max_kmh=30.0))
    return days


def fake_ytd(tool_ctx, *, clock=None, calls=None, fail=(), scripts=None):
    scripts = scripts or {}

    def fetch(hub, start, end):
        if calls is not None:
            calls.append((hub.id, start, end))
        if hub.id in fail:
            raise httpx.ConnectError("down")
        return scripts.get(hub.id, ytd_days(end))

    return YtdService(REGISTRY, tool_ctx.scoring.window, fetch, now=clock or Clock(NOW))


# --- service ------------------------------------------------------------------------------

def test_year_is_the_one_after_the_window_only_while_it_is_current(tool_ctx):
    clock = Clock(NOW)
    service = fake_ytd(tool_ctx, clock=clock)
    assert service.year() == 2026
    clock.now = datetime(2027, 1, 5, tzinfo=timezone.utc)
    assert service.year() is None
    with pytest.raises(YtdUnavailable, match="not the year after"):
        service.data("denver")


def test_fetches_jan_1_to_two_days_back_and_caches(tool_ctx):
    calls, clock = [], Clock(NOW)
    service = fake_ytd(tool_ctx, clock=clock, calls=calls)
    data = service.data("denver")
    assert calls == [("denver", date(2026, 1, 1), THROUGH)]
    assert (data.year, data.start, data.data_through) == (2026, date(2026, 1, 1), THROUGH)
    clock.now += timedelta(hours=5)
    service.data("denver")
    assert len(calls) == 1
    clock.now += timedelta(hours=2)
    service.data("denver")
    assert len(calls) == 2


def test_trailing_missing_days_are_trimmed(tool_ctx):
    service = fake_ytd(tool_ctx, scripts={"denver": ytd_days(gap_after=date(2026, 9, 24))})
    assert service.data("denver").data_through == date(2026, 9, 24)


def test_failures_are_unavailable_and_not_cached(tool_ctx):
    calls = []
    service = fake_ytd(tool_ctx, calls=calls, fail={"denver"})
    for _ in range(2):
        with pytest.raises(YtdUnavailable, match="denver"):
            service.data("denver")
    assert len(calls) == 2
    empty = fake_ytd(tool_ctx, scripts={"denver": ytd_days(gap_after=date(2025, 12, 31))})
    with pytest.raises(YtdUnavailable, match="empty"):
        empty.data("denver")


def test_no_complete_day_yet_on_new_years_day(tool_ctx):
    service = fake_ytd(tool_ctx, clock=Clock(datetime(2026, 1, 1, 12, tzinfo=timezone.utc)))
    with pytest.raises(YtdUnavailable, match="No complete day"):
        service.data("denver")


# --- weather_stat --------------------------------------------------------------------------

def _stat(ctx, **kw):
    kw = {"hub_ids": ["denver"], "stat": "snow_day", "unit": "pct_days", "year": 2026} | kw
    return weather_stat(ctx, WeatherStatInput(**kw))


def test_weather_stat_2026_is_partial_year_to_date(tool_ctx):
    ctx = dataclasses.replace(tool_ctx, ytd=fake_ytd(tool_ctx))
    result = _stat(ctx)
    row = result.rows[0]
    n = (THROUGH - date(2026, 1, 1)).days + 1  # 269
    assert result.partial_year is True
    assert (result.period_start, result.period_end) == (date(2026, 1, 1), THROUGH)
    assert (row.days_with_data, row.matching_days) == (n, 27)
    assert row.value == pytest.approx(100 * 27 / n, abs=0.005)
    assert "2026 is a partial year: 2026-01-01 to 2026-09-26" in result.caveats[0]
    assert "not part of the risk score" in result.caveats[0] and "2016-2025" in result.caveats[0]
    assert result.scores() == []


def test_weather_stat_2026_days_per_year_is_the_count_to_date(tool_ctx):
    ctx = dataclasses.replace(tool_ctx, ytd=fake_ytd(tool_ctx))
    result = _stat(ctx, unit="days_per_year")
    assert result.rows[0].value == 27
    assert any("actual count" in c and "not annualized" in c for c in result.caveats)


def test_weather_stat_2026_months_filter_and_no_data_yet(tool_ctx):
    ctx = dataclasses.replace(tool_ctx, ytd=fake_ytd(tool_ctx))
    assert _stat(ctx, months=[1]).rows[0].days_with_data == 31
    with pytest.raises(ToolError, match=r"No snowfall_cm data yet .*\(data through 2026-09-26"):
        _stat(ctx, months=[11, 12])


def test_weather_stat_2026_hubs_with_different_end_dates(tool_ctx):
    ytd = fake_ytd(tool_ctx, scripts={"chicago": ytd_days(date(2026, 9, 25))})
    result = _stat(dataclasses.replace(tool_ctx, ytd=ytd), hub_ids=["denver", "chicago"])
    assert result.period_end == date(2026, 9, 25)
    assert any("different dates" in c and "chicago 2026-09-25" in c for c in result.caveats)


def test_weather_stat_other_years_still_refused_with_ytd(tool_ctx):
    ctx = dataclasses.replace(tool_ctx, ytd=fake_ytd(tool_ctx))
    for year in (2014, 2027):
        with pytest.raises(ToolError, match="outside the historical data window") as exc:
            _stat(ctx, year=year)
        assert "2026 is available as partial year-to-date data" in str(exc.value)
    assert _stat(ctx, year=2025).partial_year is False


def test_weather_stat_2026_unavailable_is_a_tool_error(tool_ctx):
    ctx = dataclasses.replace(tool_ctx, ytd=fake_ytd(tool_ctx, fail={"denver"}))
    with pytest.raises(ToolError, match="unavailable right now"):
        _stat(ctx)
