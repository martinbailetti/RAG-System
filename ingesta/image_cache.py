"""Caché simple para imágenes remotas descargadas por el procesador HTML.

Usa un directorio de caché en el proyecto y nombra archivos por SHA256 de la URL
conservando la extensión cuando sea posible.
"""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Optional
from urllib.parse import urlsplit

try:
    from .config import PROJECT_ROOT
except Exception:
    PROJECT_ROOT = None


def _cache_dir() -> Path:
    base = Path(PROJECT_ROOT) if PROJECT_ROOT else Path(__file__).resolve().parent
    d = base / ".ingesta_image_cache"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _cache_name_for_url(url: str) -> str:
    h = hashlib.sha256(url.encode("utf-8")).hexdigest()
    path = urlsplit(url).path or ""
    ext = Path(path).suffix
    if ext and len(ext) <= 6:
        return f"{h}{ext}"
    return h


def get_cached_image(url: str) -> Optional[bytes]:
    """Devuelve bytes si la URL ya está en caché, o None si no."""
    name = _cache_name_for_url(url)
    p = _cache_dir() / name
    try:
        if p.exists():
            return p.read_bytes()
    except Exception:
        return None
    return None


def save_cached_image(url: str, data: bytes) -> Optional[Path]:
    """Guarda los bytes en caché y devuelve la ruta del fichero (o None si falla)."""
    name = _cache_name_for_url(url)
    p = _cache_dir() / name
    try:
        p.write_bytes(data)
        return p
    except Exception:
        return None
