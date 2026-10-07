"""Compresión local de fotos antes de guardarlas/encolarlas.

Una foto de celular (4-12 MB) se reduce a ~150-400 KB: lado mayor 1600 px,
JPEG calidad 70, orientación EXIF aplicada y metadatos EXIF eliminados
(incluyen la ubicación exacta del teléfono, que ya viaja en el reporte).
"""

from __future__ import annotations

import io
import logging
import shutil
from pathlib import Path
from typing import Optional

log = logging.getLogger(__name__)


def compress_photo(
    dest_dir: Path,
    name: str,
    src_path: Optional[str] = None,
    src_bytes: Optional[bytes] = None,
    max_side: int = 1600,
    quality: int = 70,
) -> Path:
    """Comprime una imagen (desde ruta o bytes) y la guarda como ``<name>.jpg``.

    Si Pillow no está disponible en la plataforma, copia el archivo original
    para no perder la evidencia.
    """
    if src_path is None and src_bytes is None:
        raise ValueError("Se requiere src_path o src_bytes")
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / f"{name}.jpg"

    try:
        from PIL import Image, ImageOps
    except ImportError:
        log.warning("Pillow no disponible: se guarda la foto sin comprimir")
        if src_bytes is not None:
            dest.write_bytes(src_bytes)
        else:
            shutil.copyfile(src_path, dest)
        return dest

    src = io.BytesIO(src_bytes) if src_bytes is not None else src_path
    with Image.open(src) as img:
        img = ImageOps.exif_transpose(img)
        if img.mode not in ("RGB", "L"):
            img = img.convert("RGB")
        img.thumbnail((max_side, max_side), Image.Resampling.LANCZOS)
        img.save(dest, format="JPEG", quality=quality, optimize=True, progressive=True)
    return dest


def make_thumbnail(path: Path, max_side: int = 320, quality: int = 60) -> bytes:
    """Miniatura en memoria para la vista previa del formulario."""
    try:
        from PIL import Image
    except ImportError:
        return path.read_bytes()
    with Image.open(path) as img:
        img.thumbnail((max_side, max_side))
        out = io.BytesIO()
        img.convert("RGB").save(out, format="JPEG", quality=quality)
        return out.getvalue()
