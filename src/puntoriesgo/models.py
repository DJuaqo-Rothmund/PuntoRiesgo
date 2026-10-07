"""Modelos de dominio: catálogo de riesgos, severidades y la entidad Alerta."""

from __future__ import annotations

import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Optional


class RiskType(str, Enum):
    """Tipos de problema de acceso / riesgo en terreno."""

    HOYO = "hoyo"
    ZANJA = "zanja"
    PIEDRA = "piedra"
    RAMA_CAIDA = "rama_caida"
    CAMINO_INUNDADO = "camino_inundado"
    BARRO = "barro"
    FUGA_RIEGO = "fuga_riego"
    CABLE_ELECTRICO = "cable_electrico"
    ANIMAL = "animal"
    OTRO = "otro"

    @property
    def label(self) -> str:
        return RISK_TYPE_META[self]["label"]

    @property
    def glyph(self) -> str:
        return RISK_TYPE_META[self]["glyph"]


RISK_TYPE_META: dict[RiskType, dict[str, str]] = {
    RiskType.HOYO: {"label": "Hoyo", "glyph": "◯"},
    RiskType.ZANJA: {"label": "Zanja", "glyph": "≋"},
    RiskType.PIEDRA: {"label": "Piedra grande", "glyph": "▲"},
    RiskType.RAMA_CAIDA: {"label": "Rama / árbol caído", "glyph": "🌿"},
    RiskType.CAMINO_INUNDADO: {"label": "Camino inundado", "glyph": "💧"},
    RiskType.BARRO: {"label": "Barro / camino intransitable", "glyph": "▒"},
    RiskType.FUGA_RIEGO: {"label": "Fuga en sistema de riego", "glyph": "⛲"},
    RiskType.CABLE_ELECTRICO: {"label": "Cable eléctrico expuesto", "glyph": "⚡"},
    RiskType.ANIMAL: {"label": "Animal peligroso", "glyph": "🐝"},
    RiskType.OTRO: {"label": "Otro", "glyph": "?"},
}


class Severity(str, Enum):
    """Nivel de severidad; define el color del pin en el mapa."""

    BAJA = "baja"
    MEDIA = "media"
    ALTA = "alta"
    CRITICA = "critica"

    @property
    def label(self) -> str:
        return SEVERITY_META[self]["label"]

    @property
    def color(self) -> str:
        return SEVERITY_META[self]["color"]

    @property
    def rank(self) -> int:
        return list(Severity).index(self)


SEVERITY_META: dict[Severity, dict[str, str]] = {
    Severity.BAJA: {"label": "Baja", "color": "#2e7d32"},
    Severity.MEDIA: {"label": "Media", "color": "#f9a825"},
    Severity.ALTA: {"label": "Alta", "color": "#ef6c00"},
    Severity.CRITICA: {"label": "Crítica", "color": "#c62828"},
}


class SyncState(str, Enum):
    PENDING = "pending"
    SYNCED = "synced"
    ERROR = "error"


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


@dataclass
class Alert:
    """Reporte de riesgo capturado en terreno."""

    lat: float
    lon: float
    risk_type: RiskType
    severity: Severity
    description: str = ""
    accuracy_m: Optional[float] = None
    photo_path: Optional[str] = None

    # Metadata del cruce espacial
    sector_id: Optional[str] = None
    sector_name: Optional[str] = None
    sector_inside: bool = False
    sector_distance_m: Optional[float] = None
    equipment_id: Optional[str] = None
    equipment_name: Optional[str] = None
    equipment_distance_m: Optional[float] = None

    id: str = field(default_factory=lambda: str(uuid.uuid4()))
    created_at: str = field(default_factory=utc_now_iso)
    device_id: Optional[str] = None
    sync_state: SyncState = SyncState.PENDING
    remote_photo_url: Optional[str] = None

    # ------------------------------------------------------------------ #
    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["risk_type"] = self.risk_type.value
        d["severity"] = self.severity.value
        d["sync_state"] = self.sync_state.value
        return d

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "Alert":
        d = dict(d)
        d["risk_type"] = RiskType(d["risk_type"])
        d["severity"] = Severity(d["severity"])
        d["sync_state"] = SyncState(d.get("sync_state") or SyncState.PENDING.value)
        d["sector_inside"] = bool(d.get("sector_inside"))
        allowed = {f for f in cls.__dataclass_fields__}
        return cls(**{k: v for k, v in d.items() if k in allowed})

    def to_remote_payload(self) -> dict[str, Any]:
        """Payload para el servidor (sin rutas locales del dispositivo)."""
        d = self.to_dict()
        d.pop("photo_path", None)
        d.pop("sync_state", None)
        d["photo_url"] = d.pop("remote_photo_url", None)
        return d

    def to_geojson_feature(self) -> dict[str, Any]:
        props = self.to_dict()
        props.pop("photo_path", None)
        props["has_photo"] = bool(self.photo_path)
        props["risk_label"] = self.risk_type.label
        props["risk_glyph"] = self.risk_type.glyph
        props["severity_label"] = self.severity.label
        props["color"] = self.severity.color
        return {
            "type": "Feature",
            "id": self.id,
            "geometry": {"type": "Point", "coordinates": [self.lon, self.lat]},
            "properties": props,
        }


def catalog_for_frontend() -> dict[str, Any]:
    """Catálogo serializable para el mapa (leyenda y estilos de pines)."""
    return {
        "risk_types": [
            {"id": r.value, "label": r.label, "glyph": r.glyph} for r in RiskType
        ],
        "severities": [
            {"id": s.value, "label": s.label, "color": s.color, "rank": s.rank}
            for s in Severity
        ],
    }
