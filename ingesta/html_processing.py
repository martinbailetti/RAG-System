"""Procesamiento de archivos HTML: tablas, imágenes y extracción de texto para ingesta."""

from __future__ import annotations

import base64
import os
import re
from pathlib import Path
from urllib.parse import urlsplit
import requests
import time

from .image_cache import get_cached_image, save_cached_image
from typing import List, Optional

from bs4 import BeautifulSoup
from langchain_core.documents import Document

from .image_descriptions import describe_image
from .utils import table_to_markdown, normalize_source_reference

# Umbrales para descartar logos, iconos y decoraciones:
# - Menos de MIN_IMAGE_BYTES bytes: muy probablemente un icono o pixel de tracking.
# - Atributos width o height del tag <img> por debajo de MIN_IMAGE_DIMENSION px.
MIN_IMAGE_BYTES = 2048        # 2 KB
MIN_IMAGE_DIMENSION = 100     # px en cualquier dimensión

try:
    from .config import PROJECT_ROOT, IA_SERVICE
except Exception:
    PROJECT_ROOT = None
    IA_SERVICE = "unknown"


def _read_image_bytes(src: str, base_path: Path) -> tuple[Optional[bytes], Optional[str]]:
    """Lee bytes de imagen y devuelve el origen (data_uri/local/remote_cache/remote_download)."""
    if not src:
        return None, None
    src = src.strip()
    # Data URI
    if src.startswith("data:") and "base64," in src:
        try:
            b64 = src.split("base64,", 1)[1]
            return base64.b64decode(b64), "data_uri"
        except Exception:
            return None, None

    # URL remota: intentar descargar con requests (retries, timeout)
    if src.startswith("//"):
        # protocolo relativo -> asumir https
        src = "https:" + src
    if src.startswith("http://") or src.startswith("https://"):
        max_attempts = 3
        timeout = 8
        backoff = 1.0
        max_bytes = 5 * 1024 * 1024  # 5 MB safety limit
        headers = {"User-Agent": "ingesta/1.0 (+https://example)"}
        # comprobar cache local primero
        cached = get_cached_image(src)
        if cached:
            return cached, "remote_cache"

        for attempt in range(1, max_attempts + 1):
            try:
                resp = requests.get(src, stream=True, timeout=timeout, headers=headers)
                if resp.status_code != 200:
                    # no es fatal, reintentar
                    time.sleep(backoff)
                    backoff *= 2
                    continue
                # leer hasta max_bytes
                chunks = []
                total = 0
                for chunk in resp.iter_content(8192):
                    if not chunk:
                        break
                    total += len(chunk)
                    if total > max_bytes:
                        # demasiado grande, abortar
                        resp.close()
                        return None, None
                    chunks.append(chunk)
                data = b"".join(chunks)
                # guardar en caché
                try:
                    save_cached_image(src, data)
                except Exception:
                    pass
                return data, "remote_download"
            except requests.RequestException:
                time.sleep(backoff)
                backoff *= 2
                continue
        return None, None

    # Quitar query strings o fragmentos (?v=1 or #fragment)
    try:
        parsed = urlsplit(src)
        clean_path = parsed.path
    except Exception:
        clean_path = src

    candidates = []
    # Ruta tal cual relativa al html
    candidates.append(base_path / clean_path)
    # Si empieza con '/', también intentar relativo al proyecto raíz
    if clean_path.startswith("/"):
        if PROJECT_ROOT:
            candidates.append(Path(PROJECT_ROOT) / clean_path.lstrip("/"))
        # intentar también sin el slash relativo al base_path
        candidates.append(base_path / clean_path.lstrip("/"))
    # También probar con ./ prefijo removido
    if clean_path.startswith("./"):
        candidates.append(base_path / clean_path[2:])

    # Normalizar y probar
    for cand in candidates:
        try:
            candidate = cand.resolve()
        except Exception:
            candidate = Path(cand)
        if not candidate.exists():
            continue
        try:
            with open(candidate, "rb") as fh:
                return fh.read(), "local"
        except Exception:
            continue

    return None, None


