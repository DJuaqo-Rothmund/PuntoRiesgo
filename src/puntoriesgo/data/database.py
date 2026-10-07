"""Base de datos local SQLite: alertas + Sync Queue (patrón Outbox).

Cada alerta se guarda **en la misma transacción** que su entrada en la cola de
sincronización. Así nunca existe una alerta local que el worker no vaya a
intentar subir, aunque la app se cierre justo después de guardar.
"""

from __future__ import annotations

import json
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any, Optional

from ..models import Alert, SyncState, utc_now_iso

SCHEMA_VERSION = 1

_MIGRATIONS: dict[int, str] = {
    1: """
    CREATE TABLE alerts (
        id                    TEXT PRIMARY KEY,
        created_at            TEXT NOT NULL,
        lat                   REAL NOT NULL,
        lon                   REAL NOT NULL,
        accuracy_m            REAL,
        risk_type             TEXT NOT NULL,
        severity              TEXT NOT NULL,
        description           TEXT NOT NULL DEFAULT '',
        photo_path            TEXT,
        remote_photo_url      TEXT,
        sector_id             TEXT,
        sector_name           TEXT,
        sector_inside         INTEGER NOT NULL DEFAULT 0,
        sector_distance_m     REAL,
        equipment_id          TEXT,
        equipment_name        TEXT,
        equipment_distance_m  REAL,
        device_id             TEXT,
        sync_state            TEXT NOT NULL DEFAULT 'pending'
    );
    CREATE INDEX idx_alerts_created ON alerts(created_at);

    CREATE TABLE sync_queue (
        id              INTEGER PRIMARY KEY AUTOINCREMENT,
        entity          TEXT NOT NULL,          -- 'alert'
        entity_id       TEXT NOT NULL,
        op              TEXT NOT NULL,          -- 'upsert' | 'delete'
        payload         TEXT,                   -- JSON opcional
        attempts        INTEGER NOT NULL DEFAULT 0,
        next_attempt_at REAL NOT NULL DEFAULT 0, -- epoch (time.time)
        last_error      TEXT,
        created_at      TEXT NOT NULL
    );
    CREATE INDEX idx_queue_next ON sync_queue(next_attempt_at);

    CREATE TABLE kv (key TEXT PRIMARY KEY, value TEXT);
    """,
}

_ALERT_COLUMNS = (
    "id", "created_at", "lat", "lon", "accuracy_m", "risk_type", "severity",
    "description", "photo_path", "remote_photo_url", "sector_id", "sector_name",
    "sector_inside", "sector_distance_m", "equipment_id", "equipment_name",
    "equipment_distance_m", "device_id", "sync_state",
)


