from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture
def config_dir() -> Path:
    return ROOT / "config"


@pytest.fixture
def fixtures() -> Path:
    return FIXTURES


# --- agent fixtures ----------------------------------------------------------------------

from datetime import date, timedelta  # noqa: E402

from skyrisk import db, pipeline  # noqa: E402
from skyrisk.agent.tools import ToolContext  # noqa: E402
from skyrisk.config import load_hubs, load_scoring_config  # noqa: E402
from skyrisk.models import NRI_HAZARDS, NriCounty, WeatherDay  # noqa: E402


def snowy(hub_index: int, day: date) -> bool:
    """Deterministic synthetic snow pattern: hub i snows on every (i+3)th day."""
    return day.toordinal() % (hub_index + 3) == 0


def build_scored_db():
    registry = load_hubs(ROOT / "config" / "hubs.yaml")
    scoring = load_scoring_config(ROOT / "config" / "scoring.yaml")
    conn = db.connect(":memory:")
    db.upsert_hubs(conn, registry.hubs)
    n = scoring.window.num_days
    for i, hub in enumerate(registry.hubs):
        days = []
        for k in range(n):
            d = scoring.window.start + timedelta(days=k)
            days.append(WeatherDay(
                date=d, snowfall_cm=2.0 if snowy(i, d) else 0.0, temp_max_c=20.0 + i, temp_min_c=5.0 - i,
                precip_mm=60.0 if k % (20 + i) == 0 else 1.0, wind_gust_max_kmh=30.0 + i * 5,
            ))
        db.upsert_weather(conn, hub.id, days)
        db.upsert_nri(conn, hub.id, NriCounty(
            county_fips=f"{i:05d}", county_name=f"{hub.city} Test", state=hub.state, nri_version="test",
            area_sqmi=500.0 + 100 * i,
            afreq={h.lower(): (None if h == "CFLD" and i % 2 else 0.1 * (i + 1)) for h in NRI_HAZARDS},
            risks={h.lower(): float(i) for h in NRI_HAZARDS},
        ))
    db.persist_run(conn, pipeline.compute_scores(conn, registry, scoring))
    return ToolContext(conn, registry, scoring)


@pytest.fixture(scope="module")
def tool_ctx():
    ctx = build_scored_db()
    yield ctx
    ctx.conn.close()
