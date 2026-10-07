#!/usr/bin/env python3
"""Convierte el dashboard HTML de sectorización (``const SECTORES_KML = [...]``)
a GeoJSON para PuntoRiesgo.

El dashboard guarda cada sector como un objeto JS con ``coordenadas``: una
lista de anillos ``[[lat, lng], ...]`` (uno por trozo físicamente separado).
El arreglo se lee con un tokenizador propio, sin ejecutar el JavaScript.

Uso:
    python scripts/import_dashboard.py dashboard.html src/assets/data/el_amanecer.geojson
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

START_RE = re.compile(r"const\s+SECTORES_KML\s*=\s*\[")


def extract_array(html: str) -> str:
    """Devuelve el texto del arreglo JS ``[...]`` (balanceando corchetes)."""
    m = START_RE.search(html)
    if not m:
        raise ValueError("No se encontró 'const SECTORES_KML = [' en el archivo")
    i = m.end() - 1
    depth, quote = 0, None
    for j in range(i, len(html)):
        c = html[j]
        if quote:
            if c == "\\":
                continue
            if c == quote and html[j - 1] != "\\":
                quote = None
        elif c in "'\"":
            quote = c
        elif c == "[":
            depth += 1
        elif c == "]":
            depth -= 1
            if depth == 0:
                return html[i : j + 1]
    raise ValueError("Arreglo SECTORES_KML sin cerrar")


def js_literal_to_json(src: str) -> str:
    """Convierte un literal JS (claves sin comillas, strings con comilla simple,
    comas finales) a JSON válido."""
    out: list[str] = []
    i, n = 0, len(src)
    while i < n:
        c = src[i]
        if c in "'\"":  # string -> string JSON
            j, buf = i + 1, []
            while src[j] != c:
                if src[j] == "\\":
                    buf.append(src[j + 1])
                    j += 2
                    continue
                buf.append(src[j])
                j += 1
            out.append(json.dumps("".join(buf), ensure_ascii=False))
            i = j + 1
        elif c.isalpha() or c == "_":  # identificador: clave o literal
            m = re.match(r"[A-Za-z_]\w*", src[i:])
            word = m.group(0)
            rest = src[i + len(word):].lstrip()
            if rest.startswith(":"):
                out.append(json.dumps(word))
            elif word in ("null", "true", "false"):
                out.append(word)
            else:
                raise ValueError(f"Identificador inesperado en los datos: {word}")
            i += len(word)
        elif c == ",":  # quitar comas finales
            k = i + 1
            while k < n and src[k].isspace():
                k += 1
            if k < n and src[k] in "]}":
                i += 1
                continue
            out.append(c)
            i += 1
        elif c == "/" and src[i:i + 2] == "//":  # comentario de línea
            i = src.index("\n", i)
        else:
            out.append(c)
            i += 1
    return "".join(out)


def to_feature(s: dict) -> dict:
    polys = []
    for ring in s["coordenadas"]:
        coords = [[float(lng), float(lat)] for lat, lng in ring]
        if coords[0] != coords[-1]:
            coords.append(coords[0])
        if len(coords) >= 4:
            polys.append([coords])
    eq = s.get("equipoRiego") or f"Equipo {s.get('equipoId')}"
    props = {
        "id": s["id"],
        "nombre": f"{eq} · {s['nombre']}",
        "layer": "sector",
        "sector": s["nombre"],
        "equipo_riego": eq,
        "equipo_id": s.get("equipoId"),
        "hectareas": s.get("hectareas"),
        "hileras": sum(s.get("hilerasPorPoligono") or []) or None,
        "etiquetas_plano": ", ".join(s.get("etiquetasPlano") or []),
        "variedades": ", ".join(v for v in (s.get("variedades") or []) if v),
        "color": s.get("color"),
    }
    props = {k: v for k, v in props.items() if v not in (None, "")}
    geom = ({"type": "Polygon", "coordinates": polys[0]} if len(polys) == 1
            else {"type": "MultiPolygon", "coordinates": polys})
    return {"type": "Feature", "properties": props, "geometry": geom}


def convert(html_path: Path) -> dict:
    data = json.loads(js_literal_to_json(extract_array(html_path.read_text(encoding="utf-8"))))
    title = re.search(r"<title>(.*?)</title>", html_path.read_text(encoding="utf-8"), re.S)
    return {
        "type": "FeatureCollection",
        "name": title.group(1).strip() if title else html_path.stem,
        "features": [to_feature(s) for s in data],
    }


def main() -> int:
    if len(sys.argv) != 3:
        print(__doc__)
        return 2
    fc = convert(Path(sys.argv[1]))
    out = Path(sys.argv[2])
    out.write_text(json.dumps(fc, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    ha = sum(f["properties"].get("hectareas", 0) for f in fc["features"])
    print(f"{len(fc['features'])} sectores ({ha:.1f} ha) -> {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
