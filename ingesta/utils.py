"""Funciones de utilidad compartidas por el proceso de ingesta."""

from __future__ import annotations

import base64
import hashlib
import os
import re
import unicodedata
from collections import defaultdict
from typing import Any, Dict, Iterable, List, Optional, Tuple, Set

import fitz

from .config import PROJECT_ROOT, ROOT_FOLDER_CANDIDATES


def norm_path(path: str) -> str:
    """Devuelve la ruta absoluta normalizada (case insensitive en Windows)."""
    return os.path.normcase(os.path.abspath(path))


def norm_project_path(path: str) -> str:
    """Normaliza una ruta relativa al proyecto."""
    return norm_path(os.path.join(PROJECT_ROOT, path))


def _trim_root_anchor(path: str) -> str | None:
    """Elimina la parte anterior a ROOT_FOLDER, si se encuentra en la ruta."""
    if not ROOT_FOLDER_CANDIDATES:
        return None

    normalized = path.replace("/", os.sep)
    for candidate in ROOT_FOLDER_CANDIDATES:
        idx = normalized.find(candidate)
        while idx != -1:
            before_ok = idx == 0 or normalized[idx - 1] in {os.sep}
            after_idx = idx + len(candidate)
            after_ok = after_idx == len(normalized) or normalized[after_idx] in {os.sep}
            if before_ok and after_ok:
                remainder = normalized[after_idx:]
                return remainder.lstrip(f"{os.sep}")
            idx = normalized.find(candidate, idx + 1)
    return None


def normalize_source_reference(path: str) -> str:
    """Normaliza la ruta usada como identificador de fuente dentro de Chroma."""
    if not path:
        return ""

    candidate = norm_path(path) if os.path.isabs(path) else os.path.normcase(os.path.normpath(path))
    trimmed = _trim_root_anchor(candidate)
    if trimmed is None:
        return candidate
    return trimmed or ""


def strip_accents(value: str) -> str:
    """Elimina acentos de la cadena usando normalización Unicode."""
    normalized = unicodedata.normalize("NFKD", value)
    return "".join(ch for ch in normalized if not unicodedata.combining(ch))


def file_fingerprint(path: str) -> Dict[str, str | int | float]:
    """Calcula hash sha1, fecha de modificación y tamaño para detección de cambios."""
    stat = os.stat(path)
    sha1 = hashlib.sha1()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            sha1.update(chunk)
    return {
        "source": normalize_source_reference(path),
        "source_hash": sha1.hexdigest(),
        "source_mtime": stat.st_mtime,
        "source_size": stat.st_size,
    }


def list_pdfs_recursive(folder_path: str) -> List[str]:
    """Lista todos los PDFs dentro de una carpeta (recursivo)."""
    pdfs: List[str] = []
    for root, _, files in os.walk(folder_path, followlinks=True):
        for fn in files:
            if fn.lower().endswith(".pdf"):
                pdfs.append(os.path.join(root, fn))
    return pdfs


def list_documents_recursive(folder_path: str, exts: Optional[List[str]] = None) -> List[str]:
    """Lista documentos según extensiones (por defecto .pdf, .html y .md)."""
    if exts is None:
        exts = [".pdf", ".html", ".md"]
    exts = [e.lower() for e in exts]
    # Carpetas a omitir durante el recorrido
    skip_dirs = {".svn", ".git", "__pycache__"}
    docs: List[str] = []
    for root, dirs, files in os.walk(folder_path, followlinks=True):
        # Modificar dirs in-place para que os.walk no entre en carpetas omitidas
        dirs[:] = [d for d in dirs if d not in skip_dirs]
        for fn in files:
            for e in exts:
                if fn.lower().endswith(e):
                    docs.append(os.path.join(root, fn))
                    break
    return docs


def get_chroma_collection(vectorstore) -> Any:
    """Accede a la colección interna de Chroma si está disponible."""
    return getattr(vectorstore, "_collection", None)


def sanitize_table_row(row: List[str], width: int) -> List[str]:
    """Normaliza filas de tablas eliminando saltos y ajustando longitud."""
    cleaned = [(cell or "").replace("\n", " ").strip() for cell in row]
    if len(cleaned) < width:
        cleaned.extend(["" for _ in range(width - len(cleaned))])
    elif len(cleaned) > width:
        cleaned = cleaned[:width]
    return cleaned


def table_to_markdown(table_data: List[List[str]]) -> str:
    """Convierte la tabla extraída por PyMuPDF a markdown sencillo."""
    if not table_data:
        return ""
    width = max((len(row) for row in table_data if row), default=0)
    if width == 0:
        return ""

    rows = [sanitize_table_row(row, width) for row in table_data if row]
    if not rows:
        return ""

    header = rows[0]
    if not any(header):
        header = [f"Columna {idx + 1}" for idx in range(width)]
    data_rows = [row for row in rows[1:] if any(row)]

    separator = " | ".join(["---"] * width)
    markdown_lines = [" | ".join(header), separator]
    if data_rows:
        markdown_lines.extend(" | ".join(row) for row in data_rows)
    else:
        markdown_lines.append(" | ".join(["" for _ in range(width)]))
    return "\n".join(markdown_lines)


