"""Caché offline de map tiles (Leaflet offline).

Componentes:

* :class:`MBTilesStore`  – almacenamiento SQLite en formato estándar MBTiles
  (un archivo por fuente de mapa). Al ser estándar, el archivo también puede
  generarse fuera de la app (QGIS, ``mb-util``) y copiarse al dispositivo.
* :class:`TileFetcher`   – descarga HTTP de un tile con reintentos y backoff.
* :class:`TileDownloader`– planifica y pre-descarga el área de trabajo
  (polígonos de sectores + buffer) en un rango de zooms, en paralelo,
  reanudable y cancelable.
* :class:`TileProvider`  – lo que consume el servidor local: devuelve un tile
  desde caché; si no existe intenta red ("cache on browse") y, sin red,
  genera un tile sobre-ampliado a partir de un ancestro cacheado (overzoom).

Nota legal: la descarga masiva de tiles está sujeta a los términos del
proveedor (ESRI / Mapbox). Verifica que tu licencia permita uso offline.
"""

from __future__ import annotations

import io
import logging
import random
import sqlite3
import threading
import time
import urllib.error
import urllib.request
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Iterable, Optional

from ..config import TileSource
from .tile_math import BBox, iter_tiles, xyz_to_tms_y

log = logging.getLogger(__name__)

USER_AGENT = "PuntoRiesgo/0.1 (+offline field risk app)"


# --------------------------------------------------------------------------- #
# Almacenamiento MBTiles
# --------------------------------------------------------------------------- #
class MBTilesStore:
    """Almacén de tiles en un archivo MBTiles (SQLite), seguro entre hilos."""

    def __init__(self, path: Path, source: Optional[TileSource] = None):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(str(self.path), check_same_thread=False)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=NORMAL")
        self._conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS metadata (name TEXT PRIMARY KEY, value TEXT);
            CREATE TABLE IF NOT EXISTS tiles (
                zoom_level  INTEGER NOT NULL,
                tile_column INTEGER NOT NULL,
                tile_row    INTEGER NOT NULL,
                tile_data   BLOB    NOT NULL,
                PRIMARY KEY (zoom_level, tile_column, tile_row)
            );
            """
        )
        if source is not None:
            self.set_metadata(
                {
                    "name": source.name,
                    "format": "jpg",
                    "type": "baselayer",
                    "attribution": source.attribution,
                    "scheme": "tms",
                }
            )
        self._conn.commit()

    # -- metadata ------------------------------------------------------ #
    def set_metadata(self, values: dict[str, str]) -> None:
        with self._lock:
            self._conn.executemany(
                "INSERT OR REPLACE INTO metadata(name, value) VALUES (?, ?)",
                [(k, str(v)) for k, v in values.items()],
            )
            self._conn.commit()

    def get_metadata(self) -> dict[str, str]:
        with self._lock:
            return dict(self._conn.execute("SELECT name, value FROM metadata").fetchall())

    # -- tiles --------------------------------------------------------- #
    def get(self, z: int, x: int, y: int) -> Optional[bytes]:
        with self._lock:
            row = self._conn.execute(
                "SELECT tile_data FROM tiles WHERE zoom_level=? AND tile_column=? AND tile_row=?",
                (z, x, xyz_to_tms_y(y, z)),
            ).fetchone()
        return row[0] if row else None

    def has(self, z: int, x: int, y: int) -> bool:
        with self._lock:
            return (
                self._conn.execute(
                    "SELECT 1 FROM tiles WHERE zoom_level=? AND tile_column=? AND tile_row=?",
                    (z, x, xyz_to_tms_y(y, z)),
                ).fetchone()
                is not None
            )

    def existing(self, z: int) -> set[tuple[int, int]]:
        """Conjunto de (x, y) XYZ ya cacheados en el zoom ``z`` (para reanudar)."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT tile_column, tile_row FROM tiles WHERE zoom_level=?", (z,)
            ).fetchall()
        n = 2**z - 1
        return {(x, n - row) for x, row in rows}

    def put(self, z: int, x: int, y: int, data: bytes) -> None:
        self.put_many([(z, x, y, data)])

    def put_many(self, items: Iterable[tuple[int, int, int, bytes]]) -> None:
        rows = [(z, x, xyz_to_tms_y(y, z), sqlite3.Binary(d)) for z, x, y, d in items]
        if not rows:
            return
        with self._lock:
            self._conn.executemany(
                "INSERT OR REPLACE INTO tiles(zoom_level, tile_column, tile_row, tile_data)"
                " VALUES (?, ?, ?, ?)",
                rows,
            )
            self._conn.commit()

    def stats(self) -> dict[str, int]:
        with self._lock:
            count, size = self._conn.execute(
                "SELECT COUNT(*), COALESCE(SUM(LENGTH(tile_data)), 0) FROM tiles"
            ).fetchone()
            zooms = self._conn.execute(
                "SELECT MIN(zoom_level), MAX(zoom_level) FROM tiles"
            ).fetchone()
        return {
            "count": count,
            "bytes": size,
            "min_zoom": zooms[0] if zooms[0] is not None else -1,
            "max_zoom": zooms[1] if zooms[1] is not None else -1,
        }

    def clear(self) -> None:
        with self._lock:
            self._conn.execute("DELETE FROM tiles")
            self._conn.commit()
            self._conn.execute("VACUUM")

    def close(self) -> None:
        with self._lock:
            self._conn.close()


