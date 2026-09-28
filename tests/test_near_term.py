"""Near-term forecast risk: config, pure engine, forecast parsing, cached service, tool and grounding."""

import dataclasses
import json
from datetime import date, datetime, timedelta, timezone

import httpx
import pytest
from pydantic import ValidationError

from conftest import ROOT
from fakes import FakeProvider, answer, call
from skyrisk.agent.core import Agent, AgentReply, Conversation
from skyrisk.agent.tools import NearTermRiskInput, ToolError, near_term_risk, run_tool
from skyrisk.config import NearTermConfig, load_hubs, load_near_term_config
from skyrisk.evals.cases import EvalCase
from skyrisk.evals.checks import check_run
from skyrisk.ingest.open_meteo import fetch_forecast, parse_forecast
from skyrisk.models import WeatherDay
from skyrisk.nearterm.engine import apply_demo, day_severity, score_forecast
from skyrisk.nearterm.service import NearTermService, NearTermUnavailable

CFG = load_near_term_config(ROOT / "config" / "near_term.yaml")
REGISTRY = load_hubs(ROOT / "config" / "hubs.yaml")
START = date(2026, 9, 28)
NOW = datetime(2026, 9, 28, 12, 0, tzinfo=timezone.utc)


def forecast(overrides: dict[int, dict] | None = None, n: int = 7) -> list[WeatherDay]:
    """A calm week; `overrides` maps day index -> field values."""
    calm = {"snowfall_cm": 0.0, "temp_max_c": 20.0, "temp_min_c": 10.0, "precip_mm": 0.0, "wind_gust_max_kmh": 20.0}
    return [WeatherDay(date=START + timedelta(days=i), **(calm | (overrides or {}).get(i, {}))) for i in range(n)]


def load_fixture(fixtures, hub_id):
    return json.loads((fixtures / f"open_meteo_forecast_{hub_id}.json").read_text())


class Clock:
    def __init__(self, now=NOW):
        self.now = now

    def __call__(self):
        return self.now


def fake_service(scripts: dict[str, list[WeatherDay]] | None = None, *, clock=None, calls=None, fail=()):
    """A service whose fetch returns scripted forecasts (calm by default) and records each call."""
    scripts = {} if scripts is None else scripts

    def fetch(hub):
        if calls is not None:
            calls.append(hub.id)
        if hub.id in fail:
            raise httpx.ConnectError("down")
        return scripts.get(hub.id, forecast())

    return NearTermService(REGISTRY, CFG, fetch, now=clock or Clock())


# --- config -----------------------------------------------------------------------------

def test_committed_config_validates_and_every_hazard_can_reach_high_alone():
    assert CFG.version and len(CFG.lead_time_weights) == CFG.forecast_days
    assert all(h.max_points >= CFG.levels.high for h in CFG.hazards.values())


def _cfg_data(**overrides):
    return CFG.model_dump(mode="json") | overrides


def test_max_points_below_high_is_rejected():
    data = _cfg_data()
    data["hazards"]["snow"]["max_points"] = 50
    with pytest.raises(ValidationError, match="max_points below levels.high"):
        NearTermConfig.model_validate(data)


@pytest.mark.parametrize("overrides, message", [
    ({"lead_time_weights": [1.0] * 6}, "needs 7 entries"),
    ({"lead_time_weights": [1.0] * 6 + [0.0]}, r"in \(0, 1\]"),
    ({"levels": {"medium": 70, "high": 65}}, "above levels.medium"),
])
def test_invalid_config_is_rejected(overrides, message):
    with pytest.raises(ValidationError, match=message):
        NearTermConfig.model_validate(_cfg_data(**overrides))


def test_watch_must_be_below_severe():
    data = _cfg_data()
    data["hazards"]["wind"]["watch"] = 120
    with pytest.raises(ValidationError, match="must be above watch"):
        NearTermConfig.model_validate(data)


# --- engine -----------------------------------------------------------------------------

def test_day_severity_ramp():
    assert day_severity(2.0, 2.0, 15.0) == 0.0
    assert day_severity(8.5, 2.0, 15.0) == 0.5
    assert day_severity(15.0, 2.0, 15.0) == 1.0
    assert day_severity(40.0, 2.0, 15.0) == 1.0
    assert day_severity(-5.0, 2.0, 15.0) == 0.0


