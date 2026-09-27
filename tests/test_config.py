import pytest
from pydantic import ValidationError

from skyrisk.config import (
    AgentConfig,
    HubRegistry,
    RateLimitConfig,
    ScoringConfig,
    load_agent_config,
    load_hubs,
    load_scoring_config,
)

KNOWN_METRICS = {
    "snow_days", "heavy_snow_days", "extreme_heat_days", "extreme_cold_days",
    "heavy_rain_days", "high_wind_days",
    "nri_hrcn_afreq", "nri_ifld_afreq", "nri_cfld_afreq", "nri_trnd_afreq_per_1k_sqmi",
    "nri_wntw_afreq", "nri_hwav_afreq", "nri_cwav_afreq",
}


def test_hub_registry_loads_13_hubs(config_dir):
    registry = load_hubs(config_dir / "hubs.yaml")
    assert len(registry.hubs) == 13
    assert registry.get("minneapolis").state == "MN"


def test_duplicate_hub_ids_rejected():
    hub = {"id": "a", "name": "A", "city": "A", "state": "TX", "lat": 30, "lon": -95, "region": "South"}
    with pytest.raises(ValidationError, match="duplicate"):
        HubRegistry.model_validate({"hubs": [hub, hub]})


def test_non_us_coordinates_rejected():
    hub = {"id": "x", "name": "X", "city": "X", "state": "XX", "lat": 51.5, "lon": -0.1, "region": "West"}
    with pytest.raises(ValidationError):
        HubRegistry.model_validate({"hubs": [hub]})


def test_scoring_config_uses_only_known_afreq_metrics(config_dir):
    config = load_scoring_config(config_dir / "scoring.yaml")
    assert set(config.metric_names) <= KNOWN_METRICS
    assert not [m for m in config.metric_names if "risks" in m]
    assert config.window.num_days == 3653  # 2016-01-01..2025-12-31


def test_hurricane_scores_on_nri_frequency_only(config_dir):
    config = load_scoring_config(config_dir / "scoring.yaml")
    assert config.hazards["hurricane"].metrics == {"nri_hrcn_afreq": 1.0}
    assert "high_wind_days" not in config.metric_names


def _config(hazards):
    return {
        "version": "t",
        "window": {"start": "2020-01-01", "end": "2020-12-31"},
        "thresholds": {"snow_day_cm": 1, "heavy_snow_cm": 10, "extreme_heat_c": 35,
                       "extreme_cold_c": -18, "heavy_rain_mm": 50, "high_wind_kmh": 90},
        "nri_area_floor_sqmi": 1000,
        "hazards": hazards,
    }


def test_hazard_weights_must_sum_to_one():
    with pytest.raises(ValidationError, match="hazard weights"):
        ScoringConfig.model_validate(_config({"a": {"weight": 0.5, "metrics": {"m": 1.0}}}))


def test_metric_weights_must_sum_to_one():
    with pytest.raises(ValidationError, match="metric weights"):
        ScoringConfig.model_validate(_config({"a": {"weight": 1.0, "metrics": {"m": 0.6, "n": 0.6}}}))


def test_agent_config_rate_limits(config_dir):
    limits = load_agent_config(config_dir / "agent.yaml").rate_limit
    assert (limits.per_ip_per_hour, limits.global_per_day) == (20, 100)


def test_rate_limit_section_is_optional():
    config = AgentConfig.model_validate({
        "primary": {"provider": "anthropic", "model": "m"},
        "max_tool_rounds": 1, "max_input_chars": 1, "max_history_turns": 1, "max_output_tokens": 1024,
    })
    assert config.rate_limit == RateLimitConfig()


def test_rate_limit_must_be_positive():
    with pytest.raises(ValidationError):
        RateLimitConfig(per_ip_per_hour=0)
    with pytest.raises(ValidationError):
        RateLimitConfig(global_per_day=0)