# --------------------------------------------------------------------------- #
# Descarga HTTP
# --------------------------------------------------------------------------- #
class TileFetchError(Exception):
    pass


class TileFetcher:
    """Descarga tiles individuales con reintentos y backoff exponencial."""

    def __init__(
        self,
        source: TileSource,
        token: str = "",
        timeout_s: float = 10.0,
        retries: int = 3,
        opener: Optional[Callable[[urllib.request.Request, float], bytes]] = None,
    ):
        self.source = source
        self.token = token
        self.timeout_s = timeout_s
        self.retries = retries
        self._opener = opener or self._default_open

    @staticmethod
    def _default_open(req: urllib.request.Request, timeout: float) -> bytes:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            ctype = resp.headers.get("Content-Type", "")
            data = resp.read()
            if not ctype.startswith("image/"):
                raise TileFetchError(f"Respuesta no es imagen ({ctype})")
            return data

    def fetch(self, z: int, x: int, y: int) -> bytes:
        url = self.source.url(z, x, y, self.token)
        req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
        last_exc: Exception = TileFetchError("sin intentos")
        for attempt in range(self.retries):
            try:
                data = self._opener(req, self.timeout_s)
                if not data:
                    raise TileFetchError("tile vacío")
                return data
            except urllib.error.HTTPError as exc:
                last_exc = exc
                if exc.code in (400, 401, 403, 404):  # errores permanentes
                    break
            except (urllib.error.URLError, TimeoutError, OSError, TileFetchError) as exc:
                last_exc = exc
            if attempt < self.retries - 1:
                time.sleep(min(8.0, (2**attempt) * 0.5 + random.random() * 0.3))
        raise TileFetchError(f"{z}/{x}/{y}: {last_exc}") from last_exc


# --------------------------------------------------------------------------- #
# Pre-descarga del área de trabajo
# --------------------------------------------------------------------------- #
AVG_TILE_BYTES = 25_000  # promedio aproximado de un JPEG satelital 256 px


@dataclass
class DownloadPlan:
    source_id: str
    min_zoom: int
    max_zoom: int
    tiles: list[tuple[int, int, int]]
    already_cached: int = 0

    @property
    def total(self) -> int:
        return len(self.tiles) + self.already_cached

    @property
    def to_download(self) -> int:
        return len(self.tiles)

    @property
    def estimated_mb(self) -> float:
        return self.to_download * AVG_TILE_BYTES / 1_048_576


