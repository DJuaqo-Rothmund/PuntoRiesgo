"""Matemática de tiles Web Mercator (esquema XYZ "slippy map", EPSG:3857).

Funciones puras, sin dependencias, usadas por el downloader y por el servidor
local de tiles.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Iterator

EARTH_RADIUS_M = 6_371_008.8
MAX_LAT = 85.05112878


@dataclass(frozen=True)
class BBox:
    """Bounding box geográfico en grados (WGS84)."""

    min_lon: float
    min_lat: float
    max_lon: float
    max_lat: float

    def __post_init__(self) -> None:
        if self.min_lon > self.max_lon or self.min_lat > self.max_lat:
            raise ValueError(f"BBox inválido: {self}")

    def buffered(self, meters: float) -> "BBox":
        """Expande el bbox ``meters`` metros hacia cada lado."""
        dlat = math.degrees(meters / EARTH_RADIUS_M)
        mid_lat = math.radians((self.min_lat + self.max_lat) / 2)
        dlon = math.degrees(meters / (EARTH_RADIUS_M * max(math.cos(mid_lat), 1e-6)))
        return BBox(
            max(self.min_lon - dlon, -180.0),
            max(self.min_lat - dlat, -MAX_LAT),
            min(self.max_lon + dlon, 180.0),
            min(self.max_lat + dlat, MAX_LAT),
        )

    def intersects(self, other: "BBox") -> bool:
        return not (
            other.min_lon > self.max_lon
            or other.max_lon < self.min_lon
            or other.min_lat > self.max_lat
            or other.max_lat < self.min_lat
        )

    def contains_point(self, lon: float, lat: float) -> bool:
        return self.min_lon <= lon <= self.max_lon and self.min_lat <= lat <= self.max_lat

    @property
    def center(self) -> tuple[float, float]:
        """(lat, lon) del centro."""
        return ((self.min_lat + self.max_lat) / 2, (self.min_lon + self.max_lon) / 2)

    def as_leaflet(self) -> list[list[float]]:
        return [[self.min_lat, self.min_lon], [self.max_lat, self.max_lon]]

    @staticmethod
    def union(boxes: "list[BBox]") -> "BBox":
        if not boxes:
            raise ValueError("Lista de bbox vacía")
        return BBox(
            min(b.min_lon for b in boxes),
            min(b.min_lat for b in boxes),
            max(b.max_lon for b in boxes),
            max(b.max_lat for b in boxes),
        )


def lonlat_to_tile(lon: float, lat: float, z: int) -> tuple[int, int]:
    """Convierte lon/lat a índices de tile (x, y) en el zoom ``z``."""
    lat = max(min(lat, MAX_LAT), -MAX_LAT)
    n = 2**z
    x = int((lon + 180.0) / 360.0 * n)
    lat_r = math.radians(lat)
    y = int((1.0 - math.asinh(math.tan(lat_r)) / math.pi) / 2.0 * n)
    return min(max(x, 0), n - 1), min(max(y, 0), n - 1)


def tile_to_lonlat(x: int, y: int, z: int) -> tuple[float, float]:
    """Esquina noroeste (lon, lat) del tile."""
    n = 2**z
    lon = x / n * 360.0 - 180.0
    lat = math.degrees(math.atan(math.sinh(math.pi * (1 - 2 * y / n))))
    return lon, lat


def tile_bbox(x: int, y: int, z: int) -> BBox:
    west, north = tile_to_lonlat(x, y, z)
    east, south = tile_to_lonlat(x + 1, y + 1, z)
    return BBox(west, south, east, north)


def tile_range(bbox: BBox, z: int) -> tuple[int, int, int, int]:
    """(x_min, y_min, x_max, y_max) inclusivos que cubren el bbox en zoom z."""
    x0, y0 = lonlat_to_tile(bbox.min_lon, bbox.max_lat, z)
    x1, y1 = lonlat_to_tile(bbox.max_lon, bbox.min_lat, z)
    return x0, y0, x1, y1


def iter_tiles(bbox: BBox, z: int) -> Iterator[tuple[int, int, int]]:
    x0, y0, x1, y1 = tile_range(bbox, z)
    for x in range(x0, x1 + 1):
        for y in range(y0, y1 + 1):
            yield z, x, y


def count_tiles(bbox: BBox, min_zoom: int, max_zoom: int) -> int:
    total = 0
    for z in range(min_zoom, max_zoom + 1):
        x0, y0, x1, y1 = tile_range(bbox, z)
        total += (x1 - x0 + 1) * (y1 - y0 + 1)
    return total


def xyz_to_tms_y(y: int, z: int) -> int:
    """MBTiles almacena filas en esquema TMS (origen abajo-izquierda)."""
    return (2**z - 1) - y


def meters_per_pixel(lat: float, z: int, tile_size: int = 256) -> float:
    return 156543.03392 * math.cos(math.radians(lat)) / (2**z) * (256 / tile_size)
