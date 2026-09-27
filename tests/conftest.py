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
