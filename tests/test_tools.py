from datetime import date

import pytest

from conftest import snowy
from skyrisk.agent.tools import (
    NRI_CAVEAT,
    TOOLS,
    RankHubsInput,
    ToolError,
    rank_hubs,
    relative_caveat,
    run_tool,
    strict_json_schema,
    weather_stat,
    WeatherStatInput,
)


def test_rank_overall_matches_hub_scores_order(tool_ctx):
    result = rank_hubs(tool_ctx, RankHubsInput(hazard="overall"))
    db_order = [r["hub_id"] for r in tool_ctx.conn.execute(
        "SELECT hub_id FROM hub_scores WHERE run_id = ? ORDER BY rank", (result.run_id,))]
    assert [r.hub_id for r in result.rows] == db_order
    assert relative_caveat(tool_ctx) in result.caveats and NRI_CAVEAT in result.caveats


def test_rank_region_filter_keeps_overall_rank(tool_ctx):
    everything = rank_hubs(tool_ctx, RankHubsInput(hazard="winter"))
    midwest = rank_hubs(tool_ctx, RankHubsInput(hazard="winter", region="Midwest"))
    expected = [r.hub_id for r in everything.rows if r.region == "Midwest"]
    assert [r.hub_id for r in midwest.rows] == expected
    assert [r.rank for r in midwest.rows] == list(range(1, len(expected) + 1))
    by_id = {r.hub_id: r for r in everything.rows}
    assert all(r.overall_rank == by_id[r.hub_id].rank for r in midwest.rows)
    assert any("Midwest" in c for c in midwest.caveats)


def test_rank_rejects_unknown_region(tool_ctx):
    outcome = run_tool(tool_ctx, "rank_hubs", {"hazard": "winter", "region": "Mars"})
    assert outcome.is_error and "region" in outcome.content


def test_weather_stat_single_year_matches_hand_count(tool_ctx):
    index = [h.id for h in tool_ctx.registry.hubs].index("denver")
    days_2025 = [date.fromordinal(o) for o in range(date(2025, 1, 1).toordinal(), date(2026, 1, 1).toordinal())]
    expected = 100 * sum(snowy(index, d) for d in days_2025) / len(days_2025)
    result = weather_stat(tool_ctx, WeatherStatInput(hub_ids=["denver"], stat="snow_day", unit="pct_days", year=2025))
    row = result.rows[0]
    assert row.days_with_data == 365
    assert row.value == pytest.approx(expected, abs=0.005)
    assert (result.period_start, result.period_end) == (date(2025, 1, 1), date(2025, 12, 31))
    assert any("2025" in c for c in result.caveats)
    assert any("snowfall >= 1 cm" in c for c in result.caveats)


def test_weather_stat_rejects_year_outside_window(tool_ctx):
    with pytest.raises(ToolError, match="outside the historical data window") as exc:
        weather_stat(tool_ctx, WeatherStatInput(hub_ids=["denver"], stat="snow_day", unit="pct_days", year=2014))
    assert "intentionally excluded" not in str(exc.value)


def test_weather_stat_2026_without_ytd_says_it_is_intentionally_excluded(tool_ctx):
    with pytest.raises(ToolError) as exc:
        weather_stat(tool_ctx, WeatherStatInput(hub_ids=["denver"], stat="snow_day", unit="pct_days", year=2026))
    message = str(exc.value)
    assert "2016-2025, full calendar years only" in message
    assert "2026 onward is intentionally excluded" in message and "near_term_risk" in message


def test_weather_stat_months_filter(tool_ctx):
    result = weather_stat(tool_ctx, WeatherStatInput(
        hub_ids=["chicago"], stat="snow_day", unit="pct_days", months=[1], year=2020))
    assert result.rows[0].days_with_data == 31


def test_unknown_hub_is_a_tool_error_not_a_crash(tool_ctx):
    outcome = run_tool(tool_ctx, "explain_score", {"hub_id": "no-such-hub", "hazard": None})
    assert outcome.is_error and "Unknown hub" in outcome.content


def test_invalid_arguments_rejected_before_running(tool_ctx):
    outcome = run_tool(tool_ctx, "compare_hubs", {"hub_ids": ["denver"]})
    assert outcome.is_error and "Invalid arguments" in outcome.content


def test_explain_score_scores_match_db_and_carry_notes(tool_ctx):
    outcome = run_tool(tool_ctx, "explain_score", {"hub_id": "chicago", "hazard": None})
    assert not outcome.is_error
    result = outcome.result
    sub = {s.hazard: s.score for s in result.scores()}
    db_sub = dict(tool_ctx.conn.execute(
        "SELECT hazard, sub_score FROM hazard_scores WHERE run_id = ? AND hub_id = 'chicago'", (result.run_id,)
    ).fetchall())
    assert all(sub[h] == pytest.approx(v, abs=0.005) for h, v in db_sub.items())
    assert result.nri is not None and "high_wind_days" in result.unscored_metrics
    assert any("County" in c for c in result.caveats)


def test_strict_schemas_are_closed_and_fully_required():
    for spec in TOOLS:
        schema = spec.json_schema()
        assert schema["additionalProperties"] is False
        assert set(schema["required"]) == set(schema["properties"])
        assert "$defs" not in schema and "default" not in str(schema)


def test_strict_schema_inlines_nested_models():
    from skyrisk.agent.schema import AgentAnswer

    schema = strict_json_schema(AgentAnswer)
    item = schema["properties"]["scores_cited"]["items"]
    assert item["additionalProperties"] is False and set(item["required"]) == {"hub_id", "hazard", "score"}


def test_hub_count_follows_the_registry(tool_ctx):
    n = len(tool_ctx.registry.hubs)
    assert f"across the {n} hubs" in relative_caveat(tool_ctx)
    everything = rank_hubs(tool_ctx, RankHubsInput(hazard="overall", top_n=n + 10))
    assert len(everything.rows) == n
    midwest = rank_hubs(tool_ctx, RankHubsInput(hazard="winter", region="Midwest"))
    assert any(f"all {n} hubs" in c for c in midwest.caveats)
    all_ids = [h.id for h in tool_ctx.registry.hubs]
    stats = weather_stat(tool_ctx, WeatherStatInput(hub_ids=all_ids, stat="snow_day", unit="pct_days"))
    assert len(stats.rows) == n
