#!/usr/bin/env python3
"""Pre-descarga los tiles satelitales del área de trabajo a un archivo MBTiles.

Útil para preparar el mapa offline en un PC con buena conexión y luego copiar
el archivo al dispositivo (``<datos de la app>/tiles/<fuente>.mbtiles``), o
para empaquetarlo con la app.

Ejemplos:
    python scripts/precache_tiles.py --layer src/assets/data/sectores_riego.geojson
    python scripts/precache_tiles.py --layer fundo.kml --min-zoom 13 --max-zoom 19 \\
        --buffer 300 --out tiles/esri_world_imagery.mbtiles
    python scripts/precache_tiles.py --layer fundo.kml --source mapbox_satellite --token pk.xxx
"""

from __future__ import annotations

import argparse
import sys
import threading
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from puntoriesgo.config import TILE_SOURCES  # noqa: E402
from puntoriesgo.geo.tile_cache import (  # noqa: E402
    DownloadProgress,
    MBTilesStore,
    TileDownloader,
    TileFetcher,
    TileLimitExceeded,
)
from puntoriesgo.geo.vector_layers import load_vector_file  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--layer", required=True, help="GeoJSON/KML/KMZ con los sectores")
    ap.add_argument("--source", default="esri_world_imagery", choices=sorted(TILE_SOURCES))
    ap.add_argument("--token", default="", help="Token del proveedor (Mapbox)")
    ap.add_argument("--min-zoom", type=int, default=12)
    ap.add_argument("--max-zoom", type=int, default=18)
    ap.add_argument("--buffer", type=float, default=250.0, help="Margen en metros alrededor de cada sector")
    ap.add_argument("--max-tiles", type=int, default=200_000)
    ap.add_argument("--workers", type=int, default=6)
    ap.add_argument("--out", default=None, help="Archivo .mbtiles de salida")
    ap.add_argument("--dry-run", action="store_true", help="Sólo calcular, no descargar")
    args = ap.parse_args()

    source = TILE_SOURCES[args.source]
    if source.requires_token and not args.token:
        ap.error(f"La fuente {args.source} requiere --token")
    out = Path(args.out or f"{source.id}.mbtiles")

    layers = load_vector_file(args.layer)
    areas = layers.work_areas(args.buffer)
    print(f"Capa: {len(layers.sectors)} sectores, {len(layers.equipment)} equipos -> {len(areas)} áreas")

    store = MBTilesStore(out, source)
    downloader = TileDownloader(store, TileFetcher(source, token=args.token), workers=args.workers)
    try:
        plan = downloader.plan(areas, args.min_zoom, args.max_zoom, args.max_tiles)
    except TileLimitExceeded as exc:
        print(f"ERROR: {exc}")
        return 2
    print(f"Zoom {plan.min_zoom}-{plan.max_zoom}: {plan.total} tiles, {plan.already_cached} ya en caché, "
          f"{plan.to_download} por descargar (~{plan.estimated_mb:.0f} MB)")
    if args.dry_run or plan.to_download == 0:
        return 0

    def show(p: DownloadProgress) -> None:
        bar = "#" * int(p.fraction * 30)
        print(f"\r[{bar:<30}] {p.fraction * 100:5.1f}%  {p.done + p.failed}/{p.total}  "
              f"{p.bytes / 1_048_576:.1f} MB  {p.rate_tiles_s:.0f} t/s  err={p.failed}",
              end="", flush=True)

    cancel = threading.Event()
    try:
        result = downloader.run(plan, on_progress=show, cancel=cancel)
    except KeyboardInterrupt:
        cancel.set()
        print("\nCancelado (re-ejecuta para reanudar).")
        return 130
    print()
    if result.aborted_reason:
        print(f"Detenido: {result.aborted_reason}")
    for err in result.errors[:5]:
        print("  error:", err)
    st = store.stats()
    print(f"Listo: {out} contiene {st['count']} tiles ({st['bytes'] / 1_048_576:.1f} MB), "
          f"zoom {st['min_zoom']}-{st['max_zoom']}")
    return 0 if not result.failed else 1


if __name__ == "__main__":
    sys.exit(main())
