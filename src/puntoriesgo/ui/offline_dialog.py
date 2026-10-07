"""Diálogo "Mapa offline": pre-descarga de tiles del área de trabajo.

Calcula el plan (tiles que tocan cada sector + buffer, en el rango de zoom
elegido), muestra cantidad y tamaño estimado, y descarga en segundo plano con
barra de progreso y cancelación. Es reanudable: los tiles ya cacheados se
omiten.
"""

from __future__ import annotations

import asyncio
import logging
import threading
from typing import Optional

import flet as ft

from ..app_context import AppContext
from ..geo.tile_cache import DownloadPlan, DownloadProgress, TileLimitExceeded

log = logging.getLogger(__name__)


def _mb(n_bytes: float) -> str:
    return f"{n_bytes / 1_048_576:.1f} MB"


class OfflineMapDialog:
    def __init__(self, page: ft.Page, ctx: AppContext):
        self.page = page
        self.ctx = ctx
        self.plan: Optional[DownloadPlan] = None
        self.cancel_event = threading.Event()
        self.running = False

        src = ctx.cfg.tile_source
        zooms = [str(z) for z in range(10, src.max_native_zoom + 1)]
        self.min_zoom = ft.Dropdown(
            label="Zoom mínimo", value=str(ctx.cfg.offline_min_zoom), width=140,
            options=[ft.DropdownOption(key=z, text=z) for z in zooms],
            on_select=self._invalidate,
        )
        self.max_zoom = ft.Dropdown(
            label="Zoom máximo", value=str(min(ctx.cfg.offline_max_zoom, src.max_native_zoom)),
            width=140, options=[ft.DropdownOption(key=z, text=z) for z in zooms],
            on_select=self._invalidate,
        )
        self.info = ft.Text(size=13)
        self.cache_info = ft.Text(size=12, color=ft.Colors.ON_SURFACE_VARIANT)
        self.bar = ft.ProgressBar(value=0, visible=False)
        self.progress_txt = ft.Text(size=12, visible=False)

        self.calc_btn = ft.OutlinedButton(content="Calcular", icon=ft.Icons.CALCULATE,
                                          on_click=self._calculate)
        self.download_btn = ft.FilledButton(content="Descargar", icon=ft.Icons.DOWNLOAD,
                                            on_click=self._download, disabled=True)
        self.cancel_btn = ft.TextButton(content="Cerrar", on_click=self._close)
        self.clear_btn = ft.TextButton(content="Borrar caché", icon=ft.Icons.DELETE_OUTLINE,
                                       on_click=self._clear_cache)

        n_sectors = len(ctx.layers.sectors)
        self.dialog = ft.AlertDialog(
            modal=True,
            title=ft.Text("Mapa satelital offline"),
            content=ft.Column(
                tight=True, width=420, spacing=12,
                controls=[
                    ft.Text(
                        f"Fuente: {src.name}. Área: {n_sectors} sectores + "
                        f"{ctx.cfg.offline_buffer_m:.0f} m de margen.",
                        size=13,
                    ),
                    ft.Row([self.min_zoom, self.max_zoom], wrap=True),
                    ft.Text("Zoom 18 ≈ 0,6 m/píxel · 19 ≈ 0,3 m/píxel. Cada nivel "
                            "extra multiplica ~4× los tiles.", size=11,
                            color=ft.Colors.ON_SURFACE_VARIANT),
                    self.info,
                    self.bar,
                    self.progress_txt,
                    self.cache_info,
                ],
            ),
            actions=[self.clear_btn, self.cancel_btn, self.calc_btn, self.download_btn],
        )

    # ------------------------------------------------------------------ #
    def open(self) -> None:
        self._refresh_cache_info()
        if not self.ctx.layers.sectors and not self.ctx.layers.equipment:
            self.info.value = "⚠ No hay capa de sectores cargada: no se puede definir el área."
            self.calc_btn.disabled = True
        self.page.show_dialog(self.dialog)

    def _refresh_cache_info(self) -> None:
        st = self.ctx.tile_store.stats()
        if st["count"]:
            self.cache_info.value = (
                f"En caché: {st['count']:,} tiles ({_mb(st['bytes'])}), "
                f"zoom {st['min_zoom']}–{st['max_zoom']}."
            ).replace(",", ".")
        else:
            self.cache_info.value = "Caché vacía."

    def _invalidate(self, _e=None) -> None:
        self.plan = None
        self.download_btn.disabled = True
        self.info.value = ""
        self.page.update()

    async def _calculate(self, _e=None) -> None:
        zmin, zmax = int(self.min_zoom.value), int(self.max_zoom.value)
        if zmin > zmax:
            self.info.value = "El zoom mínimo no puede ser mayor que el máximo."
            self.page.update()
            return
        self.info.value = "Calculando…"
        self.page.update()
        downloader = self.ctx.tile_downloader()
        try:
            self.plan = await asyncio.to_thread(
                downloader.plan, self.ctx.work_areas(), zmin, zmax, self.ctx.cfg.offline_max_tiles
            )
        except TileLimitExceeded as exc:
            self.info.value = f"⚠ {exc}"
            self.page.update()
            return
        p = self.plan
        self.info.value = (
            f"{p.total:,} tiles en total · {p.already_cached:,} ya en caché · "
            f"{p.to_download:,} por descargar (≈ {p.estimated_mb:.0f} MB)."
        ).replace(",", ".")
        self.download_btn.disabled = p.to_download == 0
        self.page.update()

    async def _download(self, _e=None) -> None:
        if not self.plan or self.running:
            return
        if not self.ctx.online:
            self.info.value = "⚠ Sin conexión. Conéctate a WiFi para descargar el mapa."
            self.page.update()
            return
        self.running = True
        self.cancel_event.clear()
        self.bar.visible = self.progress_txt.visible = True
        self.download_btn.disabled = self.calc_btn.disabled = True
        self.min_zoom.disabled = self.max_zoom.disabled = True
        self.cancel_btn.content = "Cancelar descarga"
        self.page.update()

        loop = asyncio.get_running_loop()

        def on_progress(prog: DownloadProgress) -> None:
            # Llega desde un hilo de trabajo: se reenvía al loop de Flet.
            loop.call_soon_threadsafe(self._render_progress, prog)

        downloader = self.ctx.tile_downloader()
        result = await asyncio.to_thread(downloader.run, self.plan, on_progress, self.cancel_event)

        self.running = False
        self.plan = None
        self.min_zoom.disabled = self.max_zoom.disabled = False
        self.calc_btn.disabled = False
        self.cancel_btn.content = "Cerrar"
        if result.aborted_reason:
            self.info.value = (f"⚠ Descarga detenida: {result.aborted_reason}. "
                               "Reintenta con mejor señal; se reanuda donde quedó.")
        elif result.cancelled:
            self.info.value = "Descarga cancelada. Puedes reanudarla: se omiten los tiles ya bajados."
        else:
            self.info.value = (
                f"✔ Listo: {result.done:,} tiles disponibles offline"
                + (f", {result.failed} fallidos (reintenta luego)." if result.failed else ".")
            ).replace(",", ".")
        self._refresh_cache_info()
        self.page.update()

    def _render_progress(self, prog: DownloadProgress) -> None:
        self.bar.value = prog.fraction
        self.progress_txt.value = (
            f"{prog.done + prog.failed:,}/{prog.total:,} · {_mb(prog.bytes)} · "
            f"{prog.rate_tiles_s:.0f} tiles/s" + (f" · {prog.failed} errores" if prog.failed else "")
        ).replace(",", ".")
        self.page.update()

    async def _clear_cache(self, _e=None) -> None:
        if self.running:
            return
        await asyncio.to_thread(self.ctx.tile_store.clear)
        self._invalidate()
        self._refresh_cache_info()
        self.page.update()

    def _close(self, _e=None) -> None:
        if self.running:
            self.cancel_event.set()
            self.cancel_btn.content = "Cancelando…"
            self.page.update()
            return
        self.page.pop_dialog()
