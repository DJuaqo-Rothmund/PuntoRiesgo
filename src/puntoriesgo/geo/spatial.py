"""Cruce espacial GPS ↔ sectores ↔ equipos de riego.

Dado un punto GPS (lat, lon), :class:`SpatialIndex` responde:

* ¿En qué sector está? (point-in-polygon, con soporte de huecos y MultiPolygon).
  Si cae fuera de todos, devuelve el sector más cercano si está dentro de una
  tolerancia (útil por el error del GPS en bordes de cuartel).
* ¿Cuál es el equipo de riego más cercano? (distancia geodésica Haversine).
  Además se informa el más cercano **dentro del mismo sector**, que suele ser
  el que controla el riego de ese cuartel.

Dos motores intercambiables, con el mismo resultado:

* ``"shapely"`` – usa Shapely 2 + STRtree (rápido para miles de polígonos).
* ``"pure"``    – Python puro con prefiltro por bbox. Sin dependencias nativas:
  garantiza que la app compile en Android/iOS aunque Shapely no esté
  disponible en la plataforma.

Ambos trabajan en una proyección local equirectangular en metros centrada en
el área (error < 0,1 % en extensiones de decenas de km, suficiente para un
predio agrícola). Las distancias reportadas a equipos se recalculan con
Haversine.
"""

from __future__ import annotations

import math
from dataclasses import asdict, dataclass
from typing import Any, Optional, Sequence

from .tile_math import EARTH_RADIUS_M
from .vector_layers import VectorFeature, VectorLayers

Ring = list[tuple[float, float]]

# Propiedades de un sector que indican a qué equipo de riego pertenece.
SECTOR_EQUIPMENT_KEYS = ("equipo_riego", "equipo", "equipoRiego", "cabezal")  # coordenadas proyectadas (x, y) en metros