@dataclass
class DownloadProgress:
    total: int
    done: int = 0
    failed: int = 0
    bytes: int = 0
    started_at: float = field(default_factory=time.monotonic)
    finished: bool = False
    cancelled: bool = False
    aborted_reason: Optional[str] = None
    errors: list[str] = field(default_factory=list)

    @property
    def fraction(self) -> float:
        return 1.0 if self.total == 0 else (self.done + self.failed) / self.total

    @property
    def rate_tiles_s(self) -> float:
        elapsed = max(time.monotonic() - self.started_at, 1e-6)
        return (self.done + self.failed) / elapsed


class TileLimitExceeded(Exception):
    pass


class TileDownloader:
    """Pre-descarga los tiles que cubren las áreas de trabajo indicadas."""

    def __init__(self, store: MBTilesStore, fetcher: TileFetcher, workers: int = 6,
                 max_consecutive_failures: int = 30):
        self.store = store
        self.fetcher = fetcher
        self.workers = max(1, workers)
        # Si se pierde la señal a mitad de la descarga se aborta en vez de
        # seguir reintentando cada tile (la descarga es reanudable).
        self.max_consecutive_failures = max_consecutive_failures

    def plan(
        self,
        areas: list[BBox],
        min_zoom: int,
        max_zoom: int,
        max_tiles: int = 60_000,
    ) -> DownloadPlan:
        """Calcula los tiles necesarios.

        ``areas`` suele ser la lista de bbox (con buffer) de cada sector, de modo
        que en zooms altos sólo se descargan tiles que tocan sectores reales y no
        todo el rectángulo envolvente del predio.
        """
        if min_zoom > max_zoom:
            raise ValueError("min_zoom > max_zoom")
        if max_zoom > self.fetcher.source.max_native_zoom:
            max_zoom = self.fetcher.source.max_native_zoom
        pending: list[tuple[int, int, int]] = []
        cached = 0
        for z in range(min_zoom, max_zoom + 1):
            wanted: set[tuple[int, int]] = set()
            for area in areas:
                for _, x, y in iter_tiles(area, z):
                    wanted.add((x, y))
                    if len(wanted) + len(pending) + cached > max_tiles:
                        raise TileLimitExceeded(
                            f"El área requiere más de {max_tiles} tiles hasta zoom {z}. "
                            "Reduce el zoom máximo o el área."
                        )
            have = self.store.existing(z)
            cached += len(wanted & have)
            pending.extend((z, x, y) for x, y in sorted(wanted - have))
        return DownloadPlan(
            source_id=self.fetcher.source.id,
            min_zoom=min_zoom,
            max_zoom=max_zoom,
            tiles=pending,
            already_cached=cached,
        )

    def run(
        self,
        plan: DownloadPlan,
        on_progress: Optional[Callable[[DownloadProgress], None]] = None,
        cancel: Optional[threading.Event] = None,
        batch_size: int = 64,
    ) -> DownloadProgress:
        """Ejecuta la descarga (bloqueante: llamar desde un hilo de trabajo)."""
        cancel = cancel or threading.Event()
        progress = DownloadProgress(total=plan.total, done=plan.already_cached)
        buffer: list[tuple[int, int, int, bytes]] = []
        last_report = 0.0
        consecutive_failures = 0

        def report(force: bool = False) -> None:
            nonlocal last_report
            now = time.monotonic()
            if on_progress and (force or now - last_report > 0.25):
                last_report = now
                on_progress(progress)

        tiles = iter(plan.tiles)
        with ThreadPoolExecutor(max_workers=self.workers) as pool:
            in_flight = {}
            # Ventana deslizante: nunca más de workers*4 tareas encoladas.
            for t in tiles:
                in_flight[pool.submit(self.fetcher.fetch, *t)] = t
                if len(in_flight) >= self.workers * 4:
                    break
            while in_flight:
                done, _ = wait(in_flight, return_when=FIRST_COMPLETED)
                for fut in done:
                    z, x, y = in_flight.pop(fut)
                    try:
                        data = fut.result()
                        buffer.append((z, x, y, data))
                        progress.done += 1
                        progress.bytes += len(data)
                        consecutive_failures = 0
                    except Exception as exc:  # noqa: BLE001 - se reporta y continúa
                        progress.failed += 1
                        consecutive_failures += 1
                        if len(progress.errors) < 20:
                            progress.errors.append(str(exc))
                        if (consecutive_failures >= self.max_consecutive_failures
                                and not progress.aborted_reason):
                            progress.aborted_reason = (
                                f"{consecutive_failures} errores seguidos: sin conexión con "
                                f"el servidor de mapas ({exc})"
                            )
                            cancel.set()
                    if not cancel.is_set():
                        nxt = next(tiles, None)
                        if nxt is not None:
                            in_flight[pool.submit(self.fetcher.fetch, *nxt)] = nxt
                if len(buffer) >= batch_size:
                    self.store.put_many(buffer)
                    buffer.clear()
                report()
        self.store.put_many(buffer)
        progress.cancelled = cancel.is_set() and not progress.aborted_reason
        progress.finished = True
        report(force=True)
        return progress


