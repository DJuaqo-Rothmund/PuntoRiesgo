"""Lectura de capas vectoriales de sectores y equipos de riego.

Formatos soportados (sin dependencias nativas, apto para Android/iOS):

* GeoJSON (``.geojson`` / ``.json``): FeatureCollection con Polygon/MultiPolygon
  (sectores) y Point/MultiPoint (equipos de riego).
* KML (``.kml``) y KMZ (``.kmz``): Placemarks con Polygon, Point o
  MultiGeometry, incluyendo ``ExtendedData`` (Data y SchemaData/SimpleData).

Clasificación: los polígonos son **sectores** y los puntos son **equipos de
riego**. Si el archivo trae una propiedad ``layer``/``tipo`` explícita
(``sector`` / ``equipo``) se respeta. Las geometrías LineString (p. ej.
matrices de riego) se conservan como capa auxiliar sólo para visualización.

Todas las salidas se normalizan a GeoJSON (lon, lat en WGS84).
"""

from __future__ import annotations

import json
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Optional
from xml.etree import ElementTree as ET

from .tile_math import BBox

SECTOR = "sector"
EQUIPMENT = "equipment"
OTHER = "other"

# Propiedades candidatas (en orden de preferencia) para id y nombre.
ID_KEYS = ("id", "codigo", "code", "cod", "sector_id", "equipo_id", "fid")
NAME_KEYS = ("nombre", "name", "label", "title")
KIND_NAME_KEYS = {
    "sector": ("sector", "cuartel", "descripcion"),
    "equipment": ("equipo", "valvula", "descripcion"),
    "other": ("descripcion",),
}
LAYER_KEYS = ("layer", "capa", "tipo", "type", "kind")
_SECTOR_WORDS = {"sector", "sectores", "cuartel", "cuarteles", "lote", "bloque"}
_EQUIP_WORDS = {"equipo", "equipos", "riego", "valvula", "válvula", "bomba", "caseta",
                "cabezal", "equipment", "pump", "valve"}


@dataclass
class VectorFeature:
    id: str
    name: str
    kind: str  # SECTOR | EQUIPMENT | OTHER
    geometry: dict[str, Any]
    properties: dict[str, Any] = field(default_factory=dict)

    def to_geojson(self) -> dict[str, Any]:
        props = dict(self.properties)
        props.update({"_id": self.id, "_name": self.name, "_kind": self.kind})
        return {"type": "Feature", "id": self.id, "geometry": self.geometry, "properties": props}

    def bbox(self) -> BBox:
        xs: list[float] = []
        ys: list[float] = []
        for lon, lat in _iter_coords(self.geometry):
            xs.append(lon)
            ys.append(lat)
        return BBox(min(xs), min(ys), max(xs), max(ys))


@dataclass
class VectorLayers:
    sectors: list[VectorFeature] = field(default_factory=list)
    equipment: list[VectorFeature] = field(default_factory=list)
    other: list[VectorFeature] = field(default_factory=list)
    source_path: Optional[Path] = None
    name: Optional[str] = None  # nombre del predio (GeoJSON "name" / KML Document)

    def all(self) -> list[VectorFeature]:
        return [*self.sectors, *self.equipment, *self.other]

    def to_geojson(self) -> dict[str, Any]:
        return {"type": "FeatureCollection", "features": [f.to_geojson() for f in self.all()]}

    def extent(self) -> Optional[BBox]:
        feats = self.all()
        return BBox.union([f.bbox() for f in feats]) if feats else None

    def work_areas(self, buffer_m: float) -> list[BBox]:
        """Áreas de trabajo para la pre-descarga offline: bbox de cada sector + buffer.

        Los equipos que quedan fuera de todo sector también se incluyen para que
        el camino hasta ellos quede cacheado.
        """
        areas = [s.bbox().buffered(buffer_m) for s in self.sectors]
        for e in self.equipment:
            eb = e.bbox()
            if not any(a.contains_point(eb.min_lon, eb.min_lat) for a in areas):
                areas.append(eb.buffered(buffer_m))
        if not areas and (ext := self.extent()):
            areas.append(ext.buffered(buffer_m))
        return areas


# --------------------------------------------------------------------------- #
# API pública
# --------------------------------------------------------------------------- #
def load_vector_file(path: str | Path) -> VectorLayers:
    """Carga un GeoJSON/KML/KMZ y lo separa en sectores y equipos de riego."""
    path = Path(path)
    if not path.exists():
        raise FileNotFoundError(f"No existe la capa vectorial: {path}")
    suffix = path.suffix.lower()
    name: Optional[str] = None
    if suffix in (".geojson", ".json"):
        doc = json.loads(path.read_text(encoding="utf-8-sig"))
        name = doc.get("name") if isinstance(doc.get("name"), str) else None
        features = _parse_geojson(doc)
    elif suffix == ".kml":
        features = _parse_kml(path.read_bytes())
    elif suffix == ".kmz":
        with zipfile.ZipFile(path) as zf:
            kml_name = next(
                (n for n in zf.namelist() if n.lower().endswith(".kml")), None
            )
            if kml_name is None:
                raise ValueError("El KMZ no contiene ningún .kml")
            features = _parse_kml(zf.read(kml_name))
    else:
        raise ValueError(f"Formato no soportado: {suffix} (usa GeoJSON, KML o KMZ)")
    layers = _classify(features)
    layers.source_path = path
    layers.name = name or path.stem.replace("_", " ").title()
    return layers


