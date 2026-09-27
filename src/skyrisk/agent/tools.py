"""Deterministic agent tools over the SQLite score store. No LLM code here.

Every tool has a Pydantic input model (the contract the LLM must satisfy) and a
Pydantic result model carrying `caveats`, so assumptions and data limits travel
with every number the agent can quote.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date
from typing import Any, Literal, get_args

from pydantic import BaseModel, Field, ValidationError, field_validator

from skyrisk import db
from skyrisk.config import HubRegistry, Region, ScoringConfig
from skyrisk.models import NRI_HAZARDS
from skyrisk.scoring import metrics

Hazard = Literal["overall", "winter", "hurricane", "flood", "tornado", "heat"]
Stat = Literal["snow_day", "heavy_snow", "extreme_heat", "extreme_cold", "heavy_rain", "high_wind"]
Unit = Literal["pct_days", "days_per_year"]

SCORE_DECIMALS = 2

RELATIVE_CAVEAT = (
    "Scores are relative rankings across the 13 hubs (0 = least exposed hub, "
    "100 = most exposed hub for that measure), not probabilities of disruption."
)
NRI_CAVEAT = (
    "FEMA NRI values describe the whole county containing the hub, not the hub site itself."
)


class ToolError(Exception):
    """A tool could not answer; the message is safe to show to the model."""


@dataclass(frozen=True)
class ToolContext:
    conn: sqlite3.Connection
    registry: HubRegistry
    scoring: ScoringConfig


class ScoreRef(BaseModel):
    """A score a tool returned; the agent's answer may only cite these."""

    hub_id: str
    hazard: str
    score: float


class ToolResult(BaseModel):
    caveats: list[str]

    def scores(self) -> list[ScoreRef]:
        return []


# --- helpers -----------------------------------------------------------------------

def _latest_run(ctx: ToolContext) -> tuple[int, str]:
    run_id = db.latest_run_id(ctx.conn)
    if run_id is None:
        raise ToolError("No score run exists yet; an operator must run `skyrisk score`.")
    row = ctx.conn.execute(
        "SELECT config_version FROM score_runs WHERE run_id = ?", (run_id,)
    ).fetchone()
    return run_id, row["config_version"]


def _check_hubs(ctx: ToolContext, hub_ids: list[str]) -> None:
    known = {h.id for h in ctx.registry.hubs}
    unknown = [h for h in hub_ids if h not in known]
    if unknown:
        raise ToolError(f"Unknown hub id(s) {unknown}. Valid ids: {sorted(known)}")


def _window_caveat(ctx: ToolContext) -> str:
    w = ctx.scoring.window
    return f"Weather data covers {w.start.isoformat()} to {w.end.isoformat()} (Open-Meteo daily history)."


def _round(value: float) -> float:
    return round(value, SCORE_DECIMALS)


def _hazard_scores(ctx: ToolContext, run_id: int, hazard: str) -> dict[str, float]:
    if hazard == "overall":
        rows = ctx.conn.execute(
            "SELECT hub_id, overall AS score FROM hub_scores WHERE run_id = ?", (run_id,)
        )
    else:
        rows = ctx.conn.execute(
            "SELECT hub_id, sub_score AS score FROM hazard_scores WHERE run_id = ? AND hazard = ?",
            (run_id, hazard),
        )
    return {r["hub_id"]: r["score"] for r in rows}


def _ordered(scores: dict[str, float]) -> list[str]:
    return sorted(scores, key=lambda h: (-scores[h], h))


# --- list_hubs -----------------------------------------------------------------------

class ListHubsInput(BaseModel):
    pass


class HubInfo(BaseModel):
    hub_id: str
    name: str
    city: str
    state: str
    region: str


class ListHubsResult(ToolResult):
    hubs: list[HubInfo]


def list_hubs(ctx: ToolContext, _: ListHubsInput) -> ListHubsResult:
    return ListHubsResult(
        hubs=[
            HubInfo(hub_id=h.id, name=h.name, city=h.city, state=h.state, region=h.region)
            for h in ctx.registry.hubs
        ],
        caveats=[],
    )


# --- rank_hubs -----------------------------------------------------------------------