# --------------------------------------------------------------------------- #
# Utilidades geométricas
# --------------------------------------------------------------------------- #
def haversine_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Distancia geodésica en metros entre dos puntos WGS84."""
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = p2 - p1
    dl = math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * EARTH_RADIUS_M * math.asin(min(1.0, math.sqrt(a)))


class LocalProjection:
    """Proyección equirectangular local (lon/lat -> metros)."""

    def __init__(self, lat0: float, lon0: float):
        self.lat0 = lat0
        self.lon0 = lon0
        self._kx = math.radians(1) * EARTH_RADIUS_M * math.cos(math.radians(lat0))
        self._ky = math.radians(1) * EARTH_RADIUS_M

    def forward(self, lon: float, lat: float) -> tuple[float, float]:
        return ((lon - self.lon0) * self._kx, (lat - self.lat0) * self._ky)


def point_in_ring(x: float, y: float, ring: Sequence[tuple[float, float]]) -> bool:
    """Ray casting (regla par-impar). El borde cuenta como dentro."""
    inside = False
    n = len(ring)
    j = n - 1
    for i in range(n):
        xi, yi = ring[i]
        xj, yj = ring[j]
        if _on_segment(x, y, xi, yi, xj, yj):
            return True
        if (yi > y) != (yj > y):
            x_cross = (xj - xi) * (y - yi) / (yj - yi) + xi
            if x < x_cross:
                inside = not inside
        j = i
    return inside


def _on_segment(px, py, ax, ay, bx, by, eps: float = 1e-9) -> bool:
    cross = (px - ax) * (by - ay) - (py - ay) * (bx - ax)
    if abs(cross) > eps * max(1.0, abs(bx - ax) + abs(by - ay)):
        return False
    return min(ax, bx) - eps <= px <= max(ax, bx) + eps and min(ay, by) - eps <= py <= max(ay, by) + eps


def dist_point_segment(px, py, ax, ay, bx, by) -> float:
    dx, dy = bx - ax, by - ay
    if dx == 0 and dy == 0:
        return math.hypot(px - ax, py - ay)
    t = max(0.0, min(1.0, ((px - ax) * dx + (py - ay) * dy) / (dx * dx + dy * dy)))
    return math.hypot(px - (ax + t * dx), py - (ay + t * dy))


@dataclass
class _ProjPolygon:
    """Polígono proyectado: lista de (anillo exterior, [huecos])."""

    parts: list[tuple[Ring, list[Ring]]]
    min_x: float
    min_y: float
    max_x: float
    max_y: float

    @classmethod
    def from_geojson(cls, geom: dict[str, Any], proj: LocalProjection) -> "_ProjPolygon":
        polys = [geom["coordinates"]] if geom["type"] == "Polygon" else geom["coordinates"]
        parts: list[tuple[Ring, list[Ring]]] = []
        xs: list[float] = []
        ys: list[float] = []
        for poly in polys:
            rings = [[proj.forward(lon, lat) for lon, lat in ring] for ring in poly]
            if not rings or len(rings[0]) < 3:
                continue
            for r in rings:  # GeoJSON exige anillos cerrados; se tolera lo contrario.
                if r[0] != r[-1]:
                    r.append(r[0])
            parts.append((rings[0], rings[1:]))
            xs.extend(p[0] for p in rings[0])
            ys.extend(p[1] for p in rings[0])
        if not parts:
            raise ValueError("Polígono sin anillos válidos")
        return cls(parts, min(xs), min(ys), max(xs), max(ys))

    def contains(self, x: float, y: float) -> bool:
        if not (self.min_x <= x <= self.max_x and self.min_y <= y <= self.max_y):
            return False
        for outer, holes in self.parts:
            if point_in_ring(x, y, outer) and not any(
                point_in_ring(x, y, h) and not _on_ring_boundary(x, y, h) for h in holes
            ):
                return True
        return False

    def bbox_distance(self, x: float, y: float) -> float:
        dx = max(self.min_x - x, 0.0, x - self.max_x)
        dy = max(self.min_y - y, 0.0, y - self.max_y)
        return math.hypot(dx, dy)

    def boundary_distance(self, x: float, y: float) -> float:
        best = math.inf
        for outer, holes in self.parts:
            for ring in (outer, *holes):
                for i in range(len(ring) - 1):
                    ax, ay = ring[i]
                    bx, by = ring[i + 1]
                    best = min(best, dist_point_segment(x, y, ax, ay, bx, by))
        return best


def _on_ring_boundary(x: float, y: float, ring: Ring) -> bool:
    return any(
        _on_segment(x, y, *ring[i], *ring[i + 1]) for i in range(len(ring) - 1)
    )


# --------------------------------------------------------------------------- #
# Resultado
# --------------------------------------------------------------------------- #
@dataclass
class SpatialMatch:
    lat: float
    lon: float
    sector_id: Optional[str] = None
    sector_name: Optional[str] = None
    sector_inside: bool = False
    sector_distance_m: Optional[float] = None  # 0 si está dentro
    equipment_id: Optional[str] = None
    equipment_name: Optional[str] = None
    equipment_distance_m: Optional[float] = None
    equipment_in_sector_id: Optional[str] = None
    equipment_in_sector_name: Optional[str] = None
    equipment_in_sector_distance_m: Optional[float] = None
    engine: str = "pure"

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @property
    def sector_label(self) -> str:
        if not self.sector_name:
            return "Fuera de sectores"
        if self.sector_inside:
            return self.sector_name
        return f"{self.sector_name} (a {self.sector_distance_m:.0f} m)"

    @property
    def equipment_label(self) -> str:
        """Equipo más cercano; agrega el del propio sector si es otro."""
        if not self.equipment_name:
            return "Sin equipos cargados"
        if self.equipment_distance_m is None:  # asignado por atributo del sector
            return f"{self.equipment_name} (equipo del sector)"
        label = f"{self.equipment_name} ({self.equipment_distance_m:.0f} m)"
        if self.equipment_in_sector_id and self.equipment_in_sector_id != self.equipment_id:
            label += (f" · del sector: {self.equipment_in_sector_name} "
                      f"({self.equipment_in_sector_distance_m:.0f} m)")
        return label


# --------------------------------------------------------------------------- #
# Índice espacial
# --------------------------------------------------------------------------- #
def shapely_available() -> bool:
    try:
        import shapely  # noqa: F401
        from shapely import STRtree  # noqa: F401
    except Exception:  # noqa: BLE001
        return False
    return True


class SpatialIndex:
    """Índice de sectores (polígonos) y equipos (puntos) para consultas GPS."""

    def __init__(
        self,
        layers: VectorLayers,
        engine: str = "auto",
        sector_tolerance_m: float = 25.0,
    ):
        self.layers = layers
        self.sector_tolerance_m = sector_tolerance_m
        ext = layers.extent()
        lat0, lon0 = ext.center if ext else (0.0, 0.0)
        self.proj = LocalProjection(lat0, lon0)

        self.sectors: list[VectorFeature] = list(layers.sectors)
        self._polys = [_ProjPolygon.from_geojson(s.geometry, self.proj) for s in self.sectors]
        self.equipment: list[VectorFeature] = [
            e for e in layers.equipment if e.geometry["type"] == "Point"
        ]
        self._equip_xy = [
            self.proj.forward(*e.geometry["coordinates"][:2]) for e in self.equipment
        ]
        # Pre-cálculo: a qué sector pertenece cada equipo.
        self._equip_sector: list[Optional[int]] = [
            self._sector_containing_pure(x, y) for x, y in self._equip_xy
        ]

        if engine == "auto":
            engine = "shapely" if shapely_available() else "pure"
        if engine == "shapely" and not shapely_available():
            raise RuntimeError("Shapely no está instalado")
        self.engine = engine
        if engine == "shapely":
            self._build_shapely()

    # ------------------------------------------------------------------ #
    def locate(self, lat: float, lon: float) -> SpatialMatch:
        """Cruce espacial completo para una posición GPS."""
        x, y = self.proj.forward(lon, lat)
        match = SpatialMatch(lat=lat, lon=lon, engine=self.engine)

        # 1) Sector
        if self.engine == "shapely":
            idx, dist = self._sector_shapely(x, y)
        else:
            idx, dist = self._sector_pure(x, y)
        if idx is not None:
            s = self.sectors[idx]
            match.sector_id, match.sector_name = s.id, s.name
            match.sector_inside = dist == 0.0
            match.sector_distance_m = round(dist, 1)

        # 2) Equipo más cercano (global y dentro del sector)
        if not self.equipment and idx is not None:
            # Sin puntos de equipos: se usa el equipo declarado como atributo
            # del sector (p. ej. "equipo_riego": "Equipo 2" del plano de riego).
            props = self.sectors[idx].properties
            name = next((props[k] for k in SECTOR_EQUIPMENT_KEYS if props.get(k)), None)
            if name:
                match.equipment_id = str(props.get("equipo_id") or name)
                match.equipment_name = str(name)
        if self.equipment:
            if self.engine == "shapely":
                g_idx = self._nearest_equipment_shapely(x, y)
            else:
                g_idx = min(
                    range(len(self.equipment)),
                    key=lambda i: (self._equip_xy[i][0] - x) ** 2 + (self._equip_xy[i][1] - y) ** 2,
                )
            e = self.equipment[g_idx]
            match.equipment_id, match.equipment_name = e.id, e.name
            match.equipment_distance_m = round(self._geo_dist(lat, lon, e), 1)

            if idx is not None:
                same = [i for i, si in enumerate(self._equip_sector) if si == idx]
                if same:
                    s_idx = min(same, key=lambda i: self._geo_dist(lat, lon, self.equipment[i]))
                    se = self.equipment[s_idx]
                    match.equipment_in_sector_id = se.id
                    match.equipment_in_sector_name = se.name
                    match.equipment_in_sector_distance_m = round(
                        self._geo_dist(lat, lon, se), 1
                    )
        return match

    # ------------------------------------------------------------------ #
    @staticmethod
    def _geo_dist(lat: float, lon: float, e: VectorFeature) -> float:
        elon, elat = e.geometry["coordinates"][:2]
        return haversine_m(lat, lon, elat, elon)

    # -- motor puro ---------------------------------------------------- #
    def _sector_containing_pure(self, x: float, y: float) -> Optional[int]:
        for i, poly in enumerate(self._polys):
            if poly.contains(x, y):
                return i
        return None

    def _sector_pure(self, x: float, y: float) -> tuple[Optional[int], float]:
        inside = [i for i, p in enumerate(self._polys) if p.contains(x, y)]
        if inside:
            # Polígonos superpuestos: gana el de menor área (el más específico).
            return min(inside, key=lambda i: self._area(i)), 0.0
        best, best_d = None, math.inf
        for i, poly in enumerate(self._polys):
            if poly.bbox_distance(x, y) > self.sector_tolerance_m:
                continue
            d = poly.boundary_distance(x, y)
            if d < best_d:
                best, best_d = i, d
        if best is not None and best_d <= self.sector_tolerance_m:
            return best, best_d
        return None, math.inf

    def _area(self, i: int) -> float:
        total = 0.0
        for outer, holes in self._polys[i].parts:
            total += abs(_ring_area(outer)) - sum(abs(_ring_area(h)) for h in holes)
        return total

    # -- motor Shapely ------------------------------------------------- #
    def _build_shapely(self) -> None:
        from shapely import STRtree
        from shapely.geometry import MultiPolygon, Point, Polygon

        geoms = []
        for poly in self._polys:
            parts = [Polygon(outer, holes) for outer, holes in poly.parts]
            g = parts[0] if len(parts) == 1 else MultiPolygon(parts)
            if not g.is_valid:
                g = g.buffer(0)  # repara auto-intersecciones típicas de KML dibujados a mano
            geoms.append(g)
        self._sh_polys = geoms
        self._sh_tree = STRtree(geoms) if geoms else None
        self._sh_points = [Point(x, y) for x, y in self._equip_xy]
        self._sh_eq_tree = STRtree(self._sh_points) if self._sh_points else None
        self._Point = Point

    def _sector_shapely(self, x: float, y: float) -> tuple[Optional[int], float]:
        if self._sh_tree is None:
            return None, math.inf
        pt = self._Point(x, y)
        hits = [int(i) for i in self._sh_tree.query(pt, predicate="intersects")]
        if hits:
            return min(hits, key=lambda i: self._sh_polys[i].area), 0.0
        idx = self._sh_tree.nearest(pt)
        if idx is None:
            return None, math.inf
        idx = int(idx)
        d = self._sh_polys[idx].distance(pt)
        if d <= self.sector_tolerance_m:
            return idx, float(d)
        return None, math.inf

    def _nearest_equipment_shapely(self, x: float, y: float) -> int:
        return int(self._sh_eq_tree.nearest(self._Point(x, y)))


def _ring_area(ring: Ring) -> float:
    return 0.5 * sum(
        ring[i][0] * ring[i + 1][1] - ring[i + 1][0] * ring[i][1] for i in range(len(ring) - 1)
    )
