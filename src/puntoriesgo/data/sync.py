"""Background worker que vacía la Sync Queue hacia el servidor.

Flujo:
    1. La UI guarda la alerta + entrada en ``sync_queue`` (siempre local primero).
    2. :class:`SyncWorker` corre en un hilo daemon. Se despierta cada
       ``interval_s`` o inmediatamente cuando la UI llama :meth:`wake`
       (nueva alerta) o :meth:`set_online` (cambio de conectividad detectado
       por ``ft.Connectivity``).
    3. Antes de procesar verifica que el backend sea alcanzable (una red WiFi
       sin internet no cuenta como "online").
    4. Cada ítem se procesa de forma idempotente (UUID + upsert); si falla se
       reprograma con backoff exponencial.

Backend por defecto: Supabase (PostgREST + Storage). Para usar Firebase o una
API propia basta implementar :class:`SyncBackend`.

Tabla sugerida en Supabase (SQL)::

    create table public.alerts (
      id uuid primary key, created_at timestamptz not null,
      lat double precision not null, lon double precision not null,
      accuracy_m double precision, risk_type text not null, severity text not null,
      description text, photo_url text,
      sector_id text, sector_name text, sector_inside boolean, sector_distance_m double precision,
      equipment_id text, equipment_name text, equipment_distance_m double precision,
      device_id text, received_at timestamptz default now()
    );
"""

from __future__ import annotations

import json
import logging
import socket
import threading
import urllib.error
import urllib.parse
import urllib.request
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Callable, Optional

from ..models import Alert, SyncState
from .database import Database

log = logging.getLogger(__name__)


class SyncError(Exception):
    """Error recuperable: el ítem se reintentará."""


class PermanentSyncError(SyncError):
    """Error no recuperable (p. ej. 400 por payload inválido)."""


# --------------------------------------------------------------------------- #
# Backends
# --------------------------------------------------------------------------- #
class SyncBackend(ABC):
    @abstractmethod
    def is_reachable(self) -> bool: ...

    @abstractmethod
    def upload_photo(self, alert: Alert, path: Path) -> str:
        """Sube la foto y devuelve su URL/clave remota."""

    @abstractmethod
    def upsert_alert(self, alert: Alert) -> None: ...


class NullBackend(SyncBackend):
    """Sin backend configurado: los datos quedan en la cola local."""

    def is_reachable(self) -> bool:
        return False

    def upload_photo(self, alert: Alert, path: Path) -> str:  # pragma: no cover
        raise SyncError("Backend no configurado")

    def upsert_alert(self, alert: Alert) -> None:  # pragma: no cover
        raise SyncError("Backend no configurado")


class SupabaseBackend(SyncBackend):
    """Supabase vía REST puro (urllib): sin SDK, compatible con móvil."""

    def __init__(self, url: str, api_key: str, table: str = "alerts",
                 bucket: str = "alert-photos", timeout_s: float = 20.0):
        self.url = url.rstrip("/")
        self.api_key = api_key
        self.table = table
        self.bucket = bucket
        self.timeout_s = timeout_s

    def _headers(self, extra: Optional[dict[str, str]] = None) -> dict[str, str]:
        h = {"apikey": self.api_key, "Authorization": f"Bearer {self.api_key}"}
        h.update(extra or {})
        return h

    def _request(self, method: str, url: str, body: Optional[bytes], headers: dict[str, str]) -> bytes:
        req = urllib.request.Request(url, data=body, method=method, headers=self._headers(headers))
        try:
            with urllib.request.urlopen(req, timeout=self.timeout_s) as resp:
                return resp.read()
        except urllib.error.HTTPError as exc:
            detail = exc.read()[:300].decode("utf-8", "replace")
            if exc.code in (400, 404, 409, 422):
                raise PermanentSyncError(f"HTTP {exc.code}: {detail}") from exc
            raise SyncError(f"HTTP {exc.code}: {detail}") from exc
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise SyncError(str(exc)) from exc

    def is_reachable(self) -> bool:
        host = urllib.parse.urlparse(self.url).hostname
        if not host:
            return False
        try:
            with socket.create_connection((host, 443), timeout=4):
                return True
        except OSError:
            return False

    def upload_photo(self, alert: Alert, path: Path) -> str:
        key = f"{alert.created_at[:10]}/{alert.id}.jpg"
        url = f"{self.url}/storage/v1/object/{self.bucket}/{key}"
        self._request("POST", url, path.read_bytes(),
                      {"Content-Type": "image/jpeg", "x-upsert": "true"})
        return f"{self.url}/storage/v1/object/public/{self.bucket}/{key}"

    def upsert_alert(self, alert: Alert) -> None:
        url = f"{self.url}/rest/v1/{self.table}?on_conflict=id"
        body = json.dumps(alert.to_remote_payload()).encode("utf-8")
        self._request("POST", url, body, {
            "Content-Type": "application/json",
            "Prefer": "resolution=merge-duplicates,return=minimal",
        })


