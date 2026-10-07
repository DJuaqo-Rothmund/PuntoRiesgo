"""Servidor HTTP embebido que conecta Leaflet (JS) con Python.

¿Por qué un servidor local y no ``file://``?

* Leaflet pide tiles por URL; el servidor los resuelve desde el MBTiles local
  (offline), desde la red (y los cachea) o por overzoom.
* El WebView no tiene un puente JS→Python nativo en todas las plataformas.
  Con ``fetch()`` contra 127.0.0.1 el mapa lee capas/alertas/estado y envía
  eventos (p. ej. "mantener presionado para marcar"), y funciona igual en
  Android, iOS, macOS y en modo web de escritorio para desarrollo.

Seguridad: escucha sólo en 127.0.0.1 y todas las rutas cuelgan de un token
aleatorio por sesión (``/<token>/...``), de modo que otras apps del
dispositivo no pueden leer los datos aunque descubran el puerto.

Rutas (relativas a ``/<token>/``):
    GET  ""  | index.html          -> mapa
    GET  static/<ruta>             -> assets/web/*
    GET  tiles/<z>/<x>/<y>         -> tile del mapa base
    GET  api/config                -> configuración del mapa y catálogo de riesgos
    GET  api/layers                -> GeoJSON de sectores y equipos
    GET  api/alerts                -> GeoJSON de alertas
    GET  api/state                 -> estado (posición GPS, versión, sync)
    GET  photos/<alert_id>.jpg     -> foto comprimida
    POST api/events                -> eventos del mapa hacia Python
"""

from __future__ import annotations

import json
import logging
import mimetypes
import re
import secrets
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable, Optional
from urllib.parse import unquote, urlparse

from ..data.database import Database
from ..geo.tile_cache import TileProvider
from ..geo.vector_layers import VectorLayers
from ..models import catalog_for_frontend

log = logging.getLogger(__name__)

# PNG 1x1 transparente para tiles inexistentes (evita íconos de imagen rota).
_EMPTY_PNG = bytes.fromhex(
    "89504e470d0a1a0a0000000d4948445200000001000000010806000000"
    "1f15c4890000000d49444154789c6300010000000500010d0a2db40000"
    "000049454e44ae426082"
)
_TILE_RE = re.compile(r"^tiles/(\d{1,2})/(\d+)/(\d+)(?:\.\w+)?$")
_PHOTO_RE = re.compile(r"^photos/([0-9a-fA-F-]{36})\.jpg$")


