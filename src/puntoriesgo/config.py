"""Configuración central de la aplicación.

Prioridad de valores (de menor a mayor):
    1. Valores por defecto definidos aquí.
    2. Archivo ``config.json`` en el directorio de datos de la app.
    3. Variables de entorno ``PUNTORIESGO_*``.

En un build móvil de Flet, el directorio de datos persistente lo entrega la
variable ``FLET_APP_STORAGE_DATA``; en escritorio se usa ``~/.puntoriesgo``.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field, fields
from pathlib import Path
from typing import Optional


# --------------------------------------------------------------------------- #
# Fuentes de mapas base (satelitales de alta resolución)
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class TileSource:
    """Proveedor de tiles XYZ.

    ``url_template`` acepta los marcadores ``{z}``, ``{x}``, ``{y}`` y, si
    corresponde, ``{token}``.
    """

    id: str
    name: str
    url_template: str
    attribution: str
    max_native_zoom: int = 19
    tile_size: int = 256
    requires_token: bool = False

    def url(self, z: int, x: int, y: int, token: Optional[str] = None) -> str:
        return self.url_template.format(z=z, x=x, y=y, token=token or "")


TILE_SOURCES: dict[str, TileSource] = {
    "esri_world_imagery": TileSource(
        id="esri_world_imagery",
        name="ESRI World Imagery",
        # Ojo: ESRI usa el orden {z}/{y}/{x}.
        url_template=(
            "https://server.arcgisonline.com/ArcGIS/rest/services/"
            "World_Imagery/MapServer/tile/{z}/{y}/{x}"
        ),
        attribution="Tiles &copy; Esri &mdash; Esri, Maxar, Earthstar Geographics",
        max_native_zoom=19,
    ),
    "mapbox_satellite": TileSource(
        id="mapbox_satellite",
        name="Mapbox Satellite",
        url_template=(
            "https://api.mapbox.com/styles/v1/mapbox/satellite-v9/tiles/256/"
            "{z}/{x}/{y}@2x?access_token={token}"
        ),
        attribution="&copy; Mapbox &copy; OpenStreetMap &copy; Maxar",
        max_native_zoom=20,
        requires_token=True,
    ),
}


# --------------------------------------------------------------------------- #
# Configuración de la app
# --------------------------------------------------------------------------- #
def _default_data_dir() -> Path:
    env = os.environ.get("FLET_APP_STORAGE_DATA")
    if env:
        return Path(env)
    return Path.home() / ".puntoriesgo"


def _assets_dir() -> Path:
    # src/puntoriesgo/config.py -> src/assets
    return Path(__file__).resolve().parent.parent / "assets"


@dataclass
class AppConfig:
    data_dir: Path = field(default_factory=_default_data_dir)
    assets_dir: Path = field(default_factory=_assets_dir)

    # Capa vectorial de sectores y equipos de riego (GeoJSON, KML o KMZ).
    # Si es relativa, se busca primero en data_dir y luego en assets/data.
    vector_layer_path: str = "el_amanecer.geojson"

    # Mapa base
    tile_source_id: str = "esri_world_imagery"
    mapbox_token: str = ""

    # Pre-descarga offline del área de trabajo
    offline_min_zoom: int = 12
    offline_max_zoom: int = 18
    offline_buffer_m: float = 250.0
    offline_max_tiles: int = 60_000
    offline_workers: int = 6

    # Cruce espacial
    sector_tolerance_m: float = 25.0  # si el GPS cae fuera, sector más cercano dentro de esta distancia

    # Backend de sincronización: "supabase" o "none"
    backend: str = "supabase"
    supabase_url: str = ""
    supabase_key: str = ""
    supabase_table: str = "alerts"
    supabase_bucket: str = "alert-photos"
    sync_interval_s: float = 30.0

    # Fotos
    photo_max_side_px: int = 1600
    photo_jpeg_quality: int = 70

    # Desarrollo: "lat,lon" para simular GPS en escritorio.
    fake_gps: str = ""

    # ------------------------------------------------------------------ #
    @property
    def tile_source(self) -> TileSource:
        return TILE_SOURCES[self.tile_source_id]

    @property
    def db_path(self) -> Path:
        return self.data_dir / "puntoriesgo.sqlite3"

    @property
    def photos_dir(self) -> Path:
        return self.data_dir / "photos"

    @property
    def tiles_dir(self) -> Path:
        return self.data_dir / "tiles"

    @property
    def web_dir(self) -> Path:
        return self.assets_dir / "web"

    def resolve_vector_layer(self) -> Path:
        p = Path(self.vector_layer_path)
        if p.is_absolute():
            return p
        for base in (self.data_dir, self.assets_dir / "data"):
            if (base / p).exists():
                return base / p
        return self.assets_dir / "data" / p

    def ensure_dirs(self) -> None:
        for d in (self.data_dir, self.photos_dir, self.tiles_dir):
            d.mkdir(parents=True, exist_ok=True)

    # ------------------------------------------------------------------ #
    @classmethod
    def load(cls) -> "AppConfig":
        cfg = cls()
        if "PUNTORIESGO_DATA_DIR" in os.environ:  # define dónde buscar config.json
            cfg.data_dir = Path(os.environ["PUNTORIESGO_DATA_DIR"])
        cfg_file = cfg.data_dir / "config.json"
        if cfg_file.exists():
            cfg._apply(json.loads(cfg_file.read_text(encoding="utf-8")))
        env = {
            f.name: os.environ[f"PUNTORIESGO_{f.name.upper()}"]
            for f in fields(cls)
            if f"PUNTORIESGO_{f.name.upper()}" in os.environ
        }
        cfg._apply(env)
        if cfg.tile_source_id not in TILE_SOURCES:
            raise ValueError(f"tile_source_id desconocido: {cfg.tile_source_id}")
        cfg.ensure_dirs()
        return cfg

    def _apply(self, values: dict) -> None:
        for f in fields(self):
            if f.name not in values:
                continue
            raw = values[f.name]
            current = getattr(self, f.name)
            if isinstance(current, Path):
                setattr(self, f.name, Path(raw))
            elif isinstance(current, bool):
                setattr(self, f.name, str(raw).lower() in ("1", "true", "yes", "si"))
            elif isinstance(current, int):
                setattr(self, f.name, int(raw))
            elif isinstance(current, float):
                setattr(self, f.name, float(raw))
            else:
                setattr(self, f.name, raw)