def load_vector_bytes(data: bytes, filename: str) -> VectorLayers:
    """Igual que :func:`load_vector_file` pero desde memoria (archivos importados)."""
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        p = Path(tmp) / Path(filename).name
        p.write_bytes(data)
        return load_vector_file(p)


# --------------------------------------------------------------------------- #
# GeoJSON
# --------------------------------------------------------------------------- #
def _parse_geojson(doc: dict[str, Any]) -> list[tuple[dict, dict]]:
    """Devuelve lista de (geometry, properties)."""
    t = doc.get("type")
    if t == "FeatureCollection":
        feats = doc.get("features", [])
    elif t == "Feature":
        feats = [doc]
    elif t in ("Polygon", "MultiPolygon", "Point", "MultiPoint", "LineString",
               "MultiLineString", "GeometryCollection"):
        feats = [{"type": "Feature", "geometry": doc, "properties": {}}]
    else:
        raise ValueError(f"GeoJSON no reconocido (type={t})")
    out: list[tuple[dict, dict]] = []
    for f in feats:
        geom = f.get("geometry")
        if not geom:
            continue
        props = dict(f.get("properties") or {})
        if "id" in f and "id" not in props:
            props["id"] = f["id"]
        for g in _explode_collection(geom):
            out.append((g, props))
    return out


def _explode_collection(geom: dict[str, Any]) -> Iterable[dict[str, Any]]:
    if geom.get("type") == "GeometryCollection":
        for g in geom.get("geometries", []):
            yield from _explode_collection(g)
    else:
        yield _strip_z(geom)


def _strip_z(geom: dict[str, Any]) -> dict[str, Any]:
    """Elimina altitud (3ª coordenada) para trabajar en 2D."""

    def strip(c: Any) -> Any:
        if isinstance(c, (list, tuple)) and c and isinstance(c[0], (int, float)):
            return [float(c[0]), float(c[1])]
        return [strip(x) for x in c]

    return {"type": geom["type"], "coordinates": strip(geom["coordinates"])}


# --------------------------------------------------------------------------- #
# KML / KMZ
# --------------------------------------------------------------------------- #
def _local(tag: str) -> str:
    """Nombre del tag sin namespace: '{http://...}Placemark' -> 'Placemark'."""
    return tag.rsplit("}", 1)[-1]


def _children(el: ET.Element, name: str) -> list[ET.Element]:
    return [c for c in el if _local(c.tag) == name]


def _find(el: ET.Element, name: str) -> Optional[ET.Element]:
    for c in el.iter():
        if c is not el and _local(c.tag) == name:
            return c
    return None


def _parse_kml_coords(text: Optional[str]) -> list[list[float]]:
    coords: list[list[float]] = []
    for token in (text or "").split():
        parts = token.split(",")
        if len(parts) >= 2:
            coords.append([float(parts[0]), float(parts[1])])
    return coords


def _coords_text(el: ET.Element) -> str:
    # Ojo: un Element sin hijos es "falsy"; por eso se compara con None.
    c = _find(el, "coordinates")
    return c.text or "" if c is not None else ""


def _kml_ring(boundary: ET.Element) -> list[list[float]]:
    ring = _parse_kml_coords(_coords_text(boundary))
    if ring and ring[0] != ring[-1]:
        ring.append(ring[0])
    return ring


def _kml_geometries(el: ET.Element) -> list[dict[str, Any]]:
    """Convierte un elemento de geometría KML (o MultiGeometry) a GeoJSON."""
    name = _local(el.tag)
    if name == "Point":
        c = _parse_kml_coords(_coords_text(el))
        return [{"type": "Point", "coordinates": c[0]}] if c else []
    if name == "LineString":
        c = _parse_kml_coords(_coords_text(el))
        return [{"type": "LineString", "coordinates": c}] if len(c) >= 2 else []
    if name == "Polygon":
        rings: list[list[list[float]]] = []
        for ob in _children(el, "outerBoundaryIs"):
            rings.append(_kml_ring(ob))
        for ib in _children(el, "innerBoundaryIs"):
            rings.append(_kml_ring(ib))
        rings = [r for r in rings if len(r) >= 4]
        return [{"type": "Polygon", "coordinates": rings}] if rings else []
    if name == "MultiGeometry":
        out: list[dict[str, Any]] = []
        for child in el:
            out.extend(_kml_geometries(child))
        return out
    return []


