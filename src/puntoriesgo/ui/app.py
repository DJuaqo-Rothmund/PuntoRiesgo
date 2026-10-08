"""Pantalla principal: mapa satelital a pantalla completa + FAB "Registrar Alerta".

Responsabilidades:
    * Arrancar el núcleo (:class:`AppContext`): servidor local + sync worker.
    * GPS continuo con ``flet_geolocator`` -> posición en el mapa.
    * Conectividad con ``ft.Connectivity`` -> despierta la Sync Queue.
    * Abrir el formulario con la posición GPS (FAB) o con un punto marcado
      manteniendo presionado el mapa.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any, Optional

import flet as ft

from ..app_context import AppContext
from ..models import Alert
from . import theme as T
from .alert_form import AlertForm, CapturePoint
from .map_view import MapView
from .offline_dialog import OfflineMapDialog

log = logging.getLogger(__name__)

GPS_MAX_AGE_S = 30
MOBILE = (ft.PagePlatform.ANDROID, ft.PagePlatform.IOS)


class PuntoRiesgoApp:
    def __init__(self, page: ft.Page):
        self.page = page
        self.ctx = AppContext()
        self.last_fix: Optional[CapturePoint] = None
        self.last_fix_ts = 0.0
        self.geolocator = None
        self.connectivity: Optional[ft.Connectivity] = None
        self.file_picker = ft.FilePicker()
        self.closed = False
        self.connected = True  # en modo web la página puede desconectarse
        self.sync_dot = ft.Container(width=8, height=8, border_radius=4, bgcolor=T.MUTED)
        self.sync_text = ft.Text("Conectando…", size=12, color=T.TEXT, font_family=T.FONT_SEMI)

    # ------------------------------------------------------------------ #
    async def start(self) -> None:
        page = self.page
        page.title = "PuntoRiesgo"
        page.padding = 0
        page.spacing = 0
        T.apply(page)

        self.ctx.start()
        self.ctx.add_event_listener(self._on_map_event_threadsafe)
        self.ctx.add_sync_listener(self._on_sync_status_threadsafe)
        page.on_close = self._on_close
        page.on_disconnect = lambda e: setattr(self, "connected", False)
        page.on_connect = lambda e: setattr(self, "connected", True)
        page.on_app_lifecycle_state_change = self._on_lifecycle

        self.map_view = MapView(page, self.ctx.map_url)
        page.appbar = self._build_appbar()

        fab = ft.FloatingActionButton(
            content=ft.Row(
                [ft.Icon(ft.Icons.ADD_LOCATION_ALT_ROUNDED, color=ft.Colors.WHITE, size=22),
                 ft.Text("Registrar alerta", size=15.5, color=ft.Colors.WHITE,
                         font_family=T.FONT_BOLD)],
                spacing=10, tight=True, alignment=ft.MainAxisAlignment.CENTER,
            ),
            width=230,
            height=58,
            bgcolor=T.DANGER,
            elevation=8,
            shape=ft.StadiumBorder(),
            on_click=self._on_fab,
        )
        if page.web:
            # En web el iframe del mapa taparía los clics del FAB flotante.
            page.bottom_appbar = ft.BottomAppBar(
                content=ft.Row([fab], alignment=ft.MainAxisAlignment.CENTER),
                bgcolor=T.SURFACE,
                height=86,
            )
        else:
            page.floating_action_button = fab
            page.floating_action_button_location = ft.FloatingActionButtonLocation.CENTER_FLOAT
        page.add(ft.SafeArea(content=self.map_view.control, expand=True))

        if self.ctx.layers_error:
            self._snack(f"No se pudo cargar la capa de sectores: {self.ctx.layers_error}", error=True)

        await self._start_connectivity()
        await self._start_gps()

    def _build_appbar(self) -> ft.AppBar:
        layers = self.ctx.layers
        ha = sum(float(f.properties.get("hectareas") or 0) for f in layers.sectors)
        subtitle = f"{len(layers.sectors)} sectores" + (f" · {ha:.0f} ha" if ha else "")
        menu_item = lambda text, icon, handler: ft.PopupMenuItem(  # noqa: E731
            content=ft.Row([ft.Icon(icon, size=20, color=T.ACCENT),
                            ft.Text(text, size=14, color=T.TEXT)], spacing=14),
            on_click=handler,
        )
        return ft.AppBar(
            toolbar_height=68,
            bgcolor=ft.Colors.with_opacity(0.96, T.BG),
            elevation=0,
            leading=ft.Container(T.logo_mark(38), padding=ft.Padding.only(left=14),
                                 alignment=ft.Alignment.CENTER_LEFT),
            leading_width=58,
            title_spacing=12,
            title=ft.Column(
                spacing=0, tight=True,
                controls=[
                    ft.Text(layers.name or "PuntoRiesgo", size=17.5, color=T.TEXT,
                            font_family=T.FONT_BOLD),
                    ft.Text(subtitle, size=11.5, color=T.MUTED, no_wrap=True),
                ],
            ),
            actions=[
                ft.Container(
                    content=ft.Row([self.sync_dot, self.sync_text], spacing=7, tight=True),
                    padding=ft.Padding.symmetric(horizontal=12, vertical=7),
                    border_radius=20,
                    bgcolor=T.SURFACE_2,
                    border=ft.Border.all(1, T.OUTLINE),
                ),
                ft.PopupMenuButton(
                    icon=ft.Icons.MORE_VERT_ROUNDED,
                    icon_color=T.TEXT,
                    bgcolor=T.SURFACE_2,
                    menu_padding=ft.Padding.symmetric(vertical=6),
                    shape=ft.RoundedRectangleBorder(radius=16),
                    items=[
                        menu_item("Mapa offline", ft.Icons.DOWNLOAD_FOR_OFFLINE_ROUNDED,
                                  lambda e: self._show(OfflineMapDialog(self.page, self.ctx))),
                        menu_item("Centrar en mi posición", ft.Icons.MY_LOCATION_ROUNDED,
                                  lambda e: self.ctx.state.push_command("follow")),
                        menu_item("Ver todo el predio", ft.Icons.ZOOM_OUT_MAP_ROUNDED,
                                  lambda e: self.ctx.state.push_command("fit")),
                        menu_item("Sincronizar ahora", ft.Icons.SYNC_ROUNDED,
                                  lambda e: self.ctx.sync_worker.wake()),
                        ft.PopupMenuItem(),  # separador
                        menu_item("Importar sectores (GeoJSON/KML)", ft.Icons.UPLOAD_FILE_ROUNDED,
                                  self._import_layers),
                    ],
                ),
                ft.Container(width=4),
            ],
        )

    # ------------------------------------------------------------------ #
    # GPS
    # ------------------------------------------------------------------ #
    async def _start_gps(self) -> None:
        fake = self.ctx.cfg.fake_gps.strip()
        if fake:
            lat, lon = (float(v) for v in fake.split(","))
            self._set_fix(lat, lon, 5.0)
            return
        if self.page.web or self.page.platform not in (*MOBILE, ft.PagePlatform.MACOS):
            log.info("GPS no disponible en esta plataforma; usar PUNTORIESGO_FAKE_GPS")
            return
        import flet_geolocator as ftg

        self.geolocator = ftg.Geolocator(
            configuration=ftg.GeolocatorConfiguration(
                accuracy=ftg.GeolocatorPositionAccuracy.BEST,
                distance_filter=2,  # metros
            ),
            on_position_change=self._on_position,
            on_error=lambda e: log.warning("GPS error: %s", e.data),
        )
        try:
            status = await self.geolocator.request_permission()
            if status in (ftg.GeolocatorPermissionStatus.DENIED,
                          ftg.GeolocatorPermissionStatus.DENIED_FOREVER):
                self._snack("Sin permiso de ubicación: marca los riesgos manteniendo "
                            "presionado el mapa.", error=True)
                return
            if not await self.geolocator.is_location_service_enabled():
                self._snack("Activa la ubicación (GPS) del dispositivo.", error=True)
            last = await self.geolocator.get_last_known_position()
            if last and last.latitude is not None:
                self._set_fix(last.latitude, last.longitude, last.accuracy, ts=0)
        except Exception as exc:  # noqa: BLE001
            log.warning("No se pudo iniciar el GPS: %s", exc)

    def _on_position(self, e) -> None:
        p = e.position
        if p and p.latitude is not None:
            self._set_fix(p.latitude, p.longitude, p.accuracy, heading=p.heading)

    def _set_fix(self, lat: float, lon: float, accuracy: Optional[float],
                 heading: Optional[float] = None, ts: Optional[float] = None) -> None:
        self.last_fix = CapturePoint(lat, lon, accuracy, "gps")
        self.last_fix_ts = time.monotonic() if ts is None else ts
        self.ctx.state.set_position(lat, lon, accuracy, heading)

    async def _fresh_fix(self) -> Optional[CapturePoint]:
        """Posición reciente; si la última es vieja, pide una nueva (máx. 8 s)."""
        if self.last_fix and time.monotonic() - self.last_fix_ts <= GPS_MAX_AGE_S:
            return self.last_fix
        if self.geolocator is not None:
            try:
                p = await asyncio.wait_for(self.geolocator.get_current_position(), timeout=8)
                if p and p.latitude is not None:
                    self._set_fix(p.latitude, p.longitude, p.accuracy)
                    return self.last_fix
            except Exception as exc:  # noqa: BLE001
                log.warning("get_current_position falló: %s", exc)
        return self.last_fix if self.ctx.cfg.fake_gps else None

    # ------------------------------------------------------------------ #
    # Conectividad
    # ------------------------------------------------------------------ #
    async def _start_connectivity(self) -> None:
        if self.page.web:
            return
        try:
            self.connectivity = ft.Connectivity(on_change=self._on_connectivity)
            kinds = await self.connectivity.get_connectivity()
            self._apply_connectivity(kinds)
        except Exception as exc:  # noqa: BLE001
            log.info("Connectivity no disponible (%s); se usa sondeo del backend", exc)

    def _on_connectivity(self, e) -> None:
        self._apply_connectivity(e.connectivity)

    def _apply_connectivity(self, kinds) -> None:
        online = any(k != ft.ConnectivityType.NONE for k in (kinds or []))
        log.info("Conectividad: %s", kinds)
        self.ctx.set_online(online)

    # ------------------------------------------------------------------ #
    # Registrar alerta
    # ------------------------------------------------------------------ #
    async def _on_fab(self, _e=None) -> None:
        fix = await self._fresh_fix()
        if fix is None:
            self._snack("Sin señal GPS. Mantén presionado el mapa en el lugar del riesgo "
                        "para marcarlo manualmente.", error=True)
            return
        self._open_form(fix)

    def _open_form(self, point: CapturePoint) -> None:
        self._show(AlertForm(self.page, self.ctx, point, on_saved=self._on_alert_saved))

    def _show(self, dlg) -> None:
        """Abre un diálogo (AlertForm / OfflineMapDialog) gestionando el mapa."""
        self.map_view.obscure(True)

        def restore(_e=None):
            self.map_view.obscure(False)

        dlg.dialog.on_dismiss = restore
        dlg.open()

    async def _on_alert_saved(self, alert: Alert) -> None:
        self.ctx.state.push_command("clear_pick")
        where = alert.sector_name or "fuera de sectores"
        if self.ctx.sync_worker.backend_reachable:
            self._snack(f"Alerta registrada · {where}. Sincronizando…")
        else:
            self._snack(f"Guardada sin conexión · {where}. Se enviará al volver la señal.",
                        kind="offline")

    # ------------------------------------------------------------------ #
    # Eventos desde el mapa (llegan en el hilo del servidor HTTP)
    # ------------------------------------------------------------------ #
    def _on_map_event_threadsafe(self, evt: dict[str, Any]) -> None:
        async def handle() -> None:
            await self._on_map_event(evt)

        if not self.closed and self.connected:
            self.page.run_task(handle)

    async def _on_map_event(self, evt: dict[str, Any]) -> None:
        if evt.get("type") == "map_pick":
            point = CapturePoint(float(evt["lat"]), float(evt["lon"]), None, "mapa")
            self._open_form(point)

    def _on_sync_status_threadsafe(self, status: dict) -> None:
        async def handle() -> None:
            pending = status.get("pending", 0)
            online = status.get("backend_reachable")
            if pending:
                self.sync_dot.bgcolor = T.ACCENT
                self.sync_text.value = f"{pending} por enviar"
            elif online:
                self.sync_dot.bgcolor = T.OK
                self.sync_text.value = "Al día"
            else:
                self.sync_dot.bgcolor = T.MUTED
                self.sync_text.value = "Sin conexión"
            self.page.update()

        if self.closed or not self.connected:
            return
        try:
            self.page.run_task(handle)
        except Exception:  # noqa: BLE001 - la página puede estar cerrándose
            pass

    # ------------------------------------------------------------------ #
    # Ciclo de vida
    # ------------------------------------------------------------------ #
    def _on_lifecycle(self, e) -> None:
        # Al volver a primer plano: reintentar la cola de inmediato.
        if getattr(e, "state", None) == ft.AppLifecycleState.RESUME:
            self.ctx.sync_worker.wake()

    def _on_close(self, _e=None) -> None:
        self.closed = True
        self.ctx.stop()

    # ------------------------------------------------------------------ #
    async def _import_layers(self, _e=None) -> None:
        files = await self.file_picker.pick_files(
            dialog_title="Capa de sectores y equipos de riego",
            file_type=ft.FilePickerFileType.CUSTOM,
            allowed_extensions=["geojson", "json", "kml", "kmz"],
            with_data=True,
        )
        if not files:
            return
        f = files[0]
        data = f.bytes
        if data is None and f.path:
            data = await asyncio.to_thread(lambda: open(f.path, "rb").read())
        await asyncio.to_thread(self.ctx.import_layer_file, f.name, data)
        if self.ctx.layers_error:
            self._snack(f"Archivo inválido: {self.ctx.layers_error}", error=True)
        else:
            n_s, n_e = len(self.ctx.layers.sectors), len(self.ctx.layers.equipment)
            self._snack(f"Capa cargada: {n_s} sectores, {n_e} equipos de riego.")
            self.ctx.state.push_command("fit")

    def _snack(self, text: str, error: bool = False, kind: str = "info") -> None:
        self.page.show_dialog(T.snack(text, "error" if error else kind))


async def main(page: ft.Page) -> None:
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    app = PuntoRiesgoApp(page)
    await app.start()
