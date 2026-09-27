"""Data records shared by ingestion, storage and scoring."""

from __future__ import annotations

from datetime import date

from pydantic import BaseModel

# FEMA NRI v1.20 hazard prefixes used by SkyRisk. IFLD (Inland Flooding) replaced
# RFLD (Riverine Flooding) in v1.20.
NRI_HAZARDS = ("HRCN", "IFLD", "CFLD", "TRND", "WNTW", "HWAV", "CWAV")

# Hazards whose county-level AFREQ is a count of localized events, which grows with
# county size. These are scored per 1,000 sq mi. Area-wide hazards (inland/coastal
# flooding, winter storms, heat/cold waves) and hurricanes, whose footprint exceeds
# any county, are scored on raw AFREQ.
NRI_AREA_NORMALIZED = ("TRND",)


class WeatherDay(BaseModel):
    date: date
    snowfall_cm: float | None
    temp_max_c: float | None
    temp_min_c: float | None
    precip_mm: float | None
    wind_gust_max_kmh: float | None


class NriCounty(BaseModel):
    county_fips: str
    county_name: str
    state: str
    nri_version: str
    area_sqmi: float
    # Keyed by lowercase hazard prefix, e.g. {"hrcn": 0.009, ...}. None = no data.
    afreq: dict[str, float | None]
    risks: dict[str, float | None]
