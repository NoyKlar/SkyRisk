from datetime import date, timedelta

import pytest

from skyrisk.config import Thresholds
from skyrisk.models import NriCounty, WeatherDay
from skyrisk.scoring.metrics import days_per_year, nri_metrics, pct_days, weather_metrics

T = Thresholds(snow_day_cm=1, heavy_snow_cm=10, extreme_heat_c=35, extreme_cold_c=-18,
               heavy_rain_mm=50, high_wind_kmh=90)


def _days(snow: list[float | None]) -> list[WeatherDay]:
    start = date(2020, 1, 1)
    return [
        WeatherDay(date=start + timedelta(days=i), snowfall_cm=s, temp_max_c=20, temp_min_c=5,
                   precip_mm=0, wind_gust_max_kmh=20)
        for i, s in enumerate(snow)
    ]


def test_pct_days_counts_threshold_inclusive():
    rows = _days([0, 1.0, 5, 0.9])
    assert pct_days(rows, "snowfall_cm", lambda v: v >= 1.0) == 50.0


def test_null_days_excluded_from_denominator():
    rows = _days([2, None, 0, None])
    assert pct_days(rows, "snowfall_cm", lambda v: v >= 1) == 50.0


def test_days_per_year_annualizes():
    rows = _days([2] * 10 + [0] * 30)  # 25% of days
    assert days_per_year(rows, "snowfall_cm", lambda v: v >= 1) == pytest.approx(0.25 * 365.25)


def test_all_null_field_raises():
    with pytest.raises(ValueError):
        pct_days(_days([None, None]), "snowfall_cm", lambda v: True)


def test_weather_metrics_keys_and_values():
    rows = _days([12, 3, 0, 0])
    m = weather_metrics(rows, T)
    assert m["snow_days"] == pytest.approx(0.5 * 365.25)
    assert m["heavy_snow_days"] == pytest.approx(0.25 * 365.25)
    assert m["extreme_heat_days"] == 0


FLOOR = 1000.0


def _county(area=500.0, **afreq):
    base = {"hrcn": 0.1, "ifld": 2.0, "cfld": 0.2, "trnd": 1.0, "wntw": 3.0, "hwav": 1.0, "cwav": 0.5}
    return NriCounty(county_fips="00000", county_name="Test", state="X", nri_version="v",
                     area_sqmi=area, afreq=base | afreq, risks={})


def test_nri_null_afreq_scores_zero_with_note():
    metrics, notes = nri_metrics(_county(cfld=None), FLOOR)
    assert metrics["nri_cfld_afreq"] == 0.0
    assert "nri_cfld_afreq" in notes


def test_only_tornado_is_area_normalized():
    metrics, _ = nri_metrics(_county(area=2000.0), FLOOR)
    assert metrics["nri_trnd_afreq_per_1k_sqmi"] == pytest.approx(0.5)  # 1.0 / 2000 * 1000
    # Inland flood, hurricane and area-wide hazards stay raw.
    assert metrics["nri_ifld_afreq"] == 2.0
    assert metrics["nri_hrcn_afreq"] == 0.1
    assert metrics["nri_wntw_afreq"] == 3.0
    assert "nri_trnd_afreq" not in metrics


def test_large_counties_with_equal_density_score_equal():
    a, _ = nri_metrics(_county(area=1500.0, trnd=1.5), FLOOR)
    b, _ = nri_metrics(_county(area=3000.0, trnd=3.0), FLOOR)
    assert a["nri_trnd_afreq_per_1k_sqmi"] == pytest.approx(b["nri_trnd_afreq_per_1k_sqmi"])


def test_area_floor_prevents_small_county_inflation():
    # Denver-like: 0.16 tornadoes/yr over 156 sq mi -> floored to 1,000 sq mi.
    metrics, _ = nri_metrics(_county(area=156.0, trnd=0.16), FLOOR)
    assert metrics["nri_trnd_afreq_per_1k_sqmi"] == pytest.approx(0.16)


def test_nri_invalid_area_raises():
    with pytest.raises(ValueError, match="invalid area"):
        nri_metrics(_county(area=0.0), FLOOR)
