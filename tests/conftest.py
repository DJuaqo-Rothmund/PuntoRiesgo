import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

SAMPLE = ROOT / "src" / "assets" / "data" / "sectores_riego.geojson"


@pytest.fixture
def sample_path() -> Path:
    return SAMPLE


@pytest.fixture
def cfg(tmp_path):
    from puntoriesgo.config import AppConfig

    c = AppConfig(data_dir=tmp_path / "data", vector_layer_path=str(SAMPLE),
                  backend="none", sync_interval_s=3600)
    c.ensure_dirs()
    return c