def _kml_properties(pm: ET.Element) -> dict[str, Any]:
    props: dict[str, Any] = {}
    for key in ("name", "description"):
        el = _children(pm, key)
        if el and el[0].text:
            props[key] = el[0].text.strip()
    if pm.get("id"):
        props["id"] = pm.get("id")
    ext = _children(pm, "ExtendedData")
    if ext:
        for item in ext[0].iter():
            tag = _local(item.tag)
            if tag == "Data" and item.get("name"):
                val = _find(item, "value")
                props[item.get("name")] = (val.text or "").strip() if val is not None else ""
            elif tag == "SimpleData" and item.get("name"):
                props[item.get("name")] = (item.text or "").strip()
    return props


def _parse_kml(data: bytes) -> list[tuple[dict, dict]]:
    root = ET.fromstring(data)
    out: list[tuple[dict, dict]] = []

    def walk(el: ET.Element, folder: Optional[str]) -> None:
        for child in el:
            tag = _local(child.tag)
            if tag in ("Folder", "Document"):
                fname = _children(child, "name")
                walk(child, fname[0].text.strip() if fname and fname[0].text else folder)
            elif tag == "Placemark":
                props = _kml_properties(child)
                if folder and "layer" not in props:
                    props["_folder"] = folder
                for gtag in ("Point", "Polygon", "LineString", "MultiGeometry"):
                    for g_el in _children(child, gtag):
                        for g in _kml_geometries(g_el):
                            out.append((g, props))

    walk(root, None)
    return out


# --------------------------------------------------------------------------- #
# Clasificación y normalización
# --------------------------------------------------------------------------- #
def _first(props: dict[str, Any], keys: Iterable[str]) -> Optional[str]:
    lower = {k.lower(): v for k, v in props.items()}
    for k in keys:
        v = lower.get(k)
        if v not in (None, ""):
            return str(v)
    return None


def _explicit_kind(props: dict[str, Any]) -> Optional[str]:
    hint = (_first(props, LAYER_KEYS) or props.get("_folder") or "").strip().lower()
    if not hint:
        return None
    words = set(hint.replace("_", " ").replace("-", " ").split())
    if words & _SECTOR_WORDS:
        return SECTOR
    if words & _EQUIP_WORDS:
        return EQUIPMENT
    return None


def _classify(items: list[tuple[dict, dict]]) -> VectorLayers:
    layers = VectorLayers()
    counters = {SECTOR: 0, EQUIPMENT: 0, OTHER: 0}
    for geom, props in items:
        gtype = geom["type"]
        if gtype in ("Polygon", "MultiPolygon"):
            kind = SECTOR
        elif gtype in ("Point", "MultiPoint"):
            kind = EQUIPMENT
        else:
            kind = OTHER
        explicit = _explicit_kind(props)
        # Un punto no puede ser sector y un polígono no puede ser equipo puntual:
        # la pista explícita sólo se usa si es compatible con la geometría.
        if explicit == SECTOR and kind != SECTOR:
            explicit = None
        if explicit == EQUIPMENT and kind != EQUIPMENT:
            explicit = None
        kind = explicit or kind

        geoms = [geom]
        if gtype == "MultiPoint":  # cada punto es un equipo
            geoms = [{"type": "Point", "coordinates": c} for c in geom["coordinates"]]

        for g in geoms:
            counters[kind] += 1
            prefix = {SECTOR: "S", EQUIPMENT: "E", OTHER: "O"}[kind]
            fid = _first(props, ID_KEYS) or f"{prefix}{counters[kind]:03d}"
            if len(geoms) > 1:
                fid = f"{fid}-{counters[kind]}"
            name = _first(props, NAME_KEYS + KIND_NAME_KEYS[kind]) or fid
            feat = VectorFeature(id=fid, name=name, kind=kind, geometry=g, properties=props)
            {SECTOR: layers.sectors, EQUIPMENT: layers.equipment, OTHER: layers.other}[
                kind
            ].append(feat)
    _dedupe_ids(layers.sectors)
    _dedupe_ids(layers.equipment)
    return layers


def _dedupe_ids(feats: list[VectorFeature]) -> None:
    seen: dict[str, int] = {}
    for f in feats:
        if f.id in seen:
            seen[f.id] += 1
            f.id = f"{f.id}#{seen[f.id]}"
        else:
            seen[f.id] = 0


def _iter_coords(geom: dict[str, Any]) -> Iterable[tuple[float, float]]:
    def rec(c: Any) -> Iterable[tuple[float, float]]:
        if c and isinstance(c[0], (int, float)):
            yield float(c[0]), float(c[1])
        else:
            for x in c:
                yield from rec(x)

    yield from rec(geom["coordinates"])