def rect_overlap_ratio(rect_a: fitz.Rect, rect_b: fitz.Rect) -> float:
    """Calcula el solapamiento horizontal relativo entre dos rectángulos."""
    overlap = max(0.0, min(rect_a.x1, rect_b.x1) - max(rect_a.x0, rect_b.x0))
    min_width = min(rect_a.width, rect_b.width) or 1.0
    return overlap / min_width


def rect_contains(outer: fitz.Rect, inner: fitz.Rect, tol: float = 1.0) -> bool:
    """Indica si inner está contenido en outer con cierta tolerancia."""
    return (
        inner.x0 >= outer.x0 - tol
        and inner.y0 >= outer.y0 - tol
        and inner.x1 <= outer.x1 + tol
        and inner.y1 <= outer.y1 + tol
    )


def should_merge_table(prev_rect: fitz.Rect, next_rect: fitz.Rect) -> bool:
    """Determina si dos tablas forman parte del mismo bloque con bordes."""
    if prev_rect.intersects(next_rect):
        return True

    overlap_ratio = rect_overlap_ratio(prev_rect, next_rect)
    vertical_gap = max(0.0, next_rect.y0 - prev_rect.y1)

    if overlap_ratio >= 0.35 and vertical_gap <= 25:
        return True

    if vertical_gap <= 25 and next_rect.x0 >= prev_rect.x0 - 6 and next_rect.x1 <= prev_rect.x1 + 6:
        return True

    return False


def encode_image(image_bytes: bytes) -> str:
    """Codifica bytes de imagen a base64."""
    return base64.b64encode(image_bytes).decode("utf-8")


def iter_collection_entries(collection, batch_size: int = 1000) -> Iterable[Tuple[str, Dict[str, Any]]]:
    """Itera la colección de Chroma devolviendo pares (id, metadata)."""
    if collection is None:
        return []
    offset = 0
    while True:
        try:
            res = collection.get(include=["metadatas"], limit=batch_size, offset=offset)
        except TypeError:
            res = collection.get(include=["metadatas"])  # type: ignore
        metadatas = res.get("metadatas") or []
        ids = res.get("ids") or []
        if not metadatas:
            break
        for doc_id, metadata in zip(ids, metadatas):
            if not metadata or doc_id is None:
                continue
            yield str(doc_id), metadata
        if len(metadatas) < batch_size:
            break
        offset += batch_size


def indexed_source_info(collection) -> Dict[str, Dict[str, Any]]:
    """Devuelve mapa ruta-normalizada -> info (hash + ids) desde la colección."""
    info: Dict[str, Dict[str, Any]] = {}
    for doc_id, metadata in iter_collection_entries(collection):
        source = metadata.get("source")
        if not source:
            continue
        norm = normalize_source_reference(source)
        entry = info.setdefault(
            norm,
            {"source": source, "hash": metadata.get("source_hash"), "ids": []},
        )
        entry.setdefault("ids", []).append(doc_id)
    return info


def _split_camel_case(text: str) -> List[str]:
    """Divide texto CamelCase en palabras individuales.

    Ejemplos::

        'SmartPayout' → ['Smart', 'Payout']
        'SmartHopper' → ['Smart', 'Hopper']
        'smartpayout' → ['smartpayout']  # sin CamelCase no se divide
    """
    parts = re.findall(r"[A-Z][a-z0-9]*|[a-z][a-z0-9]*|[0-9]+", text)
    return parts if parts else [text]


def build_source_token_index(collection, min_token_length: int = 3) -> Dict[str, Set[str]]:
    """Genera índice de tokens provenientes de rutas para heurísticas de búsqueda.

    Además de dividir por separadores estándar, aplica split CamelCase a cada
    fragmento para que, p. ej., ``'SmartPayout.pdf'`` indexe tanto
    ``'smartpayout'`` como ``'smart'`` y ``'payout'`` de forma independiente.
    Esto permite encontrar el documento cuando el usuario escribe las palabras
    por separado (``'smart payout'``).
    """
    if collection is None:
        return {}

    index: Dict[str, Set[str]] = defaultdict(set)
    for _, metadata in iter_collection_entries(collection):
        source = metadata.get("source")
        if not source:
            continue
        normalized_source = normalize_source_reference(source)
        segments = re.split(r"[\\/]+", normalized_source)
        tokens: Set[str] = set()
        for segment in segments:
            for raw_part in re.split(r"[^a-zA-Z0-9áéíóúüñÁÉÍÓÚÜÑ]+", segment):
                if not raw_part:
                    continue
                # Token completo (en minúsculas)
                full_token = strip_accents(raw_part.lower().strip())
                if len(full_token) >= min_token_length:
                    tokens.add(full_token)
                # Sub-tokens CamelCase: 'SmartPayout' → 'smart', 'payout'
                camel_parts = _split_camel_case(raw_part)
                if len(camel_parts) > 1:
                    for part in camel_parts:
                        sub_token = strip_accents(part.lower().strip())
                        if len(sub_token) >= min_token_length:
                            tokens.add(sub_token)
        if not tokens:
            continue
        for token in tokens:
            index[token].add(normalized_source)

    return index
