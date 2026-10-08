"""Composición de servicios (sin dependencias de UI).

:class:`AppContext` arma y conecta todas las piezas: configuración, SQLite,
capas vectoriales, índice espacial, caché de tiles, servidor local y worker
de sincronización. La UI de Flet sólo consume esta fachada, lo que permite
probar todo el núcleo con pytest y sin dispositivo.
"""

from __future__ import annotations

import logging
import threading
import uuid
from pathlib import Path
from typing import Any, Callable, Optional

from .config import AppConfig
from .data.database import Database
from .data.photos import compress_photo
from .data.sync import SyncWorker, build_backend
from .geo.spatial import SpatialIndex, SpatialMatch
from .geo.tile_cache import MBTilesStore, TileDownloader, TileFetcher, TileProvider
from .geo.vector_layers import VectorLayers, load_vector_file
from .models import Alert, RiskType, Severity
from .server.local_server import LocalMapServer, MapState

log = logging.getLogger(__name__)

EventListener = Callable[[dict[str, Any]], None]


class AppContext:
    def __init__(self, cfg: Optional[AppConfig] = None):
        self.cfg = cfg or AppConfig.load()
        self.cfg.ensure_dirs()
        self.db = Database(self.cfg.db_path)
        self.device_id = self._device_id()
        self.state = MapState()
        self._online = True
        self._listeners: list[EventListener] = []
        self._layers_lock = threading.Lock()

        # Capas vectoriales + índice espacial
        self.layers = VectorLayers()
        self.index: Optional[SpatialIndex] = None
        self.layers_error: Optional[str] = None
        stored = self.db.get_kv("vector_layer_path")  # capa importada por el usuario
        if stored and Path(stored).exists():
            self.load_layers(Path(stored))
        else:
            self.load_layers(self.cfg.resolve_vector_layer())

        # Mapa base offline
        src = self.cfg.tile_source
        self.tile_store = MBTilesStore(self.cfg.tiles_dir / f"{src.id}.mbtiles", src)
        self.tile_fetcher = TileFetcher(src, token=self.cfg.mapbox_token)
        self.tile_provider = TileProvider(self.tile_store, self.tile_fetcher,
                                          is_online=lambda: self._online)

        # Servidor local para el WebView
        self.server = LocalMapServer(
            web_dir=self.cfg.web_dir,
            state=self.state,
            db=self.db,
            tile_provider=self.tile_provider,
            layers_getter=lambda: self.layers,
            map_config=self.map_config(),
            on_event=self._dispatch_event,
        )

        # Sync Queue -> backend
        self.sync_worker = SyncWorker(
            self.db,
            build_backend(self.cfg),
            interval_s=self.cfg.sync_interval_s,
            on_status=self._on_sync_status,
        )
        self._sync_listeners: list[Callable[[dict], None]] = []

    # ------------------------------------------------------------------ #
    def start(self) -> None:
        self.server.start()
        self.sync_worker.start()

    def stop(self) -> None:
        self.sync_worker.stop()
        self.server.stop()

    @property
    def map_url(self) -> str:
        return self.server.base_url

    # ------------------------------------------------------------------ #
    # Capas vectoriales
    # ------------------------------------------------------------------ #
    def load_layers(self, path: Path) -> None:
        try:
            layers = load_vector_file(path)
            index = SpatialIndex(layers, sector_tolerance_m=self.cfg.sector_tolerance_m)
        except Exception as exc:  # noqa: BLE001 - la app debe abrir igual
            log.error("No se pudo cargar la capa vectorial %s: %s", path, exc)
            self.layers_error = str(exc)
            return
        with self._layers_lock:
            self.layers, self.index, self.layers_error = layers, index, None
        log.info("Capa %s: %d sectores, %d equipos (motor %s)", path.name,
                 len(layers.sectors), len(layers.equipment), index.engine)
        if hasattr(self, "server"):
            self.server.map_config = self.map_config()
        self.state.bump_layers()

    def import_layer_file(self, filename: str, data: bytes) -> None:
        """Importa un GeoJSON/KML/KMZ elegido por el usuario y lo deja persistente."""
        dest = self.cfg.data_dir / Path(filename).name
        dest.write_bytes(data)
        self.load_layers(dest)
        if self.layers_error is None:
            self.db.set_kv("vector_layer_path", str(dest))

    def locate(self, lat: float, lon: float) -> SpatialMatch:
        if self.index is None:
            return SpatialMatch(lat=lat, lon=lon)
        return self.index.locate(lat, lon)

    def map_config(self) -> dict[str, Any]:
        src = self.cfg.tile_source
        ext = self.layers.extent()
        return {
            "attribution": src.attribution,
            "max_native_zoom": src.max_native_zoom,
            "max_zoom": 21,
            "embedded": True,  # dentro de la app el estado de sync va en el encabezado
            "initial_zoom": 16,
            "center": list(ext.center) if ext else None,
            "bounds": ext.as_leaflet() if ext else None,
        }

    # ------------------------------------------------------------------ #
    # Alertas
    # ------------------------------------------------------------------ #
    def create_alert(
        self,
        lat: float,
        lon: float,
        risk_type: RiskType,
        severity: Severity,
        description: str = "",
        accuracy_m: Optional[float] = None,
        photo_path: Optional[str] = None,
        photo_bytes: Optional[bytes] = None,
    ) -> Alert:
        """Cruce espacial + compresión de foto + guardado local + encolado.

        Bloqueante (I/O de disco): desde la UI llamar con ``asyncio.to_thread``.
        """
        match = self.locate(lat, lon)
        alert = Alert(
            lat=lat, lon=lon, risk_type=risk_type, severity=severity,
            description=description.strip(), accuracy_m=accuracy_m,
            sector_id=match.sector_id, sector_name=match.sector_name,
            sector_inside=match.sector_inside, sector_distance_m=match.sector_distance_m,
            equipment_id=match.equipment_id, equipment_name=match.equipment_name,
            equipment_distance_m=match.equipment_distance_m,
            device_id=self.device_id,
        )
        if photo_path or photo_bytes:
            dest = compress_photo(
                self.cfg.photos_dir, alert.id, src_path=photo_path, src_bytes=photo_bytes,
                max_side=self.cfg.photo_max_side_px, quality=self.cfg.photo_jpeg_quality,
            )
            alert.photo_path = str(dest)
        self.db.create_alert(alert)
        self.state.bump_alerts()
        self.sync_worker.wake()
        return alert

    # ------------------------------------------------------------------ #
    # Conectividad / sync
    # ------------------------------------------------------------------ #
    def set_online(self, online: bool) -> None:
        self._online = online
        self.sync_worker.set_online(online)

    @property
    def online(self) -> bool:
        return self._online

    def add_sync_listener(self, fn: Callable[[dict], None]) -> None:
        self._sync_listeners.append(fn)

    def _on_sync_status(self, status: dict) -> None:
        self.state.set_sync(status)
        if status.get("synced"):
            self.state.bump_alerts()  # refresca el indicador "pendiente" de los pines
        for fn in self._sync_listeners:
            fn(status)

    # ------------------------------------------------------------------ #
    # Offline tiles
    # ------------------------------------------------------------------ #
    def tile_downloader(self) -> TileDownloader:
        return TileDownloader(self.tile_store, self.tile_fetcher, workers=self.cfg.offline_workers)

    def work_areas(self):
        return self.layers.work_areas(self.cfg.offline_buffer_m)

    # ------------------------------------------------------------------ #
    # Eventos del mapa (JS -> Python)
    # ------------------------------------------------------------------ #
    def add_event_listener(self, fn: EventListener) -> None:
        self._listeners.append(fn)

    def _dispatch_event(self, evt: dict[str, Any]) -> None:
        for fn in self._listeners:
            try:
                fn(evt)
            except Exception:  # noqa: BLE001
                log.exception("Listener de evento falló")

    def _device_id(self) -> str:
        did = self.db.get_kv("device_id")
        if not did:
            did = str(uuid.uuid4())
            self.db.set_kv("device_id", did)
        return did
