"""FEMA National Risk Index (county level) client.

SkyRisk scores on annualized frequency (*_AFREQ): how often a hazard occurs at the
location. The composite *_RISKS score is stored for reference only because it is
driven by population/building exposure and social vulnerability.
"""

from __future__ import annotations

import httpx

from skyrisk.config import Hub
from skyrisk.ingest.http import get_json
from skyrisk.models import NRI_HAZARDS, NriCounty

NRI_COUNTIES_QUERY_URL = (
    "https://services.arcgis.com/XG15cJAlne2vxtgt/arcgis/rest/services/"
    "National_Risk_Index_Counties/FeatureServer/0/query"
)
OUT_FIELDS = ["NRI_VER", "STCOFIPS", "COUNTY", "STATE", "AREA"] + [
    f"{h}_{suffix}" for h in NRI_HAZARDS for suffix in ("AFREQ", "RISKS")
]


class NriLookupError(RuntimeError):
    pass


def parse_query(payload: dict) -> NriCounty:
    if "error" in payload:
        raise NriLookupError(f"NRI service error: {payload['error']}")
    features = payload.get("features") or []
    if len(features) != 1:
        raise NriLookupError(f"expected exactly 1 county, got {len(features)}")
    attrs = features[0]["attributes"]
    missing = [f for f in OUT_FIELDS if f not in attrs]
    if missing:
        raise NriLookupError(f"NRI response missing fields: {missing}")
    return NriCounty(
        county_fips=attrs["STCOFIPS"],
        county_name=attrs["COUNTY"],
        state=attrs["STATE"],
        nri_version=attrs["NRI_VER"],
        area_sqmi=attrs["AREA"],
        afreq={h.lower(): attrs[f"{h}_AFREQ"] for h in NRI_HAZARDS},
        risks={h.lower(): attrs[f"{h}_RISKS"] for h in NRI_HAZARDS},
    )


def fetch_county(hub: Hub, client: httpx.Client) -> NriCounty:
    params = {
        "geometry": f"{hub.lon},{hub.lat}",
        "geometryType": "esriGeometryPoint",
        "inSR": 4326,
        "spatialRel": "esriSpatialRelIntersects",
        "outFields": ",".join(OUT_FIELDS),
        "returnGeometry": "false",
        "f": "json",
    }
    return parse_query(get_json(client, NRI_COUNTIES_QUERY_URL, params))
