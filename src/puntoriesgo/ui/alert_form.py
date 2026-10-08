"""Formulario "Registrar Alerta".

El sector y el equipo de riego más cercano se autocompletan por cruce
espacial y se muestran como sólo-lectura. La foto se comprime localmente al
guardar (en un hilo, para no congelar la UI).
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import Awaitable, Callable, Optional

import flet as ft

from ..app_context import AppContext
from ..models import Alert, RiskType, Severity
from .camera_capture import CameraCapture, camera_supported

log = logging.getLogger(__name__)

# Íconos Material para el selector (no dependen de fuentes emoji del sistema).
RISK_ICONS = {
    RiskType.HOYO: ft.Icons.RADIO_BUTTON_UNCHECKED,
    RiskType.ZANJA: ft.Icons.WAVES,
    RiskType.PIEDRA: ft.Icons.LANDSCAPE,
    RiskType.RAMA_CAIDA: ft.Icons.PARK,
    RiskType.CAMINO_INUNDADO: ft.Icons.FLOOD,
    RiskType.BARRO: ft.Icons.TERRAIN,
    RiskType.FUGA_RIEGO: ft.Icons.WATER_DROP,
    RiskType.CABLE_ELECTRICO: ft.Icons.ELECTRIC_BOLT,
    RiskType.ANIMAL: ft.Icons.PETS,
    RiskType.OTRO: ft.Icons.HELP_OUTLINE,
}


@dataclass
class CapturePoint:
    lat: float
    lon: float
    accuracy_m: Optional[float]
    source: str  # "gps" | "mapa"


class AlertForm:
    def __init__(
        self,
        page: ft.Page,
        ctx: AppContext,
        point: CapturePoint,
        on_saved: Callable[[Alert], Awaitable[None]],
    ):
        self.page = page
        self.ctx = ctx
        self.point = point
        self.on_saved = on_saved
        self.photo_path: Optional[str] = None
        self.photo_bytes: Optional[bytes] = None
        self.file_picker = ft.FilePicker()  # servicio: se registra al construirse

        match = ctx.locate(point.lat, point.lon)

        self.risk_dd = ft.Dropdown(
            label="Tipo de problema *",
            options=[
                ft.DropdownOption(key=r.value, text=r.label, leading_icon=RISK_ICONS[r])
                for r in RiskType
            ],
            expand=True,
            enable_search=False,
        )
        self.severity = ft.SegmentedButton(
            selected=[Severity.MEDIA.value],
            allow_empty_selection=False,
            allow_multiple_selection=False,
            show_selected_icon=False,
            segments=[
                ft.Segment(
                    value=s.value,
                    label=ft.Text(s.label, size=12, color=s.color, weight=ft.FontWeight.BOLD),
                )
                for s in Severity
            ],
        )
        self.description = ft.TextField(
            label="Observaciones", multiline=True, min_lines=2, max_lines=4,
            hint_text="Ej.: zanja de 1 m en el camino principal, no pasa camioneta",
        )
        acc = f" · ±{point.accuracy_m:.0f} m" if point.accuracy_m else ""
        warn = point.accuracy_m is not None and point.accuracy_m > 30
        self.location_txt = ft.Text(
            f"{'GPS' if point.source == 'gps' else 'Punto en mapa'}: "
            f"{point.lat:.6f}, {point.lon:.6f}{acc}"
            + ("  ⚠ precisión baja" if warn else ""),
            size=12,
            color=ft.Colors.ORANGE_700 if warn else ft.Colors.ON_SURFACE_VARIANT,
        )
        self.sector_tf = ft.TextField(
            label="Sector (automático)", value=match.sector_label, read_only=True,
            prefix_icon=ft.Icons.GRID_ON, dense=True,
        )
        self.equipment_tf = ft.TextField(
            label="Equipo de riego más cercano", value=match.equipment_label, read_only=True,
            prefix_icon=ft.Icons.WATER_DROP, dense=True, multiline=True, min_lines=1, max_lines=3,
        )
        self.photo_preview = ft.Image(src=b"", height=140, fit=ft.BoxFit.COVER,
                                      border_radius=8, visible=False)
        self.camera_btn = ft.FilledTonalButton(
            content="Tomar foto", icon=ft.Icons.PHOTO_CAMERA, on_click=self._take_photo,
            visible=camera_supported(page),
        )
        self.photo_btn = ft.OutlinedButton(
            content="Galería", icon=ft.Icons.PHOTO_LIBRARY, on_click=self._pick_photo,
        )
        self.error_txt = ft.Text("", color=ft.Colors.ERROR, size=12, visible=False)
        self.save_btn = ft.FilledButton(content="Guardar alerta", icon=ft.Icons.SAVE,
                                        on_click=self._save)
        self.progress = ft.ProgressRing(width=18, height=18, visible=False)

        self.dialog = ft.AlertDialog(
            modal=True,
            title=ft.Text("Registrar alerta"),
            scrollable=True,
            content=ft.Column(
                tight=True,
                spacing=12,
                width=420,
                controls=[
                    self.location_txt,
                    self.sector_tf,
                    self.equipment_tf,
                    ft.Row([self.risk_dd]),
                    ft.Text("Severidad", size=12, weight=ft.FontWeight.W_500),
                    self.severity,
                    self.description,
                    ft.Row([self.camera_btn, self.photo_btn], wrap=True),
                    self.photo_preview,
                    self.error_txt,
                ],
            ),
            actions=[
                self.progress,
                ft.TextButton(content="Cancelar", on_click=self._cancel),
                self.save_btn,
            ],
        )

    # ------------------------------------------------------------------ #
    def open(self) -> None:
        self.page.show_dialog(self.dialog)

    def _cancel(self, _e=None) -> None:
        self.page.pop_dialog()

    async def _pick_photo(self, _e=None) -> None:
        files = await self.file_picker.pick_files(
            dialog_title="Foto del riesgo",
            file_type=ft.FilePickerFileType.IMAGE,
            allow_multiple=False,
            with_data=bool(self.page.web),  # en web no hay ruta local
        )
        if not files:
            return
        f = files[0]
        await self._set_photo(f.path, f.bytes)

    async def _take_photo(self, _e=None) -> None:
        # La cámara ocupa toda la pantalla: se cierra el formulario y se
        # vuelve a abrir (con todo lo ya ingresado) al terminar.
        self.page.pop_dialog()
        await CameraCapture(self.page, on_done=self._camera_done).open()

    async def _camera_done(self, data: Optional[bytes], error: Optional[str]) -> None:
        self.page.show_dialog(self.dialog)
        if error:
            self.error_txt.value = error
            self.error_txt.visible = True
            self.page.update()
        elif data:
            self.error_txt.visible = False
            await self._set_photo(None, data)

    async def _set_photo(self, path: Optional[str], data: Optional[bytes]) -> None:
        self.photo_path, self.photo_bytes = path, data
        try:
            self.photo_preview.src = await asyncio.to_thread(self._thumbnail)
            self.photo_preview.visible = True
        except Exception as exc:  # noqa: BLE001
            log.warning("No se pudo previsualizar: %s", exc)
            self.photo_preview.visible = False
        self.camera_btn.content = "Otra foto"
        self.page.update()

    def _thumbnail(self) -> bytes:
        import io

        try:
            from PIL import Image, ImageOps
        except ImportError:
            return self.photo_bytes or open(self.photo_path, "rb").read()
        src = io.BytesIO(self.photo_bytes) if self.photo_bytes else self.photo_path
        with Image.open(src) as img:
            img = ImageOps.exif_transpose(img)
            img.thumbnail((480, 480))
            out = io.BytesIO()
            img.convert("RGB").save(out, format="JPEG", quality=65)
            return out.getvalue()

    async def _save(self, _e=None) -> None:
        if not self.risk_dd.value:
            self.error_txt.value = "Selecciona el tipo de problema."
            self.error_txt.visible = True
            self.page.update()
            return
        self.error_txt.visible = False
        self.save_btn.disabled = True
        self.progress.visible = True
        self.page.update()
        try:
            alert = await asyncio.to_thread(
                self.ctx.create_alert,
                lat=self.point.lat,
                lon=self.point.lon,
                risk_type=RiskType(self.risk_dd.value),
                severity=Severity(self.severity.selected[0]),
                description=self.description.value or "",
                accuracy_m=self.point.accuracy_m,
                photo_path=self.photo_path,
                photo_bytes=self.photo_bytes if not self.photo_path else None,
            )
        except Exception as exc:  # noqa: BLE001
            log.exception("Error guardando alerta")
            self.error_txt.value = f"No se pudo guardar: {exc}"
            self.error_txt.visible = True
            self.save_btn.disabled = False
            self.progress.visible = False
            self.page.update()
            return
        self.page.pop_dialog()
        await self.on_saved(alert)