def _reorder_title_to_front(enriched: str, soup: BeautifulSoup) -> str:
    """Mueve el bloque de título + intro al inicio del texto si el DOM lo dejó al final.

    Muchos HTMLs generados por herramientas (ej. editores WYSIWYG, CMS) colocan
    la cabecera visual (título, metadatos, párrafo introductorio) en un ``<div>``
    que aparece **después** del contenido en el DOM, usando CSS para posicionarlo
    visualmente al principio.  ``soup.stripped_strings`` recorre en orden DOM, así
    que el título queda al final del texto extraído.

    Estrategia:
    1. Obtener el título del documento desde ``<title>`` o ``<h1>``.
    2. Encontrar la **última** ocurrencia del título en el texto (la del ``<h1>``
       dentro del contenido, no la del ``<title>`` en ``<head>``).
    3. Si está lejos del inicio (>33% del texto), extraer desde esa posición hasta
       el final del documento y moverlo al frente.
    """
    if not enriched or len(enriched) < 200:
        return enriched

    # 1. Obtener el título del documento desde <title> o <h1>
    title_text = ""
    title_tag = soup.find("title")
    if title_tag and title_tag.string:
        title_text = title_tag.string.strip()
    if not title_text:
        h1 = soup.find("h1")
        if h1:
            title_text = h1.get_text(" ", strip=True)
    if not title_text or len(title_text) < 5:
        return enriched  # Sin título detectable

    # 2. Buscar la ÚLTIMA ocurrencia del título en el texto
    #    La primera puede ser del <title> tag (en <head>); la última es del <h1>
    #    que está en el cuerpo del documento.
    last_pos = enriched.rfind(title_text)
    if last_pos < 0:
        # Intentar con match parcial (primeras 40 chars del título)
        short_title = title_text[:40]
        last_pos = enriched.rfind(short_title)
        if last_pos < 0:
            return enriched

    # 3. Si ya está cerca del inicio (< 33% del texto), no reordenar
    threshold = len(enriched) // 3
    if last_pos < threshold:
        return enriched

    # 4. Determinar el inicio del bloque: retroceder al \n\n anterior
    block_start = enriched.rfind("\n\n", 0, last_pos)
    if block_start < 0:
        block_start = 0
    else:
        block_start += 2  # Saltar el \n\n

    # 5. El bloque va desde block_start hasta el final del documento.
    #    En estos HTMLs, el encabezado + intro + primeros pasos están todos
    #    al final del DOM, así que tomamos todo lo que queda.
    intro_block = enriched[block_start:].strip()
    if not intro_block or len(intro_block) < 50:
        return enriched

    # 6. Construir el texto reordenado: intro primero, resto después
    remaining = enriched[:block_start].strip()
    if not remaining:
        return enriched  # No había nada antes → texto ya estaba bien

    reordered = intro_block + "\n\n" + remaining
    return reordered