class RankHubsInput(BaseModel):
    hazard: Hazard = Field(description="'overall' or one hazard sub-score")
    top_n: int | None = Field(default=None, ge=1, le=13, description="Limit rows; null = all")
    region: Region | None = Field(default=None, description="Only hubs in this region; null = all")


class RankRow(BaseModel):
    hub_id: str
    name: str
    region: str
    score: float
    rank: int  # within the region filter (if any)
    overall_rank: int  # among all 13 hubs for this hazard


class RankHubsResult(ToolResult):
    run_id: int
    config_version: str
    hazard: str
    region: str | None
    rows: list[RankRow]

    def scores(self) -> list[ScoreRef]:
        return [ScoreRef(hub_id=r.hub_id, hazard=self.hazard, score=r.score) for r in self.rows]


def rank_hubs(ctx: ToolContext, args: RankHubsInput) -> RankHubsResult:
    run_id, version = _latest_run(ctx)
    scores = _hazard_scores(ctx, run_id, args.hazard)
    overall_order = _ordered(scores)
    hubs = {h.id: h for h in ctx.registry.hubs}
    selected = [h for h in overall_order if args.region is None or hubs[h].region == args.region]
    if args.top_n is not None:
        selected = selected[: args.top_n]
    rows = [
        RankRow(
            hub_id=h, name=hubs[h].name, region=hubs[h].region, score=_round(scores[h]),
            rank=i, overall_rank=overall_order.index(h) + 1,
        )
        for i, h in enumerate(selected, start=1)
    ]
    caveats = [RELATIVE_CAVEAT, NRI_CAVEAT, _window_caveat(ctx)]
    if args.region:
        caveats.append(
            f"Filtered to the {args.region} region; scores are still relative to all 13 hubs "
            "(overall_rank shows the position among all hubs)."
        )
    return RankHubsResult(
        run_id=run_id, config_version=version, hazard=args.hazard, region=args.region,
        rows=rows, caveats=caveats,
    )


# --- compare_hubs --------------------------------------------------------------------

class CompareHubsInput(BaseModel):
    hub_ids: list[str] = Field(min_length=2, max_length=5, description="2-5 hub ids")

    @field_validator("hub_ids")
    @classmethod
    def _unique(cls, v: list[str]) -> list[str]:
        if len(set(v)) != len(v):
            raise ValueError("hub_ids must be unique")
        return v


class CompareRow(BaseModel):
    hub_id: str
    overall: float
    overall_rank: int
    hazards: dict[str, float]


class CompareHubsResult(ToolResult):
    run_id: int
    config_version: str
    rows: list[CompareRow]

    def scores(self) -> list[ScoreRef]:
        refs = []
        for r in self.rows:
            refs.append(ScoreRef(hub_id=r.hub_id, hazard="overall", score=r.overall))
            refs += [ScoreRef(hub_id=r.hub_id, hazard=h, score=s) for h, s in r.hazards.items()]
        return refs


def compare_hubs(ctx: ToolContext, args: CompareHubsInput) -> CompareHubsResult:
    _check_hubs(ctx, args.hub_ids)
    run_id, version = _latest_run(ctx)
    rows = []
    for hub_id in args.hub_ids:
        hub = ctx.conn.execute(
            "SELECT overall, rank FROM hub_scores WHERE run_id = ? AND hub_id = ?", (run_id, hub_id)
        ).fetchone()
        hazards = {
            r["hazard"]: _round(r["sub_score"])
            for r in ctx.conn.execute(
                "SELECT hazard, sub_score FROM hazard_scores WHERE run_id = ? AND hub_id = ? ORDER BY hazard",
                (run_id, hub_id),
            )
        }
        rows.append(CompareRow(hub_id=hub_id, overall=_round(hub["overall"]),
                               overall_rank=hub["rank"], hazards=hazards))
    return CompareHubsResult(
        run_id=run_id, config_version=version, rows=rows,
        caveats=[RELATIVE_CAVEAT, NRI_CAVEAT, _window_caveat(ctx)],
    )


# --- explain_score -------------------------------------------------------------------

class ExplainScoreInput(BaseModel):
    hub_id: str
    hazard: Hazard | None = Field(default=None, description="One hazard, or null for all")


