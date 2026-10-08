"""Formulario "Registrar alerta" (hoja inferior).

* Ubicación, sector y equipo de riego se calculan por cruce espacial y se
  muestran como tarjeta informativa.
* El tipo de problema se elige en una grilla de íconos (un toque, sin menú).
* La severidad se elige con cuatro botones del color de su pin.
* La foto se toma con la cámara (Android/iOS) o se elige de la galería, y
  se comprime localmente al guardar (en un hilo, sin congelar la UI).
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import Awaitable, Callable, Optional

import flet as ft

from . import theme as T
from ..app_context import AppContext
from ..models import Alert, RiskType, Severity
from .camera_capture import CameraCapture, camera_supported

log = logging.getLogger(__name__)

# Íconos Material para cada tipo (no dependen de fuentes emoji del sistema).
RISK_ICONS = {
    RiskType.HOYO: ft.Icons.RADIO_BUTTON_UNCHECKED_ROUNDED,
    RiskType.ZANJA: ft.Icons.WAVES_ROUNDED,
    RiskType.PIEDRA: ft.Icons.LANDSCAPE_ROUNDED,
    RiskType.RAMA_CAIDA: ft.Icons.PARK_ROUNDED,
    RiskType.CAMINO_INUNDADO: ft.Icons.FLOOD_ROUNDED,
    RiskType.BARRO: ft.Icons.TERRAIN_ROUNDED,
    RiskType.FUGA_RIEGO: ft.Icons.WATER_DROP_ROUNDED,
    RiskType.CABLE_ELECTRICO: ft.Icons.ELECTRIC_BOLT_ROUNDED,
    RiskType.ANIMAL: ft.Icons.PETS_ROUNDED,
    RiskType.OTRO: ft.Icons.HELP_OUTLINE_ROUNDED,
}

# Etiquetas cortas para que quepan en la grilla de dos columnas.
RISK_SHORT = {
    RiskType.RAMA_CAIDA: "Rama / árbol",
    RiskType.CAMINO_INUNDADO: "Camino inundado",
    RiskType.BARRO: "Barro",
    RiskType.FUGA_RIEGO: "Fuga de riego",
    RiskType.CABLE_ELECTRICO: "Cable eléctrico",
    RiskType.ANIMAL: "Animal",
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
        self.risk: Optional[RiskType] = None
        self.severity: Severity = Severity.MEDIA
        self.saving = False
        self.file_picker = ft.FilePicker()  # servicio: se registra al construirse

        match = ctx.locate(point.lat, point.lon)

        # ---- Encabezado ------------------------------------------------ #
        acc = f" · ±{point.accuracy_m:.0f} m" if point.accuracy_m else ""
        warn = point.accuracy_m is not None and point.accuracy_m > 30
        source = "GPS" if point.source == "gps" else "Punto marcado en el mapa"
        header = ft.Row(
            vertical_alignment=ft.CrossAxisAlignment.CENTER,
            controls=[
                ft.Container(
                    width=46, height=46, border_radius=15,
                    bgcolor=ft.Colors.with_opacity(0.16, T.DANGER),
                    alignment=ft.Alignment.CENTER,
                    content=ft.Icon(ft.Icons.ADD_LOCATION_ALT_ROUNDED, color=T.DANGER, size=26),
                ),
                ft.Column(
                    spacing=2, expand=True,
                    controls=[
                        T.title("Nueva alerta", 21),
                        ft.Text(
                            f"{source} · {point.lat:.5f}, {point.lon:.5f}{acc}"
                            + ("  ·  precisión baja" if warn else ""),
                            size=12, color=T.ACCENT if warn else T.MUTED,
                        ),
                    ],
                ),
                ft.IconButton(ft.Icons.CLOSE_ROUNDED, icon_color=T.MUTED,
                              tooltip="Cancelar", on_click=self._cancel),
            ],
        )

        # ---- Ubicación (cruce espacial) -------------------------------- #
        location_card = T.card(
            ft.Column(
                spacing=12,
                controls=[
                    T.info_row(ft.Icons.GRID_VIEW_ROUNDED, "Sector", match.sector_label,
                               T.TEXT if match.sector_name else T.MUTED),
                    ft.Divider(height=1, color=T.OUTLINE),
                    T.info_row(ft.Icons.WATER_DROP_ROUNDED, "Equipo de riego",
                               match.equipment_label),
                ],
            )
        )

        # ---- Tipo de problema (grilla) --------------------------------- #
        self.risk_tiles: dict[RiskType, ft.Container] = {}
        tiles = []
        for r in RiskType:
            tile = ft.Container(
                col={"xs": 6, "sm": 4},
                height=54,
                border_radius=14,
                padding=ft.Padding.symmetric(horizontal=12),
                on_click=lambda e, rt=r: self._select_risk(rt),
                ink=True,
                content=ft.Row(
                    spacing=10,
                    controls=[
                        ft.Icon(RISK_ICONS[r], size=21),
                        ft.Text(RISK_SHORT.get(r, r.label), size=13.5,
                                font_family=T.FONT_SEMI, expand=True,
                                max_lines=2, overflow=ft.TextOverflow.ELLIPSIS),
                    ],
                ),
            )
            self.risk_tiles[r] = tile
            tiles.append(tile)
        self._paint_risks()
        risk_grid = ft.ResponsiveRow(tiles, spacing=8, run_spacing=8)

        # ---- Severidad -------------------------------------------------- #
        self.sev_pills: dict[Severity, ft.Container] = {}
        pills = []
        for s in Severity:
            pill = ft.Container(
                expand=True, height=44, border_radius=12,
                alignment=ft.Alignment.CENTER,
                on_click=lambda e, sv=s: self._select_severity(sv),
                ink=True,
                content=ft.Text(s.label, size=13.5, font_family=T.FONT_BOLD),
            )
            self.sev_pills[s] = pill
            pills.append(pill)
        self._paint_severity()
        severity_row = ft.Row(pills, spacing=8)

        # ---- Observaciones --------------------------------------------- #
        self.description = ft.TextField(
            multiline=True, min_lines=2, max_lines=4,
            hint_text="Ej.: zanja de 1 m en el camino principal, no pasa camioneta",
            filled=True, fill_color=T.SURFACE_2, border_radius=14,
            border_color=T.OUTLINE, focused_border_color=T.ACCENT,
            text_style=ft.TextStyle(size=14.5, color=T.TEXT),
            hint_style=ft.TextStyle(size=13.5, color=T.MUTED),
            cursor_color=T.ACCENT,
            content_padding=ft.Padding.all(14),
            expand=True,
        )

        # ---- Foto ------------------------------------------------------- #
        self.camera_btn = T.secondary_button("Tomar foto", ft.Icons.PHOTO_CAMERA_ROUNDED,
                                             self._take_photo)
        self.camera_btn.visible = camera_supported(page)
        self.gallery_btn = T.secondary_button("Galería", ft.Icons.PHOTO_LIBRARY_ROUNDED,
                                              self._pick_photo)
        self.photo_preview = ft.Image(src=b"", height=190, fit=ft.BoxFit.COVER,
                                      border_radius=T.RADIUS, expand=True)
        self.photo_box = ft.Stack(
            visible=False,
            controls=[
                ft.Row([self.photo_preview]),
                ft.Container(
                    top=10, right=10, width=34, height=34, border_radius=17,
                    bgcolor=ft.Colors.with_opacity(0.6, ft.Colors.BLACK),
                    alignment=ft.Alignment.CENTER,
                    on_click=self._remove_photo,
                    content=ft.Icon(ft.Icons.CLOSE_ROUNDED, size=18, color=ft.Colors.WHITE),
                ),
            ],
        )

        # ---- Guardar ---------------------------------------------------- #
        self.error_txt = ft.Text("", color=T.DANGER, size=13, visible=False)
        self.save_btn = T.primary_button("Guardar alerta", ft.Icons.CHECK_ROUNDED, self._save)
        self.saving_ring = ft.ProgressRing(width=22, height=22, stroke_width=3,
                                           color=ft.Colors.WHITE, visible=False)

        body = ft.Column(
            spacing=0,
            tight=True,
            controls=[
                header,
                ft.Container(height=16),
                location_card,
                ft.Container(height=22),
                T.eyebrow("Tipo de problema"),
                ft.Container(height=10),
                risk_grid,
                ft.Container(height=22),
                T.eyebrow("Severidad"),
                ft.Container(height=10),
                severity_row,
                ft.Container(height=22),
                T.eyebrow("Observaciones"),
                ft.Container(height=10),
                ft.Row([self.description]),
                ft.Container(height=22),
                T.eyebrow("Foto"),
                ft.Container(height=10),
                ft.Row([self.camera_btn, self.gallery_btn], spacing=10),
                ft.Container(height=10),
                self.photo_box,
                self.error_txt,
                ft.Container(height=14),
                ft.Stack([
                    ft.Row([ft.Container(self.save_btn, expand=True)]),
                    ft.Container(self.saving_ring, right=18, top=16),
                ]),
            ],
        )
        self.dialog = ft.BottomSheet(
            content=ft.Container(
                content=body,
                padding=ft.Padding.only(left=20, right=20, top=4, bottom=24),
            ),
            bgcolor=T.SURFACE,
            scrollable=True,
            show_drag_handle=True,
            use_safe_area=True,
            maintain_bottom_view_insets_padding=True,
            shape=ft.RoundedRectangleBorder(
                radius=ft.BorderRadius.only(top_left=28, top_right=28)),
        )

    # ------------------------------------------------------------------ #
    # Selección
    # ------------------------------------------------------------------ #
    def _paint_risks(self) -> None:
        for r, tile in self.risk_tiles.items():
            on = r == self.risk
            tile.bgcolor = ft.Colors.with_opacity(0.14, T.ACCENT) if on else T.SURFACE_2
            tile.border = ft.Border.all(1.6 if on else 1, T.ACCENT if on else T.OUTLINE)
            icon, text = tile.content.controls
            icon.color = T.ACCENT if on else T.MUTED
            text.color = T.TEXT if on else "#C9D4CF"

    def _select_risk(self, r: RiskType) -> None:
        self.risk = r
        self.error_txt.visible = False
        self._paint_risks()
        self.page.update()

    def _paint_severity(self) -> None:
        for s, pill in self.sev_pills.items():
            on = s == self.severity
            pill.bgcolor = s.color if on else T.SURFACE_2
            pill.border = ft.Border.all(1, s.color if on else T.OUTLINE)
            pill.content.color = "#14100A" if on else s.color

    def _select_severity(self, s: Severity) -> None:
        self.severity = s
        self._paint_severity()
        self.page.update()

    # ------------------------------------------------------------------ #
    def open(self) -> None:
        self.page.show_dialog(self.dialog)

    def _cancel(self, _e=None) -> None:
        self.page.pop_dialog()

    # ------------------------------------------------------------------ #
    # Foto
    # ------------------------------------------------------------------ #
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
            self._show_error(error)
        elif data:
            self.error_txt.visible = False
            await self._set_photo(None, data)

    async def _set_photo(self, path: Optional[str], data: Optional[bytes]) -> None:
        self.photo_path, self.photo_bytes = path, data
        try:
            self.photo_preview.src = await asyncio.to_thread(self._thumbnail)
            self.photo_box.visible = True
        except Exception as exc:  # noqa: BLE001
            log.warning("No se pudo previsualizar: %s", exc)
            self.photo_box.visible = False
        self._set_camera_label("Otra foto")
        self.page.update()

    def _remove_photo(self, _e=None) -> None:
        self.photo_path = self.photo_bytes = None
        self.photo_box.visible = False
        self._set_camera_label("Tomar foto")
        self.page.update()

    def _set_camera_label(self, text: str) -> None:
        self.camera_btn.content.controls[1].value = text

    def _thumbnail(self) -> bytes:
        import io

        try:
            from PIL import Image, ImageOps
        except ImportError:
            return self.photo_bytes or open(self.photo_path, "rb").read()
        src = io.BytesIO(self.photo_bytes) if self.photo_bytes else self.photo_path
        with Image.open(src) as img:
            img = ImageOps.exif_transpose(img)
            img.thumbnail((720, 720))
            out = io.BytesIO()
            img.convert("RGB").save(out, format="JPEG", quality=70)
            return out.getvalue()

    # ------------------------------------------------------------------ #
    # Guardar
    # ------------------------------------------------------------------ #
    def _show_error(self, text: str) -> None:
        self.error_txt.value = text
        self.error_txt.visible = True
        self.page.update()

    async def _save(self, _e=None) -> None:
        if self.saving:
            return
        if self.risk is None:
            self._show_error("Elige el tipo de problema.")
            return
        self.saving = True
        self.error_txt.visible = False
        self.save_btn.disabled = True
        self.saving_ring.visible = True
        self.page.update()
        try:
            alert = await asyncio.to_thread(
                self.ctx.create_alert,
                lat=self.point.lat,
                lon=self.point.lon,
                risk_type=self.risk,
                severity=self.severity,
                description=self.description.value or "",
                accuracy_m=self.point.accuracy_m,
                photo_path=self.photo_path,
                photo_bytes=self.photo_bytes if not self.photo_path else None,
            )
        except Exception as exc:  # noqa: BLE001
            log.exception("Error guardando alerta")
            self.saving = False
            self.save_btn.disabled = False
            self.saving_ring.visible = False
            self._show_error(f"No se pudo guardar: {exc}")
            return
        self.page.pop_dialog()
        await self.on_saved(alert)