def test_calm_week_scores_zero_low():
    result = score_forecast(forecast(), CFG)
    assert (result.score, result.level) == (0.0, "low")
    assert (result.forecast_start, result.forecast_end) == (START, START + timedelta(days=6))


def test_one_severe_hazard_on_day_zero_is_high_on_its_own():
    result = score_forecast(forecast({0: {"snowfall_cm": 15.0}}), CFG)
    assert result.score == 70.0 and result.level == "high"
    snow = next(h for h in result.hazards if h.hazard == "snow")
    assert (snow.peak_value, snow.peak_date, snow.points) == (15.0, START, 70.0)


def test_level_boundaries():
    assert score_forecast(forecast({0: {"snowfall_cm": 8.5}}), CFG).level == "medium"  # exactly 35
    assert CFG.level_for(34.99) == "low" and CFG.level_for(65) == "high" and CFG.level_for(64.99) == "medium"


def test_lead_time_weights_discount_later_days():
    result = score_forecast(forecast({6: {"snowfall_cm": 15.0}}), CFG)
    assert result.score == pytest.approx(70 * CFG.lead_time_weights[6])
    assert result.level == "medium"


def test_peak_is_the_worst_weighted_day_not_the_largest_value():
    # 10 cm today (0.615 x 1.0) beats 15 cm on day 5 (1.0 x 0.6)
    result = score_forecast(forecast({0: {"snowfall_cm": 10.0}, 5: {"snowfall_cm": 15.0}}), CFG)
    snow = next(h for h in result.hazards if h.hazard == "snow")
    assert snow.peak_date == START and snow.peak_value == 10.0


def test_hazards_add_up_and_cap_at_100():
    two = score_forecast(forecast({1: {"precip_mm": 50.0}, 2: {"temp_max_c": 38.5}}), CFG)
    assert two.score == pytest.approx(35.0 + 70 * 0.5 * 0.9)
    storm = score_forecast(forecast({0: {"snowfall_cm": 30, "wind_gust_max_kmh": 120, "precip_mm": 100}}), CFG)
    assert storm.score == 100.0 and storm.level == "high"


def test_missing_values_count_as_no_risk_with_a_note():
    result = score_forecast(forecast({0: {"snowfall_cm": None}}), CFG)
    assert result.score == 0.0
    assert any("snow: 1 forecast day(s) had no snowfall_cm" in n for n in result.notes)


def test_demo_merges_the_storm_into_the_real_forecast():
    days = forecast({1: {"snowfall_cm": 40.0}})
    demo = apply_demo(days, CFG)
    assert demo[1].snowfall_cm == 40.0  # a bigger real value is kept
    assert demo[1].wind_gust_max_kmh == CFG.demo.values["wind_gust_max_kmh"]
    assert demo[0] == days[0] and score_forecast(demo, CFG).level == "high"


# --- forecast client ----------------------------------------------------------------------

def test_parse_recorded_forecasts(fixtures):
    houston = parse_forecast(load_fixture(fixtures, "houston"))
    assert len(houston) == 7 and houston[0].date == date(2026, 9, 28)
    assert score_forecast(houston, CFG).score == 0.0  # a calm, hot-but-not-extreme week (max 33.4 C)
    minneapolis = score_forecast(parse_forecast(load_fixture(fixtures, "minneapolis")), CFG)
    rain = next(h for h in minneapolis.hazards if h.hazard == "heavy_rain")
    assert rain.peak_value == 27.28 and minneapolis.score == pytest.approx(70 * 2.28 / 50, abs=0.01)


def test_parse_forecast_fails_loud_on_wrong_units(fixtures):
    payload = load_fixture(fixtures, "houston")
    payload["daily_units"]["precipitation_sum"] = "inch"
    with pytest.raises(ValidationError, match="unexpected unit"):
        parse_forecast(payload)


def test_fetch_forecast_requests_the_daily_forecast(fixtures):
    seen = []

    def handler(request):
        seen.append(request.url)
        return httpx.Response(200, json=load_fixture(fixtures, "houston"))

    client = httpx.Client(transport=httpx.MockTransport(handler))
    days = fetch_forecast(REGISTRY.get("houston"), client, days=7)
    assert len(days) == 7
    params = seen[0].params
    assert seen[0].host == "api.open-meteo.com" and params["forecast_days"] == "7"
    assert "wind_gusts_10m_max" in params["daily"]


# --- service ------------------------------------------------------------------------------

