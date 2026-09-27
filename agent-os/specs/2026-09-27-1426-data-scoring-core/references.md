# References for Data + Scoring Core

## Similar Implementations

None. This is a greenfield repo.

## External Data Sources

### Open-Meteo Historical Weather (Archive API)

- **Location:** https://archive-api.open-meteo.com/v1/archive (docs: https://open-meteo.com/en/docs/historical-weather-api)
- **Relevance:** daily snowfall, temperature max/min, precipitation and max wind gust per hub
- **Key patterns:** one request per hub for the full date range; `timezone=auto`; metric units

### FEMA National Risk Index — Counties

- **Location:** FeatureServer https://services.arcgis.com/XG15cJAlne2vxtgt/arcgis/rest/services/National_Risk_Index_Counties/FeatureServer/0 — ArcGIS item `39485e8035d446a5bff03259508ae355` (https://www.arcgis.com/home/item.html?id=39485e8035d446a5bff03259508ae355); data overview at https://www.fema.gov/about/openfema/data-sets/national-risk-index-data
- **Relevance:** county-level annualized hazard frequency (`*_AFREQ`) for hurricane, inland (IFLD) and coastal flood, tornado, winter weather, heat wave and cold wave
- **Key patterns:** point-in-polygon query using the hub's lat/lon; store `*_AFREQ` (scored) and `*_RISKS` (reference only)

### NRI Technical Documentation v1.20 (Dec 2025)

- **Location:** https://www.fema.gov/sites/default/files/documents/fema_national-risk-index_technical-documentation.pdf
- **Relevance:** data dictionary, used to confirm field names and definitions
