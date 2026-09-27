import copy

import pytest

from skyrisk.config import ScoringConfig
from skyrisk.scoring.engine import min_max, score

CONFIG = ScoringConfig.model_validate({
    "version": "test",
    "window": {"start": "2020-01-01", "end": "2020-12-31"},
    "thresholds": {"snow_day_cm": 1, "heavy_snow_cm": 10, "extreme_heat_c": 35,
                   "extreme_cold_c": -18, "heavy_rain_mm": 50, "high_wind_kmh": 90},
    "nri_area_floor_sqmi": 1000,
    "hazards": {
        "a": {"weight": 0.6, "metrics": {"m1": 1.0}},
        "b": {"weight": 0.4, "metrics": {"m2": 0.5, "m3": 0.5}},
    },
})

METRICS = {
    "x": {"m1": 0.0, "m2": 1.0, "m3": 3.0},
    "y": {"m1": 5.0, "m2": 1.0, "m3": 1.0},
    "z": {"m1": 10.0, "m2": 1.0, "m3": 2.0},
}


def _by_id(result):
    return {h.hub_id: h for h in result.hubs}


def test_hand_computed_scores_and_ranks():
    # m1 -> x0 y50 z100; m2 all equal -> 0; m3 -> x100 y0 z50
    # b = 0.5*m2 + 0.5*m3 -> x50 y0 z25
    # overall = 0.6*a + 0.4*b -> x20 y30 z70
    hubs = _by_id(score(METRICS, CONFIG))
    assert hubs["x"].overall == pytest.approx(20)
    assert hubs["y"].overall == pytest.approx(30)
    assert hubs["z"].overall == pytest.approx(70)
    assert [hubs[h].rank for h in "zyx"] == [1, 2, 3]
    b = {hz.hazard: hz for hz in hubs["x"].hazards}["b"]
    assert b.sub_score == pytest.approx(50)


def test_contributions_sum_to_overall():
    for hub in score(METRICS, CONFIG).hubs:
        total = sum(m.contribution for hz in hub.hazards for m in hz.metrics)
        assert total == pytest.approx(hub.overall)


def test_all_equal_metric_normalizes_to_zero():
    assert min_max({"a": 4.0, "b": 4.0}) == {"a": 0.0, "b": 0.0}


def test_deterministic_output_and_hashes():
    first = score(METRICS, CONFIG)
    second = score(copy.deepcopy(METRICS), CONFIG)
    assert first.model_dump() == second.model_dump()


def test_data_hash_changes_with_input():
    changed = copy.deepcopy(METRICS)
    changed["x"]["m1"] = 1.0
    assert score(changed, CONFIG).data_hash != score(METRICS, CONFIG).data_hash


def test_ties_broken_by_hub_id():
    tied = {"b": {"m1": 1.0, "m2": 1.0, "m3": 1.0}, "a": {"m1": 1.0, "m2": 1.0, "m3": 1.0}}
    assert [h.hub_id for h in score(tied, CONFIG).hubs] == ["a", "b"]


@pytest.mark.parametrize("metric", ["m1", "m2", "m3"])
def test_raising_a_metric_never_lowers_that_hubs_hazard_score(metric):
    base = _by_id(score(METRICS, CONFIG))["y"]
    bumped_input = copy.deepcopy(METRICS)
    bumped_input["y"][metric] += 3.0
    bumped = _by_id(score(bumped_input, CONFIG))["y"]
    for before, after in zip(base.hazards, bumped.hazards):
        if metric in {m.metric for m in before.metrics}:
            assert after.sub_score >= before.sub_score


def test_missing_metric_raises():
    with pytest.raises(ValueError, match="missing metrics"):
        score({"x": {"m1": 1.0}}, CONFIG)