class Database:
    """Acceso thread-safe a SQLite (UI, servidor local y worker comparten instancia)."""

    def __init__(self, path: Path | str):
        self.path = Path(path)
        if str(path) != ":memory:":
            self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(str(path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA foreign_keys=ON")
        self._migrate()

    def _migrate(self) -> None:
        with self._lock:
            current = self._conn.execute("PRAGMA user_version").fetchone()[0]
            for version in range(current + 1, SCHEMA_VERSION + 1):
                self._conn.executescript(_MIGRATIONS[version])
                self._conn.execute(f"PRAGMA user_version={version}")
            self._conn.commit()

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    # ------------------------------------------------------------------ #
    # Alertas
    # ------------------------------------------------------------------ #
    def create_alert(self, alert: Alert) -> Alert:
        """Inserta la alerta y la encola para sincronizar (atómico)."""
        row = alert.to_dict()
        row["sector_inside"] = int(alert.sector_inside)
        values = [row[c] for c in _ALERT_COLUMNS]
        placeholders = ",".join("?" for _ in _ALERT_COLUMNS)
        with self._lock, self._conn:
            self._conn.execute(
                f"INSERT INTO alerts ({','.join(_ALERT_COLUMNS)}) VALUES ({placeholders})",
                values,
            )
            self._enqueue("alert", alert.id, "upsert", None)
        return alert

    def get_alert(self, alert_id: str) -> Optional[Alert]:
        with self._lock:
            r = self._conn.execute("SELECT * FROM alerts WHERE id=?", (alert_id,)).fetchone()
        return Alert.from_dict(dict(r)) if r else None

    def list_alerts(self, limit: int = 2000) -> list[Alert]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM alerts ORDER BY created_at DESC LIMIT ?", (limit,)
            ).fetchall()
        return [Alert.from_dict(dict(r)) for r in rows]

    def alerts_geojson(self) -> dict[str, Any]:
        return {
            "type": "FeatureCollection",
            "features": [a.to_geojson_feature() for a in self.list_alerts()],
        }

    def update_alert_sync(
        self,
        alert_id: str,
        state: SyncState,
        remote_photo_url: Optional[str] = None,
    ) -> None:
        with self._lock, self._conn:
            if remote_photo_url is not None:
                self._conn.execute(
                    "UPDATE alerts SET sync_state=?, remote_photo_url=? WHERE id=?",
                    (state.value, remote_photo_url, alert_id),
                )
            else:
                self._conn.execute(
                    "UPDATE alerts SET sync_state=? WHERE id=?", (state.value, alert_id)
                )

    # ------------------------------------------------------------------ #
    # Sync Queue
    # ------------------------------------------------------------------ #
    def _enqueue(self, entity: str, entity_id: str, op: str, payload: Optional[dict]) -> None:
        self._conn.execute(
            "INSERT INTO sync_queue(entity, entity_id, op, payload, created_at)"
            " VALUES (?, ?, ?, ?, ?)",
            (entity, entity_id, op, json.dumps(payload) if payload else None, utc_now_iso()),
        )

    def enqueue(self, entity: str, entity_id: str, op: str, payload: Optional[dict] = None) -> None:
        with self._lock, self._conn:
            self._enqueue(entity, entity_id, op, payload)

    def due_queue_items(self, limit: int = 20, now: Optional[float] = None) -> list[dict[str, Any]]:
        now = time.time() if now is None else now
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM sync_queue WHERE next_attempt_at <= ? ORDER BY id LIMIT ?",
                (now, limit),
            ).fetchall()
        return [dict(r) for r in rows]

    def complete_queue_item(self, item_id: int) -> None:
        with self._lock, self._conn:
            self._conn.execute("DELETE FROM sync_queue WHERE id=?", (item_id,))

    def fail_queue_item(self, item_id: int, error: str, base_delay_s: float = 15.0,
                        max_delay_s: float = 3600.0) -> float:
        """Registra el fallo y programa el reintento con backoff exponencial."""
        with self._lock, self._conn:
            attempts = self._conn.execute(
                "SELECT attempts FROM sync_queue WHERE id=?", (item_id,)
            ).fetchone()
            n = (attempts[0] if attempts else 0) + 1
            delay = min(max_delay_s, base_delay_s * (2 ** (n - 1)))
            self._conn.execute(
                "UPDATE sync_queue SET attempts=?, next_attempt_at=?, last_error=? WHERE id=?",
                (n, time.time() + delay, error[:500], item_id),
            )
        return delay

    def reset_backoff(self) -> None:
        """Al recuperar conexión se reintenta todo de inmediato."""
        with self._lock, self._conn:
            self._conn.execute("UPDATE sync_queue SET next_attempt_at=0")

    def pending_count(self) -> int:
        with self._lock:
            return self._conn.execute("SELECT COUNT(*) FROM sync_queue").fetchone()[0]

    # ------------------------------------------------------------------ #
    # Key/Value (ajustes y metadatos del dispositivo)
    # ------------------------------------------------------------------ #
    def get_kv(self, key: str, default: Optional[str] = None) -> Optional[str]:
        with self._lock:
            r = self._conn.execute("SELECT value FROM kv WHERE key=?", (key,)).fetchone()
        return r[0] if r else default

    def set_kv(self, key: str, value: str) -> None:
        with self._lock, self._conn:
            self._conn.execute("INSERT OR REPLACE INTO kv(key, value) VALUES (?, ?)", (key, value))