class MapState:
    """Estado compartido entre la UI (Flet) y el mapa (JS), thread-safe.

    ``version`` se incrementa con cada cambio para que el JS sólo vuelva a
    pedir alertas/capas cuando algo cambió.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.version = 0
        self.alerts_version = 0
        self.layers_version = 0
        self.position: Optional[dict[str, float]] = None
        self.sync: dict[str, Any] = {"pending": 0, "backend_reachable": False}
        self.commands: list[dict[str, Any]] = []  # órdenes Python -> JS (p. ej. centrar)
        self._command_seq = 0

    def set_position(self, lat: float, lon: float, accuracy: Optional[float],
                     heading: Optional[float] = None) -> None:
        with self._lock:
            self.position = {"lat": lat, "lon": lon, "accuracy": accuracy or 0.0,
                             "heading": heading, "ts": time.time()}
            self.version += 1

    def bump_alerts(self) -> None:
        with self._lock:
            self.alerts_version += 1
            self.version += 1

    def bump_layers(self) -> None:
        with self._lock:
            self.layers_version += 1
            self.version += 1

    def set_sync(self, status: dict[str, Any]) -> None:
        with self._lock:
            self.sync = dict(status)
            self.version += 1

    def push_command(self, name: str, **args: Any) -> None:
        with self._lock:
            self._command_seq += 1
            self.commands.append({"seq": self._command_seq, "name": name, "args": args})
            self.commands = self.commands[-20:]
            self.version += 1

    def snapshot(self, after_cmd: int = 0) -> dict[str, Any]:
        with self._lock:
            return {
                "version": self.version,
                "alerts_version": self.alerts_version,
                "layers_version": self.layers_version,
                "position": self.position,
                "sync": self.sync,
                "commands": [c for c in self.commands if c["seq"] > after_cmd],
            }


class LocalMapServer:
    def __init__(
        self,
        web_dir: Path,
        state: MapState,
        db: Database,
        tile_provider: TileProvider,
        layers_getter: Callable[[], VectorLayers],
        map_config: dict[str, Any],
        on_event: Callable[[dict[str, Any]], None],
        host: str = "127.0.0.1",
        port: int = 0,
    ):
        self.web_dir = web_dir.resolve()
        self.state = state
        self.db = db
        self.tiles = tile_provider
        self.layers_getter = layers_getter
        self.map_config = map_config
        self.on_event = on_event
        self.token = secrets.token_urlsafe(16)
        self._httpd = ThreadingHTTPServer((host, port), self._make_handler())
        self._httpd.daemon_threads = True
        self._thread: Optional[threading.Thread] = None

    @property
    def port(self) -> int:
        return self._httpd.server_address[1]

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self.port}/{self.token}/"

    def start(self) -> "LocalMapServer":
        self._thread = threading.Thread(target=self._httpd.serve_forever,
                                        name="map-server", daemon=True)
        self._thread.start()
        log.info("Servidor de mapa en %s", self.base_url)
        return self

    def stop(self) -> None:
        self._httpd.shutdown()
        self._httpd.server_close()

    # ------------------------------------------------------------------ #
    def _make_handler(self):
        server = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, fmt: str, *args: Any) -> None:  # silenciar stdout
                log.debug("%s - " + fmt, self.address_string(), *args)

            # -- helpers ------------------------------------------------- #
            def _route(self) -> Optional[str]:
                path = unquote(urlparse(self.path).path)
                prefix = f"/{server.token}/"
                if path == f"/{server.token}":
                    return ""
                if not path.startswith(prefix):
                    return None
                return path[len(prefix):]

            def _send(self, status: int, body: bytes, ctype: str,
                      cache: str = "no-store") -> None:
                self.send_response(status)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Cache-Control", cache)
                # Flet web sirve la app con COEP "require-corp": sin estas cabeceras
                # el navegador bloquea el iframe del mapa (inocuas en Android/iOS).
                self.send_header("Cross-Origin-Resource-Policy", "cross-origin")
                self.send_header("Cross-Origin-Embedder-Policy", "require-corp")
                self.end_headers()
                if self.command != "HEAD":
                    self.wfile.write(body)

            def _json(self, obj: Any, status: int = 200) -> None:
                self._send(status, json.dumps(obj, ensure_ascii=False).encode("utf-8"),
                           "application/json; charset=utf-8")

            def _static(self, rel: str) -> None:
                target = (server.web_dir / rel).resolve()
                # Protección contra path traversal (../../)
                if server.web_dir not in target.parents and target != server.web_dir:
                    return self._send(403, b"forbidden", "text/plain")
                if not target.is_file():
                    return self._send(404, b"not found", "text/plain")
                ctype = mimetypes.guess_type(target.name)[0] or "application/octet-stream"
                if ctype.startswith("text/") or ctype.endswith("javascript"):
                    ctype += "; charset=utf-8"
                self._send(200, target.read_bytes(), ctype, cache="no-cache")

            # -- verbs --------------------------------------------------- #
            def do_HEAD(self) -> None:  # noqa: N802
                self.do_GET()

            def do_GET(self) -> None:  # noqa: N802
                route = self._route()
                if route is None:
                    return self._send(404, b"not found", "text/plain")
                try:
                    if route in ("", "index.html"):
                        return self._static("map.html")
                    if route.startswith("static/"):
                        return self._static(route[len("static/"):])
                    if m := _TILE_RE.match(route):
                        z, x, y = (int(v) for v in m.groups())
                        data, origin = server.tiles.get_tile(z, x, y)
                        if data is None:
                            return self._send(200, _EMPTY_PNG, "image/png", cache="no-store")
                        ctype = "image/png" if data[:4] == b"\x89PNG" else "image/jpeg"
                        cache = "max-age=86400" if origin in ("cache", "network") else "no-store"
                        return self._send(200, data, ctype, cache=cache)
                    if route == "api/config":
                        return self._json(server.map_config | {"catalog": catalog_for_frontend()})
                    if route == "api/layers":
                        return self._json(server.layers_getter().to_geojson())
                    if route == "api/alerts":
                        return self._json(server.db.alerts_geojson())
                    if route == "api/state":
                        q = urlparse(self.path).query
                        after = 0
                        for part in q.split("&"):
                            if part.startswith("cmd="):
                                after = int(part[4:] or 0)
                        return self._json(server.state.snapshot(after))
                    if m := _PHOTO_RE.match(route):
                        alert = server.db.get_alert(m.group(1))
                        if alert and alert.photo_path and Path(alert.photo_path).is_file():
                            return self._send(200, Path(alert.photo_path).read_bytes(),
                                              "image/jpeg", cache="max-age=3600")
                        return self._send(404, b"not found", "text/plain")
                    return self._send(404, b"not found", "text/plain")
                except (BrokenPipeError, ConnectionResetError):
                    pass
                except Exception as exc:  # noqa: BLE001
                    log.exception("Error en GET %s", route)
                    self._json({"error": str(exc)}, status=500)

            def do_POST(self) -> None:  # noqa: N802
                route = self._route()
                if route != "api/events":
                    return self._send(404, b"not found", "text/plain")
                length = int(self.headers.get("Content-Length") or 0)
                if length > 64_000:
                    return self._send(413, b"too large", "text/plain")
                try:
                    evt = json.loads(self.rfile.read(length) or b"{}")
                    if not isinstance(evt, dict) or "type" not in evt:
                        return self._json({"error": "evento inválido"}, status=400)
                    server.on_event(evt)
                    self._json({"ok": True})
                except json.JSONDecodeError:
                    self._json({"error": "JSON inválido"}, status=400)

        return Handler