# --------------------------------------------------------------------------- #
# Proveedor de tiles para el servidor local
# --------------------------------------------------------------------------- #
class TileProvider:
    """Resuelve tiles para Leaflet: caché -> red (y guarda) -> overzoom."""

    def __init__(
        self,
        store: MBTilesStore,
        fetcher: TileFetcher,
        is_online: Callable[[], bool] = lambda: True,
        max_overzoom_levels: int = 6,
    ):
        self.store = store
        self.fetcher = fetcher
        self.is_online = is_online
        self.max_overzoom_levels = max_overzoom_levels
        self._network_failures = 0
        self._network_backoff_until = 0.0

    def get_tile(self, z: int, x: int, y: int) -> tuple[Optional[bytes], str]:
        """Devuelve (bytes, origen). origen ∈ {"cache", "network", "overzoom", "miss"}."""
        data = self.store.get(z, x, y)
        if data:
            return data, "cache"
        if self._network_allowed():
            try:
                data = self.fetcher.fetch(z, x, y)
                self.store.put(z, x, y, data)
                self._network_failures = 0
                return data, "network"
            except Exception as exc:  # noqa: BLE001
                log.debug("Tile %s/%s/%s sin red: %s", z, x, y, exc)
                self._network_failures += 1
                if self._network_failures >= 3:
                    # Evita bloquear el mapa con timeouts sucesivos en zonas sin señal.
                    self._network_backoff_until = time.monotonic() + 30
        data = self._overzoom(z, x, y)
        if data:
            return data, "overzoom"
        return None, "miss"

    def _network_allowed(self) -> bool:
        return self.is_online() and time.monotonic() >= self._network_backoff_until

    def _overzoom(self, z: int, x: int, y: int) -> Optional[bytes]:
        """Recorta y escala el ancestro cacheado más cercano (requiere Pillow)."""
        try:
            from PIL import Image
        except ImportError:
            return None
        for dz in range(1, self.max_overzoom_levels + 1):
            pz = z - dz
            if pz < 0:
                break
            px, py = x >> dz, y >> dz
            parent = self.store.get(pz, px, py)
            if not parent:
                continue
            with Image.open(io.BytesIO(parent)) as img:
                img = img.convert("RGB")
                size = img.width
                factor = 2**dz
                sub = size / factor
                ox = (x - (px << dz)) * sub
                oy = (y - (py << dz)) * sub
                crop = img.crop((round(ox), round(oy), round(ox + sub), round(oy + sub)))
                crop = crop.resize((size, size), Image.Resampling.BILINEAR)
                out = io.BytesIO()
                crop.save(out, format="JPEG", quality=80)
                return out.getvalue()
        return None

