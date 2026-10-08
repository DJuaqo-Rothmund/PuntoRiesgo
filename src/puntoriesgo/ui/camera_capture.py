"""Captura de fotos con la cámara del teléfono (``flet-camera``).

Muestra la vista previa a pantalla completa con disparador, flash y cambio
de cámara trasera/frontal. Devuelve los bytes JPEG de la foto; la compresión
y el guardado los hace :meth:`AppContext.create_alert` como con la galería.

Sólo Android/iOS: en escritorio o web el formulario ofrece únicamente la
galería (ver :func:`camera_supported`).
"""

from __future__ import annotations

import asyncio
import logging
from typing import Awaitable, Callable, Optional

import flet as ft

log = logging.getLogger(__name__)

CAMERA_PLATFORMS = (ft.PagePlatform.ANDROID, ft.PagePlatform.IOS)
FLASH_CYCLE = ("off", "auto", "always")
FLASH_ICONS = {"off": ft.Icons.FLASH_OFF, "auto": ft.Icons.FLASH_AUTO, "always": ft.Icons.FLASH_ON}

# on_done(bytes_jpeg | None, mensaje_de_error | None)
DoneCallback = Callable[[Optional[bytes], Optional[str]], Awaitable[None]]


def camera_supported(page: ft.Page) -> bool:
    return not page.web and page.platform in CAMERA_PLATFORMS


