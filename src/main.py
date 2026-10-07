"""Punto de entrada de PuntoRiesgo.

Desarrollo:
    flet run --web src/main.py          # mapa embebido en el navegador (escritorio)
    flet run --android src/main.py      # app Flet en el teléfono (misma red)

Build:
    flet build apk     /  flet build ipa
"""

import flet as ft

from puntoriesgo.ui.app import main

if __name__ == "__main__":
    ft.run(main)