class MetricRow(BaseModel):
    metric: str
    raw: float
    normalized: float
    points: float  # contribution to the overall score
    note: str | None


class HazardExplanation(BaseModel):
    hazard: str
    weight: float
    sub_score: float
    metrics: list[MetricRow]


class NriInfo(BaseModel):
    county: str
    state: str
    county_fips: str
    area_sqmi: float
    nri_version: str
    afreq: dict[str, float | None]
    risks_reference_only: dict[str, float | None]


class ExplainScoreResult(ToolResult):
    run_id: int
    config_version: str
    hub_id: str
    overall: float
    overall_rank: int
    hub_count: int
    hazards: list[HazardExplanation]
    unscored_metrics: dict[str, float]
    nri: NriInfo | None

    def scores(self) -> list[ScoreRef]:
        refs = [ScoreRef(hub_id=self.hub_id, hazard="overall", score=self.overall)]
        refs += [ScoreRef(hub_id=self.hub_id, hazard=h.hazard, score=h.sub_score) for h in self.hazards]
        return refs


def explain_score(ctx: ToolContext, args: ExplainScoreInput) -> ExplainScoreResult:
    _check_hubs(ctx, [args.hub_id])
    run_id, version = _latest_run(ctx)
    hub = ctx.conn.execute(
        "SELECT overall, rank FROM hub_scores WHERE run_id = ? AND hub_id = ?", (run_id, args.hub_id)
    ).fetchone()
    hub_count = ctx.conn.execute(
        "SELECT COUNT(*) FROM hub_scores WHERE run_id = ?", (run_id,)
    ).fetchone()[0]

    hazards = []
    notes: list[str] = []
    for hz in ctx.conn.execute(
        "SELECT hazard, sub_score FROM hazard_scores WHERE run_id = ? AND hub_id = ? ORDER BY hazard",
        (run_id, args.hub_id),
    ).fetchall():
        if args.hazard not in (None, "overall") and hz["hazard"] != args.hazard:
            continue
        rows = ctx.conn.execute(
            """SELECT metric, raw, normalized, contribution, note FROM metric_values
               WHERE run_id = ? AND hub_id = ? AND hazard = ? ORDER BY metric""",
            (run_id, args.hub_id, hz["hazard"]),
        ).fetchall()
        metric_rows = [
            MetricRow(metric=m["metric"], raw=round(m["raw"], 4), normalized=_round(m["normalized"]),
                      points=_round(m["contribution"]), note=m["note"])
            for m in rows
        ]
        notes += [m.note for m in metric_rows if m.note]
        hazards.append(HazardExplanation(
            hazard=hz["hazard"], weight=ctx.scoring.hazards[hz["hazard"]].weight,
            sub_score=_round(hz["sub_score"]), metrics=metric_rows,
        ))

    unscored = {
        r["metric"]: round(r["raw"], 4)
        for r in ctx.conn.execute(
            "SELECT metric, raw FROM hub_metrics WHERE run_id = ? AND hub_id = ? AND scored = 0 ORDER BY metric",
            (run_id, args.hub_id),
        )
    }

    county = db.load_nri(ctx.conn, args.hub_id)
    nri = None
    caveats = [RELATIVE_CAVEAT, NRI_CAVEAT, _window_caveat(ctx)]
    if county:
        nri = NriInfo(
            county=county.county_name, state=county.state, county_fips=county.county_fips,
            area_sqmi=county.area_sqmi, nri_version=county.nri_version,
            afreq={h.lower(): county.afreq[h.lower()] for h in NRI_HAZARDS},
            risks_reference_only={h.lower(): county.risks[h.lower()] for h in NRI_HAZARDS},
        )
        caveats.append(
            f"NRI county: {county.county_name} County, {county.state} ({county.area_sqmi:,.0f} sq mi). "
            "*_RISKS values are shown for reference only and are not used in scoring."
        )
        if county.area_sqmi < ctx.scoring.nri_area_floor_sqmi:
            caveats.append(
                f"Tornado frequency is divided by max(county area, {ctx.scoring.nri_area_floor_sqmi:,.0f} sq mi); "
                f"this county is smaller than the floor, so its per-area rate rests on few events."
            )
    caveats += notes

    return ExplainScoreResult(
        run_id=run_id, config_version=version, hub_id=args.hub_id, overall=_round(hub["overall"]),
        overall_rank=hub["rank"], hub_count=hub_count, hazards=hazards,
        unscored_metrics=unscored, nri=nri, caveats=caveats,
    )


