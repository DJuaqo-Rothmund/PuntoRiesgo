"""Sistema visual de PuntoRiesgo.

Línea "huerto nocturno": superficies verde-carbón translúcidas sobre la
imagen satelital, acento dorado para la marca y la selección, y coral
para las acciones de riesgo. Tipografía Manrope empaquetada en la app
(funciona offline). Los mismos tokens se replican en ``assets/web/map.css``.
"""

from __future__ import annotations

import flet as ft

# --------------------------------------------------------------------------- #
# Tokens
# --------------------------------------------------------------------------- #
BG = "#0B1110"
SURFACE = "#131B19"
SURFACE_2 = "#1A2421"
SURFACE_3 = "#223029"
OUTLINE = "#2C3A35"
TEXT = "#EEF3F0"
MUTED = "#93A39C"
ACCENT = "#E9B949"       # dorado: marca, selección, foco
ON_ACCENT = "#1B1403"
DANGER = "#E5484D"       # coral: registrar alerta / errores
OK = "#3DD68C"

FONT = "Manrope"
FONT_SEMI = "Manrope SemiBold"
FONT_BOLD = "Manrope Bold"
FONT_XBOLD = "Manrope ExtraBold"

FONTS = {
    FONT: "fonts/Manrope_500Medium.ttf",
    FONT_SEMI: "fonts/Manrope_600SemiBold.ttf",
    FONT_BOLD: "fonts/Manrope_700Bold.ttf",
    FONT_XBOLD: "fonts/Manrope_800ExtraBold.ttf",
}

RADIUS = 16


def apply(page: ft.Page) -> None:
    """Configura fuentes, esquema de color y componentes de la página."""
    page.fonts = dict(FONTS)
    scheme = ft.ColorScheme(
        primary=ACCENT,
        on_primary=ON_ACCENT,
        primary_container=SURFACE_3,
        on_primary_container=TEXT,
        secondary=OK,
        on_secondary=BG,
        error=DANGER,
        on_error="#FFFFFF",
        surface=SURFACE,
        on_surface=TEXT,
        on_surface_variant=MUTED,
        outline=OUTLINE,
        outline_variant=OUTLINE,
        surface_container=SURFACE_2,
        inverse_surface=TEXT,
        on_inverse_surface=BG,
    )
    theme = ft.Theme(
        color_scheme=scheme,
        font_family=FONT,
        use_material3=True,
    )
    page.theme = theme
    page.dark_theme = theme
    page.theme_mode = ft.ThemeMode.DARK
    page.bgcolor = BG


# --------------------------------------------------------------------------- #
# Piezas reutilizables
# --------------------------------------------------------------------------- #
def eyebrow(text: str) -> ft.Text:
    """Rótulo de sección pequeño en versalitas."""
    return ft.Text(text.upper(), size=11, color=MUTED, font_family=FONT_BOLD,
                   style=ft.TextStyle(letter_spacing=1.2))


def title(text: str, size: int = 20) -> ft.Text:
    return ft.Text(text, size=size, color=TEXT, font_family=FONT_BOLD)


def logo_mark(size: int = 38) -> ft.Container:
    """Isotipo: pin con alerta sobre degradado dorado."""
    return ft.Container(
        width=size, height=size, border_radius=size * 0.32,
        gradient=ft.LinearGradient(
            begin=ft.Alignment.TOP_LEFT, end=ft.Alignment.BOTTOM_RIGHT,
            colors=["#F4CF6B", "#D99A2B"],
        ),
        alignment=ft.Alignment.CENTER,
        content=ft.Icon(ft.Icons.WHERE_TO_VOTE_ROUNDED, color=ON_ACCENT, size=size * 0.58),
    )


def card(content: ft.Control, padding: int = 14, bgcolor: str = SURFACE_2) -> ft.Container:
    return ft.Container(
        content=content, padding=padding, bgcolor=bgcolor,
        border_radius=RADIUS, border=ft.Border.all(1, OUTLINE),
    )


def info_row(icon: ft.IconData, label: str, value, value_color: str = TEXT) -> ft.Row:
    """Fila ícono + rótulo + valor. ``value`` puede ser texto o un control."""
    if not isinstance(value, ft.Control):
        value = ft.Text(value, size=14.5, color=value_color, font_family=FONT_SEMI)
    return ft.Row(
        spacing=12,
        vertical_alignment=ft.CrossAxisAlignment.CENTER,
        controls=[
            ft.Container(
                width=36, height=36, border_radius=11, bgcolor=SURFACE_3,
                alignment=ft.Alignment.CENTER,
                content=ft.Icon(icon, size=19, color=ACCENT),
            ),
            ft.Column(
                spacing=1, expand=True,
                controls=[
                    ft.Text(label, size=11.5, color=MUTED),
                    value,
                ],
            ),
        ],
    )


def snack(text: str, kind: str = "info") -> ft.SnackBar:
    icon, color = {
        "info": (ft.Icons.CHECK_CIRCLE_ROUNDED, OK),
        "offline": (ft.Icons.CLOUD_QUEUE_ROUNDED, ACCENT),
        "error": (ft.Icons.ERROR_ROUNDED, DANGER),
    }[kind]
    return ft.SnackBar(
        behavior=ft.SnackBarBehavior.FLOATING,
        bgcolor=SURFACE_3,
        shape=ft.RoundedRectangleBorder(radius=14),
        margin=ft.Margin.only(left=16, right=16, bottom=96),
        duration=ft.Duration(seconds=5),
        content=ft.Row(
            spacing=12,
            controls=[
                ft.Icon(icon, color=color, size=22),
                ft.Text(text, color=TEXT, size=13.5, expand=True),
            ],
        ),
    )


def primary_button(text: str, icon: ft.IconData, on_click, color: str = DANGER,
                   fg: str = "#FFFFFF") -> ft.FilledButton:
    return ft.FilledButton(
        content=ft.Row(
            [ft.Icon(icon, size=20, color=fg),
             ft.Text(text, size=15.5, font_family=FONT_BOLD, color=fg)],
            alignment=ft.MainAxisAlignment.CENTER, spacing=10, tight=True,
        ),
        on_click=on_click,
        height=54,
        style=ft.ButtonStyle(
            bgcolor=color,
            shape=ft.RoundedRectangleBorder(radius=RADIUS),
            elevation=0,
        ),
    )


def secondary_button(text: str, icon: ft.IconData, on_click, expand: bool = True) -> ft.OutlinedButton:
    return ft.OutlinedButton(
        content=ft.Row(
            [ft.Icon(icon, size=19, color=TEXT),
             ft.Text(text, size=14, font_family=FONT_SEMI, color=TEXT)],
            alignment=ft.MainAxisAlignment.CENTER, spacing=8, tight=True,
        ),
        on_click=on_click,
        height=48,
        expand=expand,
        style=ft.ButtonStyle(
            bgcolor=SURFACE_2,
            side=ft.BorderSide(1, OUTLINE),
            shape=ft.RoundedRectangleBorder(radius=14),
        ),
    )