def test_service_caches_within_ttl_and_refetches_after():
    calls, clock = [], Clock()
    service = fake_service(calls=calls, clock=clock)
    first = service.score("houston")
    service.score("houston")
    assert calls == ["houston"]
    clock.now += timedelta(seconds=CFG.cache_ttl_s + 1)
    later = service.score("houston")
    assert calls == ["houston", "houston"] and later.fetched_at > first.fetched_at
    service.score("houston", fresh=True)
    assert len(calls) == 3


def test_service_failure_is_unavailable():
    with pytest.raises(NearTermUnavailable, match="houston"):
        fake_service(fail={"houston"}).score("houston")


# --- tool ---------------------------------------------------------------------------------

def test_near_term_tool_for_one_hub(tool_ctx):
    ctx = dataclasses.replace(tool_ctx, near_term=fake_service({"houston": forecast({0: {"precip_mm": 75.0}})}))
    result = near_term_risk(ctx, NearTermRiskInput(hub_ids=["houston"]))
    row = result.rows[0]
    assert (row.hub_id, row.score, row.level) == ("houston", 70.0, "high")
    rain = next(h for h in row.hazards if h.hazard == "heavy_rain")
    assert (rain.unit, rain.peak_value) == ("mm", 75.0)
    assert row.last_check is None and row.recent_alerts == []
    assert [s.model_dump() for s in result.scores()] == [{"hub_id": "houston", "hazard": "near_term", "score": 70.0}]
    assert any("NOT the relative historical" in c for c in result.caveats)


def test_near_term_tool_all_hubs_sorted_and_partial_failure(tool_ctx):
    service = fake_service({"denver": forecast({0: {"snowfall_cm": 15.0}})}, fail={"miami"})
    result = near_term_risk(dataclasses.replace(tool_ctx, near_term=service), NearTermRiskInput())
    assert result.rows[0].hub_id == "denver" and len(result.rows) == len(REGISTRY.hubs) - 1
    assert any("Missing" in c and "miami" in c for c in result.caveats)


def test_near_term_tool_errors(tool_ctx):
    with pytest.raises(ToolError, match="not configured"):
        near_term_risk(tool_ctx, NearTermRiskInput(hub_ids=["houston"]))
    ctx = dataclasses.replace(tool_ctx, near_term=fake_service())
    assert run_tool(ctx, "near_term_risk", {"hub_ids": ["seattle"]}).is_error


def test_agent_grounds_near_term_citations(tool_ctx):
    ctx = dataclasses.replace(tool_ctx, near_term=fake_service({"houston": forecast({0: {"precip_mm": 75.0}})}))
    provider = FakeProvider(script=[
        call("near_term_risk", hub_ids=["houston"]),
        answer("Houston's near-term risk is 71.5.", scores=[("houston", "near_term", 71.5)]),  # invented
        answer("Houston's near-term risk is 70.0 (high).", scores=[("houston", "near_term", 70.0)]),
    ])
    agent = Agent(ctx, [provider], "system", log=lambda m: None)
    reply = agent.ask(Conversation(), "Near-term risk for Houston?")
    assert reply.status == "answered" and reply.scores_cited[0]["score"] == 70.0
    assert "71.5" in provider.received[2]["feedback"]


def test_eval_grounding_rechecks_near_term_against_the_service(tool_ctx):
    ctx = dataclasses.replace(tool_ctx, near_term=fake_service({"houston": forecast({0: {"precip_mm": 75.0}})}))
    case = EvalCase(id="c", category="near_term", question="q?", expect="answered")

    def reply(score):
        return AgentReply(status="answered", text="", limitations=["x"],
                          scores_cited=[{"hub_id": "houston", "hazard": "near_term", "score": score}])

    assert check_run(case, reply(70.0), ctx) == []
    assert "forecast has 70.00" in check_run(case, reply(60.0), ctx)[0]
    assert "no near-term service" in check_run(case, reply(70.0), tool_ctx)[0]


# --- prompt -------------------------------------------------------------------------------

def test_system_prompt_states_the_history_window_and_2026_rule(tool_ctx):
    from skyrisk.agent.prompts import build_system_prompt

    prompt = build_system_prompt(tool_ctx.registry, tool_ctx.scoring)
    assert "full calendar years 2016-2025 only" in prompt
    assert "2026 is intentionally excluded" in prompt and "do not guess" in prompt
    assert "near_term_risk" in prompt and "never add, average or rank a near-term score" in prompt