# --- weather_stat --------------------------------------------------------------------

class WeatherStatInput(BaseModel):
    hub_ids: list[str] = Field(min_length=1, max_length=13)
    stat: Stat
    unit: Unit = Field(description="pct_days = % of days; days_per_year = average days per year")
    months: list[int] | None = Field(default=None, description="Restrict to these months (1-12); null = all")
    year: int | None = Field(default=None, description="Restrict to one calendar year; null = whole window")

    @field_validator("months")
    @classmethod
    def _months(cls, v: list[int] | None) -> list[int] | None:
        if v is not None and (not v or any(m < 1 or m > 12 for m in v)):
            raise ValueError("months must be a non-empty list of integers 1-12")
        return v


class StatRow(BaseModel):
    hub_id: str
    value: float
    matching_days: int
    days_with_data: int


class WeatherStatResult(ToolResult):
    stat: str
    unit: str
    definition: str
    period_start: date
    period_end: date
    months: list[int] | None
    rows: list[StatRow]


def _stat_definition(stat: Stat, s: ScoringConfig) -> tuple[str, Callable[[float], bool], str]:
    t = s.thresholds
    return {
        "snow_day": ("snowfall_cm", lambda v: v >= t.snow_day_cm, f"snowfall >= {t.snow_day_cm:g} cm"),
        "heavy_snow": ("snowfall_cm", lambda v: v >= t.heavy_snow_cm, f"snowfall >= {t.heavy_snow_cm:g} cm"),
        "extreme_heat": ("temp_max_c", lambda v: v >= t.extreme_heat_c, f"daily max temperature >= {t.extreme_heat_c:g} C"),
        "extreme_cold": ("temp_min_c", lambda v: v <= t.extreme_cold_c, f"daily min temperature <= {t.extreme_cold_c:g} C"),
        "heavy_rain": ("precip_mm", lambda v: v >= t.heavy_rain_mm, f"precipitation >= {t.heavy_rain_mm:g} mm"),
        "high_wind": ("wind_gust_max_kmh", lambda v: v >= t.high_wind_kmh, f"max wind gust >= {t.high_wind_kmh:g} km/h"),
    }[stat]


def weather_stat(ctx: ToolContext, args: WeatherStatInput) -> WeatherStatResult:
    _check_hubs(ctx, args.hub_ids)
    window = ctx.scoring.window
    start, end = window.start, window.end
    if args.year is not None:
        if not window.start.year <= args.year <= window.end.year:
            raise ToolError(
                f"Year {args.year} is outside the data window "
                f"({window.start.year}-{window.end.year}); no data is available for it."
            )
        start, end = max(start, date(args.year, 1, 1)), min(end, date(args.year, 12, 31))

    field, predicate, definition = _stat_definition(args.stat, ctx.scoring)
    rows = []
    for hub_id in args.hub_ids:
        days = db.load_weather(ctx.conn, hub_id, start, end)
        if args.months:
            days = [d for d in days if d.date.month in args.months]
        valid = [v for d in days if (v := getattr(d, field)) is not None]
        if not valid:
            raise ToolError(f"No {field} data for {hub_id} in the requested period.")
        compute = metrics.pct_days if args.unit == "pct_days" else metrics.days_per_year
        rows.append(StatRow(
            hub_id=hub_id, value=_round(compute(days, field, predicate)),
            matching_days=sum(1 for v in valid if predicate(v)), days_with_data=len(valid),
        ))

    period = f"{start.isoformat()} to {end.isoformat()}"
    caveats = [
        f"Definition: a qualifying day has {definition} (Open-Meteo daily data at the hub's coordinates).",
        f"Period used: {period}" + (f", months {args.months}" if args.months else "") + ".",
        "Open-Meteo historical data is gridded reanalysis; local station records can differ.",
    ]
    if args.year is not None:
        caveats.append(f"Computed for calendar year {args.year} only, not the full {window.start.year}-{window.end.year} window.")
    return WeatherStatResult(
        stat=args.stat, unit=args.unit, definition=definition, period_start=start, period_end=end,
        months=args.months, rows=rows, caveats=caveats,
    )