def process_html_for_chunks(
    html_path: str,
    gemini_client: Optional[object] = None,
    extra_metadata: Optional[dict] = None,
) -> List[Document]:
    """Procesa un archivo HTML y devuelve fragmentos enriquecidos como Document.

    - Convierte tablas <table> a Markdown y las marca como BLOQUE DELIMITADO (inicio/fin).
    - Describe imágenes <img> (si los ficheros referenciados están locales o data URIs).
    - Extrae el texto restante para paginar en chunks (similar a los PDF).
    """
    html_file = Path(html_path)
    try:
        text = html_file.read_text(encoding="utf-8")
    except Exception:
        try:
            text = html_file.read_text(encoding="latin-1")
        except Exception:
            print(f"ERROR: No se pudo leer HTML {html_path}")
            return []

    soup = BeautifulSoup(text, "html.parser")

    # Eliminar scripts y estilos inline para que su contenido no contamine
    # los chunks (JavaScript/CSS no son texto técnico indexable).
    for tag in soup.find_all(["script", "style", "noscript"]):
        tag.decompose()

    # Descripciones de imágenes
    imgs = soup.find_all("img")
    if imgs:
        print(f"  • HTML '{html_file.name}': {len(imgs)} imagen(es) detectada(s).")
    for idx, img in enumerate(imgs, start=1):
        src = img.get("src") or ""
        if src.startswith("data:") and "base64," in src:
            header, b64_payload = src.split("base64,", 1)
            preview = b64_payload[:20]
            display_src = f"{header}base64,{preview}..."
        else:
            display_src = src
        print(f"    - Imagen {idx}: src='{display_src}'")
        image_bytes, origin = _read_image_bytes(src, html_file.parent)
        if not image_bytes:
            placeholder = (
                f"[IMAGEN EN HTML '{html_file.name}' (img {idx})]: no se pudo leer o descargar la imagen."
            )
            img.replace_with(placeholder)
            print("      -> No se pudo leer la imagen; marcador insertado.")
            continue

        # Filtrar imágenes demasiado pequeñas (logos, iconos, píxeles de tracking).
        if len(image_bytes) < MIN_IMAGE_BYTES:
            img.decompose()
            print(f"      -> Imagen omitida: {len(image_bytes)} bytes < {MIN_IMAGE_BYTES} B (logo/icono).")
            continue
        # Comprobar atributos HTML de dimensión (si existen y son numéricos).
        def _attr_px(val: str | None) -> int | None:
            try:
                v = int(str(val).strip().rstrip("px"))
                return v if v > 0 else None
            except Exception:
                return None
        attr_w = _attr_px(img.get("width"))
        attr_h = _attr_px(img.get("height"))
        if (attr_w is not None and attr_w < MIN_IMAGE_DIMENSION) or \
           (attr_h is not None and attr_h < MIN_IMAGE_DIMENSION):
            img.decompose()
            print(f"      -> Imagen omitida: dimensión HTML {attr_w}x{attr_h}px < {MIN_IMAGE_DIMENSION}px.")
            continue
        source_label = origin or "unknown"
        if source_label == "data_uri":
            source_label = "data URI"
        elif source_label == "local":
            source_label = "archivo local"
        elif source_label == "remote_cache":
            source_label = "remota (cache)"
        elif source_label == "remote_download":
            source_label = "remota (descargada)"
        print(f"      -> Origen: {source_label}")
        mime = "image/png"
        # intentar deducir por extensión
        _, ext = os.path.splitext(src or "")
        if ext:
            ext = ext.lower().lstrip('.')
            mime = {
                "jpg": "image/jpeg",
                "jpeg": "image/jpeg",
                "png": "image/png",
                "gif": "image/gif",
                "bmp": "image/bmp",
                "tif": "image/tiff",
                "tiff": "image/tiff",
                "webp": "image/webp",
                "jp2": "image/jp2",
            }.get(ext, mime)
        print(f"    - Generando descripción con {IA_SERVICE.capitalize()} ({mime})...")
        b64 = base64.b64encode(image_bytes).decode("utf-8")
        desc = describe_image(b64, mime_type=mime, gemini_client=gemini_client)
        if desc:
            marker = f"[DESCRIPCIÓN DE IMAGEN (img {idx})]: {desc}]"
            print("      -> Descripción añadida al texto.")
        else:
            marker = (
                f"[IMAGEN (img {idx})]: sin descripción disponible."
            )
            print("      -> No se obtuvo descripción (API deshabilitada o respuesta vacía).")
        img.replace_with(marker)
    # Tablas: convertir cada <table> a markdown in-place (preserva orden DOM).
    # Antes se extraían a ``enriched`` ANTES del texto visible, provocando que el
    # título y la intro del documento quedaran al final del texto extraído.
    tables = soup.find_all("table")
    for t_idx, table in enumerate(tables, start=1):
        # Construir lista de filas
        rows = []
        for tr in table.find_all("tr"):
            cells = []
            # Buscar th o td
            for cell in tr.find_all(["th", "td"]):
                # texto interno limpio
                cells.append(cell.get_text(" ", strip=True))
            if cells:
                rows.append(cells)
        md = table_to_markdown(rows)
        if not md.strip():
            table.decompose()
            continue
        # Reemplazar la tabla por su markdown en la posición original del DOM.
        replacement = (
            f"\n[BLOQUE DELIMITADO {t_idx} INICIO]\n"
            f"{md}\n"
            f"[BLOQUE DELIMITADO {t_idx} FIN]\n"
        )
        table.replace_with(soup.new_string(replacement))

    # Texto completo en orden DOM: imágenes como marcadores, tablas como markdown.
    visible_texts = []
    for element in soup.stripped_strings:
        visible_texts.append(element)
    enriched = "\n\n".join(visible_texts)

    # --- Anteponer el <title> del HTML como primera línea ---
    title_tag = soup.find("title")
    if title_tag and title_tag.string and title_tag.string.strip():
        doc_title = title_tag.string.strip()
        enriched = doc_title + "\n\n" + enriched

    # --- Post-procesamiento: mover título/intro al inicio del texto ---
    # En muchos HTMLs generados por herramientas, el DOM coloca la cabecera
    # (título, autor, fecha, intro) DESPUÉS del contenido principal.  Para la
    # ingesta, el título + intro deben ir primero para que el chunking produzca
    # un primer fragmento coherente con la similitud vectorial.
    enriched = _reorder_title_to_front(enriched, soup)

    # Chunking: reusar el splitter usado en PDF (import local to avoid cycle)
    try:
        from langchain_text_splitters import RecursiveCharacterTextSplitter

        splitter = RecursiveCharacterTextSplitter(
            chunk_size=1500,
            chunk_overlap=300,
            # Priorizar cortes en límites de bloques de tabla para no partir
            # Markdown de tablas a mitad.
            separators=["\n[BLOQUE DELIMITADO", "\n\n", "\n", " ", ""],
        )
        metadata = {"source": normalize_source_reference(str(html_file.absolute()))}
        if extra_metadata:
            metadata.update(extra_metadata)
        document = Document(page_content=enriched, metadata=metadata)
        chunks = splitter.split_documents([document])
        # Añadir chunk_index para preservar el orden de lectura en /documentos/detalle
        for i, chunk in enumerate(chunks):
            chunk.metadata["chunk_index"] = i
        print(f"HTML {html_file.name} dividido en {len(chunks)} trozos enriquecidos.")
        return chunks
    except Exception:
        # Fallback: devolver todo en un Document
        metadata = {"source": normalize_source_reference(str(html_file.absolute()))}
        if extra_metadata:
            metadata.update(extra_metadata)
        return [Document(page_content=enriched, metadata=metadata)]