def build_backend(cfg) -> SyncBackend:
    if cfg.backend == "supabase" and cfg.supabase_url and cfg.supabase_key:
        return SupabaseBackend(cfg.supabase_url, cfg.supabase_key,
                               cfg.supabase_table, cfg.supabase_bucket)
    return NullBackend()


# --------------------------------------------------------------------------- #
# Worker
# --------------------------------------------------------------------------- #
class SyncWorker(threading.Thread):
    def __init__(
        self,
        db: Database,
        backend: SyncBackend,
        interval_s: float = 30.0,
        on_status: Optional[Callable[[dict], None]] = None,
    ):
        super().__init__(name="sync-worker", daemon=True)
        self.db = db
        self.backend = backend
        self.interval_s = interval_s
        self.on_status = on_status
        self._wake_event = threading.Event()
        self._stop_event = threading.Event()
        self._device_online = True  # pista del sistema operativo (ft.Connectivity)
        self.backend_reachable = False

    # -- API para la UI -------------------------------------------------- #
    def wake(self) -> None:
        self._wake_event.set()

    def set_online(self, online: bool) -> None:
        was = self._device_online
        self._device_online = online
        if online and not was:
            self.db.reset_backoff()  # volvió la red: reintentar todo ya
        self._wake_event.set()

    def stop(self) -> None:
        self._stop_event.set()
        self._wake_event.set()

    # -- loop ------------------------------------------------------------ #
    def run(self) -> None:
        while not self._stop_event.is_set():
            try:
                self.run_once()
            except Exception:  # noqa: BLE001 - el worker nunca debe morir
                log.exception("Error inesperado en sync worker")
            self._wake_event.wait(self.interval_s)
            self._wake_event.clear()

    def run_once(self) -> int:
        """Procesa los ítems vencidos. Devuelve cuántos se sincronizaron."""
        synced = 0
        if not self._device_online:
            self.backend_reachable = False
        else:
            self.backend_reachable = self.backend.is_reachable()
        if self.backend_reachable:
            while not self._stop_event.is_set():
                items = self.db.due_queue_items(limit=10)
                if not items:
                    break
                for item in items:
                    if self._process(item):
                        synced += 1
                    elif not self.backend.is_reachable():
                        self.backend_reachable = False
                        break
                if not self.backend_reachable:
                    break
        self._emit_status(synced)
        return synced

    def _process(self, item: dict) -> bool:
        try:
            if item["entity"] != "alert":
                raise PermanentSyncError(f"Entidad desconocida: {item['entity']}")
            alert = self.db.get_alert(item["entity_id"])
            if alert is None:  # borrada localmente
                self.db.complete_queue_item(item["id"])
                return True
            if alert.photo_path and not alert.remote_photo_url:
                p = Path(alert.photo_path)
                if p.exists():
                    alert.remote_photo_url = self.backend.upload_photo(alert, p)
                    # Persistir de inmediato: si falla el upsert no se re-sube la foto.
                    self.db.update_alert_sync(alert.id, SyncState.PENDING, alert.remote_photo_url)
            self.backend.upsert_alert(alert)
            self.db.update_alert_sync(alert.id, SyncState.SYNCED)
            self.db.complete_queue_item(item["id"])
            return True
        except PermanentSyncError as exc:
            log.error("Error permanente sincronizando %s: %s", item["entity_id"], exc)
            self.db.update_alert_sync(item["entity_id"], SyncState.ERROR)
            self.db.fail_queue_item(item["id"], str(exc), base_delay_s=3600)
        except SyncError as exc:
            log.warning("Reintento programado para %s: %s", item["entity_id"], exc)
            self.db.fail_queue_item(item["id"], str(exc))
        return False

    def _emit_status(self, synced: int = 0) -> None:
        if self.on_status:
            self.on_status({
                "synced": synced,
                "pending": self.db.pending_count(),
                "device_online": self._device_online,
                "backend_reachable": self.backend_reachable,
                "backend": type(self.backend).__name__,
            })