# --- registry ------------------------------------------------------------------------

@dataclass(frozen=True)
class ToolSpec:
    name: str
    description: str
    input_model: type[BaseModel]
    fn: Callable[[ToolContext, Any], ToolResult]

    def json_schema(self) -> dict[str, Any]:
        return strict_json_schema(self.input_model)


TOOLS: tuple[ToolSpec, ...] = (
    ToolSpec("list_hubs", "List the 13 hubs with ids, cities and regions.", ListHubsInput, list_hubs),
    ToolSpec(
        "rank_hubs",
        "Rank hubs by the overall risk score or one hazard sub-score (0-100, relative across hubs). "
        "Optionally filter to one region. Use for 'which hubs are most exposed to X'.",
        RankHubsInput, rank_hubs,
    ),
    ToolSpec(
        "compare_hubs",
        "Compare 2-5 hubs side by side: overall score, overall rank and every hazard sub-score.",
        CompareHubsInput, compare_hubs,
    ),
    ToolSpec(
        "explain_score",
        "Explain why a hub scores what it does: per-hazard sub-scores, the raw metrics behind them, "
        "their normalized values and points, notes, and the FEMA NRI county used.",
        ExplainScoreInput, explain_score,
    ),
    ToolSpec(
        "weather_stat",
        "Historical weather statistics per hub from daily data, e.g. percent of days with snowfall "
        "or average extreme-heat days per year. Optionally restrict to one calendar year or to months.",
        WeatherStatInput, weather_stat,
    ),
)
TOOLS_BY_NAME = {t.name: t for t in TOOLS}


@dataclass(frozen=True)
class ToolOutcome:
    content: str  # JSON sent back to the model
    is_error: bool
    result: ToolResult | None


def run_tool(ctx: ToolContext, name: str, arguments: dict[str, Any]) -> ToolOutcome:
    """Validate arguments with Pydantic, run the tool, and never raise to the caller."""
    spec = TOOLS_BY_NAME.get(name)
    if spec is None:
        return ToolOutcome(f"Unknown tool {name!r}. Available: {sorted(TOOLS_BY_NAME)}", True, None)
    try:
        args = spec.input_model.model_validate(arguments)
    except ValidationError as e:
        return ToolOutcome(f"Invalid arguments for {name}: {e.errors(include_url=False)}", True, None)
    try:
        result = spec.fn(ctx, args)
    except ToolError as e:
        return ToolOutcome(str(e), True, None)
    return ToolOutcome(result.model_dump_json(), False, result)


# --- strict schema -------------------------------------------------------------------

_UNSUPPORTED_KEYWORDS = {
    "default", "title", "minimum", "maximum", "exclusiveMinimum", "exclusiveMaximum",
    "minItems", "maxItems", "minLength", "maxLength", "pattern", "format",
}


def strict_json_schema(model: type[BaseModel]) -> dict[str, Any]:
    """JSON schema accepted by both providers' strict modes.

    Inlines $refs, requires every property (optional ones stay nullable), forbids
    extra properties and drops constraint keywords the strict modes may reject.
    Pydantic validation stays the authoritative check on every call.
    """
    schema = model.model_json_schema()
    defs = schema.pop("$defs", {})

    def walk(node: Any) -> Any:
        if isinstance(node, list):
            return [walk(n) for n in node]
        if not isinstance(node, dict):
            return node
        if "$ref" in node:
            return walk(defs[node["$ref"].split("/")[-1]])
        out = {k: walk(v) for k, v in node.items() if k not in _UNSUPPORTED_KEYWORDS}
        if out.get("type") == "object" or "properties" in out:
            out["type"] = "object"
            out.setdefault("properties", {})
            out["required"] = list(out["properties"])
            out["additionalProperties"] = False
        return out

    return walk(schema)


def region_values() -> tuple[str, ...]:
    return get_args(Region)
