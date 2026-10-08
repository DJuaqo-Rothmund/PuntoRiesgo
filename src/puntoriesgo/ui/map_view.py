"""Contenedor del mapa Leaflet dentro de Flet.

* Android / iOS / macOS / modo web: ``flet_webview.WebView`` apuntando al
  servidor local.
* En modo web los controles superpuestos al iframe no reciben clics, por lo que
  la UI usa una barra inferior y oculta el mapa mientras hay diálogos abiertos.
* Windows / Linux de escritorio (WebView no soportado por Flet): se muestra un
  panel que abre el mapa en el navegador del sistema. Para desarrollar con el
  mapa embebido en escritorio, ejecutar ``flet run --web``.
"""

from __future__ import annotations

import logging

import flet as ft

log = logging.getLogger(__name__)

WEBVIEW_PLATFORMS = (ft.PagePlatform.ANDROID, ft.PagePlatform.IOS, ft.PagePlatform.MACOS)


def webview_supported(page: ft.Page) -> bool:
    return bool(page.web) or page.platform in WEBVIEW_PLATFORMS


class MapView:
    def __init__(self, page: ft.Page, url: str):
        self.page = page
        self.url = url
        self.embedded = webview_supported(page)
        if self.embedded:
            import flet_webview as fwv

            self.webview = fwv.WebView(
                url=url,
                expand=True,
                bgcolor=ft.Colors.BLACK,
                on_web_resource_error=self._on_error,
                on_console_message=self._on_console,
            )
            # Stack: permite ocultar el WebView (ver obscure) sin dejar al
            # SafeArea padre con un contenido invisible.
            self.control: ft.Control = ft.Stack([self.webview], expand=True)
        else:
            self.webview = None
            self.control = self._desktop_fallback()

    # ------------------------------------------------------------------ #
    def _desktop_fallback(self) -> ft.Control:
        async def open_browser(_e=None):
            await ft.UrlLauncher().launch_url(self.url)

        return ft.Container(
            expand=True,
            bgcolor=ft.Colors.BLUE_GREY_900,
            alignment=ft.Alignment.CENTER,
            padding=24,
            content=ft.Column(
                horizontal_alignment=ft.CrossAxisAlignment.CENTER,
                alignment=ft.MainAxisAlignment.CENTER,
                tight=True,
                controls=[
                    ft.Icon(ft.Icons.SATELLITE_ALT, size=64, color=ft.Colors.WHITE_70),
                    ft.Text(
                        "El WebView no está disponible en esta plataforma de escritorio.",
                        color=ft.Colors.WHITE, text_align=ft.TextAlign.CENTER,
                    ),
                    ft.Text(
                        "Abre el mapa en el navegador o ejecuta `flet run --web`.",
                        color=ft.Colors.WHITE_70, size=12, text_align=ft.TextAlign.CENTER,
                    ),
                    ft.Button(content="Abrir mapa", icon=ft.Icons.OPEN_IN_BROWSER,
                              on_click=open_browser),
                    ft.Text(self.url, selectable=True, size=11, color=ft.Colors.WHITE_54),
                ],
            ),
        )

    def _on_error(self, e) -> None:
        log.warning("WebView error: %s", e.data)

    def _on_console(self, e) -> None:
        log.debug("JS: %s", getattr(e, "message", e))

    def obscure(self, hidden: bool) -> None:
        """En Flutter web el iframe del mapa captura los clics de los controles
        que se le superponen (diálogos). Mientras haya un diálogo abierto se
        oculta el mapa. En Android/iOS no es necesario y no hace nada."""
        if self.webview is not None and self.page.web:
            self.webview.visible = not hidden
            self.webview.update()

    async def reload(self) -> None:
        if self.webview is not None and not self.page.web:
            await self.webview.reload()
