import json
from datetime import date

import httpx
import pytest

from skyrisk import db, pipeline
from skyrisk.config import load_hubs, load_scoring_config
from skyrisk.ingest import fema_nri, open_meteo
from skyrisk.ingest.http import get_json, post_json


def _load(fixtures, name):
    return json.loads((fixtures / name).read_text())


def test_parse_open_meteo_fixture(fixtures):
    days = open_meteo.parse_archive(_load(fixtures, "open_meteo_minneapolis.json"))
    assert len(days) == 5
    assert days[0].date == date(2024, 1, 1)
    assert days[4].snowfall_cm == pytest.approx(0.21)


def test_parse_archive_ytd_fixture_and_trim(fixtures):
    days = open_meteo.parse_archive(_load(fixtures, "open_meteo_archive_denver_ytd.json"))
    assert (days[0].date, days[-1].date, len(days)) == (date(2026, 9, 17), date(2026, 9, 26), 10)
    assert open_meteo.trim_trailing_missing(days) == days
    empty = days[-1].model_copy(update={f: None for f in ("snowfall_cm", "temp_max_c", "temp_min_c",
                                                          "precip_mm", "wind_gust_max_kmh")})
    partly = days[-2].model_copy(update={"snowfall_cm": None})
    assert open_meteo.trim_trailing_missing([*days[:-2], partly, empty]) == [*days[:-2], partly]
    assert open_meteo.trim_trailing_missing([empty]) == []


def test_open_meteo_rejects_unexpected_units(fixtures):
    payload = _load(fixtures, "open_meteo_minneapolis.json")
    payload["daily_units"]["snowfall_sum"] = "inch"
    with pytest.raises(ValueError, match="unexpected unit"):
        open_meteo.parse_archive(payload)


def test_parse_nri_fixture_keeps_afreq_and_risks(fixtures):
    county = fema_nri.parse_query(_load(fixtures, "nri_cook.json"))
    assert county.county_fips == "17031"
    assert county.nri_version == "December 2025"
    assert county.area_sqmi == pytest.approx(1651, abs=1)
    assert county.afreq["wntw"] == pytest.approx(14.5988, rel=1e-4)
    assert county.risks["wntw"] == 100
    assert set(county.afreq) == {"hrcn", "ifld", "cfld", "trnd", "wntw", "hwav", "cwav"}


def test_nri_no_county_raises():
    with pytest.raises(fema_nri.NriLookupError):
        fema_nri.parse_query({"features": []})


def test_get_json_retries_transient_errors():
    calls = {"n": 0}

    def handler(request):
        calls["n"] += 1
        return httpx.Response(503) if calls["n"] < 3 else httpx.Response(200, json={"ok": True})

    client = httpx.Client(transport=httpx.MockTransport(handler))
    assert get_json(client, "https://example.test", {}, sleep=lambda s: None) == {"ok": True}
    assert calls["n"] == 3


def _fake_api(fixtures, config):
    """Mock transport serving a full window of synthetic weather plus the Cook NRI fixture."""
    nri = _load(fixtures, "nri_cook.json")
    n = config.window.num_days
    times = [date.fromordinal(config.window.start.toordinal() + i).isoformat() for i in range(n)]
    weather = {
        "daily_units": open_meteo.EXPECTED_UNITS,
        "daily": {"time": times, **{v: [1.0] * n for v in open_meteo.DAILY_VARS}},
    }
    calls = {"weather": 0, "nri": 0}

    def handler(request):
        if "open-meteo" in request.url.host:
            calls["weather"] += 1
            return httpx.Response(200, json=weather)
        calls["nri"] += 1
        return httpx.Response(200, json=nri)

    return httpx.Client(transport=httpx.MockTransport(handler)), calls


def test_ingest_uses_cache_on_second_run(fixtures, config_dir):
    registry = load_hubs(config_dir / "hubs.yaml")
    config = load_scoring_config(config_dir / "scoring.yaml")
    conn = db.connect(":memory:")
    client, calls = _fake_api(fixtures, config)

    pipeline.ingest(conn, registry, config, client, hub_ids=["chicago"], log=lambda m: None, sleep=lambda s: None)
    assert calls == {"weather": 1, "nri": 1}
    assert db.weather_day_count(conn, "chicago", config.window.start, config.window.end) == 3653

    pipeline.ingest(conn, registry, config, client, hub_ids=["chicago"], log=lambda m: None, sleep=lambda s: None)
    assert calls == {"weather": 1, "nri": 1}

    pipeline.ingest(conn, registry, config, client, hub_ids=["chicago"], refresh=True, log=lambda m: None, sleep=lambda s: None)
    assert calls == {"weather": 2, "nri": 2}


def test_end_to_end_score_and_persist(fixtures, config_dir):
    registry = load_hubs(config_dir / "hubs.yaml")
    config = load_scoring_config(config_dir / "scoring.yaml")
    conn = db.connect(":memory:")
    client, _ = _fake_api(fixtures, config)
    pipeline.ingest(conn, registry, config, client, log=lambda m: None, sleep=lambda s: None)

    result = pipeline.compute_scores(conn, registry, config)
    run_id = db.persist_run(conn, result)
    assert len(result.hubs) == len(registry.hubs)
    rows = conn.execute("SELECT COUNT(*) FROM metric_values WHERE run_id = ?", (run_id,)).fetchone()[0]
    assert rows == len(registry.hubs) * sum(len(h.metrics) for h in config.hazards.values())
    # Unscored metrics (e.g. wind) are still stored per run for stats questions.
    wind = conn.execute(
        "SELECT scored FROM hub_metrics WHERE run_id = ? AND hub_id = 'chicago' AND metric = 'high_wind_days'",
        (run_id,),
    ).fetchone()
    assert wind is not None and wind[0] == 0
    # Identical inputs everywhere -> every hub scores 0; re-running gives the same hashes.
    assert all(h.overall == 0 for h in result.hubs)
    again = pipeline.compute_scores(conn, registry, config)
    assert (again.config_hash, again.data_hash) == (result.config_hash, result.data_hash)


def test_nri_cached_without_area_is_refetched(fixtures, config_dir):
    registry = load_hubs(config_dir / "hubs.yaml")
    config = load_scoring_config(config_dir / "scoring.yaml")
    conn = db.connect(":memory:")
    client, calls = _fake_api(fixtures, config)
    pipeline.ingest(conn, registry, config, client, hub_ids=["chicago"], log=lambda m: None, sleep=lambda s: None)
    conn.execute("UPDATE nri_county SET area_sqmi = NULL")
    pipeline.ingest(conn, registry, config, client, hub_ids=["chicago"], log=lambda m: None, sleep=lambda s: None)
    assert calls == {"weather": 1, "nri": 2}


def test_get_json_honors_retry_after():
    responses = iter([httpx.Response(429, headers={"Retry-After": "7"}), httpx.Response(200, json={})])
    client = httpx.Client(transport=httpx.MockTransport(lambda r: next(responses)))
    waits = []
    get_json(client, "https://example.test", {}, sleep=waits.append)
    assert waits == [7.0]


def test_post_json_sends_body_and_headers_without_retry_when_asked():
    seen = []

    def handler(request):
        seen.append(request)
        return httpx.Response(503)

    client = httpx.Client(transport=httpx.MockTransport(handler))
    with pytest.raises(httpx.HTTPStatusError):
        post_json(client, "https://example.test", {"a": 1}, {"X-Key": "k"}, retries=0)
    assert len(seen) == 1
    assert (seen[0].method, json.loads(seen[0].content), seen[0].headers["X-Key"]) == ("POST", {"a": 1}, "k")