class CameraCapture:
    def __init__(self, page: ft.Page, on_done: DoneCallback):
        import flet_camera as fc

        self.page = page
        self.on_done = on_done
        self.fc = fc
        self.cameras: list = []
        self.cam_index = 0
        self.flash = "off"
        self.busy = False
        self.finished = False

        self.camera = fc.Camera(expand=True, preview_enabled=True)
        self.status = ft.Text("Abriendo cámara…", color=ft.Colors.WHITE, size=13.5,
                              font_family="Manrope SemiBold")
        # Disparador: anillo blanco con núcleo (estilo cámara nativa).
        self.shutter_core = ft.Container(width=60, height=60, border_radius=30,
                                         bgcolor=ft.Colors.WHITE_54)
        self.shutter = ft.Container(
            width=80, height=80, border_radius=40,
            border=ft.Border.all(4, ft.Colors.WHITE),
            alignment=ft.Alignment.CENTER,
            content=self.shutter_core,
            on_click=self._shoot,
            disabled=True,
        )
        self.flash_btn = ft.IconButton(
            icon=FLASH_ICONS["off"], icon_color=ft.Colors.WHITE, icon_size=28,
            tooltip="Flash", on_click=self._toggle_flash, disabled=True,
        )
        self.flip_btn = ft.IconButton(
            icon=ft.Icons.CAMERASWITCH, icon_color=ft.Colors.WHITE, icon_size=28,
            tooltip="Cambiar cámara", on_click=self._flip, disabled=True,
        )
        close_btn = ft.IconButton(
            icon=ft.Icons.CLOSE, icon_color=ft.Colors.WHITE, icon_size=28,
            tooltip="Cancelar", on_click=self._cancel,
        )

        width = page.width or 400
        height = page.height or 720
        bar_bg = ft.Colors.with_opacity(0.45, ft.Colors.BLACK)
        self.dialog = ft.AlertDialog(
            modal=True,
            bgcolor=ft.Colors.BLACK,
            inset_padding=ft.Padding.only(),
            content_padding=ft.Padding.only(),
            shape=ft.RoundedRectangleBorder(radius=0),
            content=ft.Container(
                width=width,
                height=height,
                bgcolor=ft.Colors.BLACK,
                content=ft.Stack(
                    expand=True,
                    controls=[
                        self.camera,
                        ft.Container(  # barra superior
                            top=0, left=0, right=0, bgcolor=bar_bg,
                            padding=ft.Padding.only(left=8, right=8, top=28, bottom=4),
                            content=ft.Row(
                                [close_btn, self.status, self.flash_btn],
                                alignment=ft.MainAxisAlignment.SPACE_BETWEEN,
                            ),
                        ),
                        ft.Container(  # barra inferior con disparador
                            bottom=0, left=0, right=0, bgcolor=bar_bg,
                            padding=ft.Padding.only(left=24, right=24, top=8, bottom=24),
                            content=ft.Row(
                                [ft.Container(width=52), self.shutter, self.flip_btn],
                                alignment=ft.MainAxisAlignment.SPACE_BETWEEN,
                            ),
                        ),
                    ],
                ),
            ),
        )

    # ------------------------------------------------------------------ #
    async def open(self) -> None:
        if not await self._request_permission():
            await self._finish(None, "Sin permiso para usar la cámara. Actívalo en "
                                     "Ajustes → Apps → PuntoRiesgo → Permisos.")
            return
        self.page.show_dialog(self.dialog)
        try:
            await self._init_camera()
        except Exception as exc:  # noqa: BLE001
            log.exception("No se pudo iniciar la cámara")
            self.page.pop_dialog()
            await self._finish(None, f"No se pudo abrir la cámara: {exc}")

    async def _request_permission(self) -> bool:
        try:
            import flet_permission_handler as fph

            self._perm = fph.PermissionHandler()  # referencia fuerte al servicio
            status = await self._perm.request(fph.Permission.CAMERA)
            return status in (fph.PermissionStatus.GRANTED, fph.PermissionStatus.LIMITED)
        except Exception as exc:  # noqa: BLE001
            # Si el plugin de permisos falla, el plugin de cámara pide el permiso solo.
            log.warning("permission_handler no disponible: %s", exc)
            return True

    async def _init_camera(self) -> None:
        # El control debe estar montado en el cliente antes de invocar métodos.
        last_exc: Optional[Exception] = None
        for _ in range(10):
            await asyncio.sleep(0.25)
            try:
                self.cameras = await self.camera.get_available_cameras()
                break
            except Exception as exc:  # noqa: BLE001
                last_exc = exc
        else:
            raise RuntimeError(f"cámara no disponible ({last_exc})")
        if not self.cameras:
            raise RuntimeError("el dispositivo no reporta cámaras")
        back = self.fc.CameraLensDirection.BACK
        self.cam_index = next(
            (i for i, c in enumerate(self.cameras) if c.lens_direction == back), 0
        )
        await self.camera.initialize(
            self.cameras[self.cam_index],
            self.fc.ResolutionPreset.VERY_HIGH,  # 1080p: se reduce a 1600 px al guardar
            enable_audio=False,
            image_format_group=self.fc.ImageFormatGroup.JPEG,
        )
        self.status.value = ""
        self.shutter.disabled = False
        self.shutter_core.bgcolor = ft.Colors.WHITE
        self.flash_btn.disabled = False
        self.flip_btn.disabled = len(self.cameras) < 2
        self.page.update()

    # ------------------------------------------------------------------ #
    async def _shoot(self, _e=None) -> None:
        if self.busy:
            return
        self.busy = True
        self.shutter.disabled = True
        self.shutter_core.bgcolor = ft.Colors.WHITE_54
        self.status.value = "Guardando…"
        self.page.update()
        try:
            data = await self.camera.take_picture()
        except Exception as exc:  # noqa: BLE001
            log.exception("take_picture falló")
            self.busy = False
            self.shutter.disabled = False
            self.shutter_core.bgcolor = ft.Colors.WHITE
            self.status.value = f"Error: {exc}"
            self.page.update()
            return
        self.page.pop_dialog()
        await self._finish(bytes(data), None)

    async def _toggle_flash(self, _e=None) -> None:
        nxt = FLASH_CYCLE[(FLASH_CYCLE.index(self.flash) + 1) % len(FLASH_CYCLE)]
        try:
            await self.camera.set_flash_mode(self.fc.FlashMode(nxt))
        except Exception as exc:  # noqa: BLE001
            log.warning("Flash no soportado: %s", exc)
            return
        self.flash = nxt
        self.flash_btn.icon = FLASH_ICONS[nxt]
        self.page.update()

    async def _flip(self, _e=None) -> None:
        if len(self.cameras) < 2 or self.busy:
            return
        self.cam_index = (self.cam_index + 1) % len(self.cameras)
        try:
            await self.camera.set_description(self.cameras[self.cam_index])
        except Exception as exc:  # noqa: BLE001
            log.warning("No se pudo cambiar de cámara: %s", exc)

    async def _cancel(self, _e=None) -> None:
        self.page.pop_dialog()
        await self._finish(None, None)

    async def _finish(self, data: Optional[bytes], error: Optional[str]) -> None:
        if self.finished:
            return
        self.finished = True
        await self.on_done(data, error)
