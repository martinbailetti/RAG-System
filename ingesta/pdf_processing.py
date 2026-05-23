"""Lógica de procesamiento de PDFs para generar fragmentos enriquecidos."""

from __future__ import annotations

import os
import re
from typing import List, Optional, Tuple

# Marcador interno usado para rastrear número de página por chunk.
# Formato chosen para ser inequívoco y fácil de extraer/limpiar.
_RE_PAGE_MARKER = re.compile(r"\[__PÁG(\d+)__\]", re.IGNORECASE)

# Dimensión mínima (píxeles) que debe tener una imagen para ser descrita.
# Imágenes más pequeñas en cualquier dimensión se consideran logos, iconos
# o decoraciones y se omiten para no contaminar el texto indexado.
MIN_IMAGE_DIMENSION = 100

import fitz
from langchain_core.documents import Document
from langchain_text_splitters import RecursiveCharacterTextSplitter

from .config import IA_SERVICE
from .image_descriptions import describe_image
from .utils import (
    encode_image,
    normalize_source_reference,
    rect_overlap_ratio,
    should_merge_table,
    table_to_markdown,
)


_TEXT_EXTRACTION_FLAGS = 0
for _flag_name in ("TEXT_PRESERVE_WHITESPACE", "TEXT_PRESERVE_LIGATURES"):
    _TEXT_EXTRACTION_FLAGS |= getattr(fitz, _flag_name, 0)


def _extract_bordered_text(page: fitz.Page) -> List[str]:
    """Agrupa tablas contiguas detectadas por PyMuPDF en bloques con bordes."""
    table_finder = page.find_tables()
    if not table_finder or not getattr(table_finder, "tables", None):
        return []

    entries: List[Tuple[fitz.Rect, str]] = []
    for table in table_finder.tables:
        rect = fitz.Rect(table.bbox)
        markdown = table_to_markdown(table.extract())
        if not markdown.strip():
            continue
        entries.append((rect, markdown))

    if not entries:
        return []

    entries.sort(key=lambda item: (item[0].y0, item[0].x0))

    grouped_markdowns: List[str] = []
    current_rect: Optional[fitz.Rect] = None
    current_chunks: List[str] = []

    for rect, markdown in entries:
        if current_rect is None:
            current_rect = rect
            current_chunks = [markdown]
            continue

        if should_merge_table(current_rect, rect):
            current_rect = current_rect | rect
            current_chunks.append(markdown)
            continue

        upper_md = markdown.upper()
        vertical_gap = max(0.0, rect.y0 - current_rect.y1)
        if "RECOMEND" in upper_md and vertical_gap <= 80 and rect_overlap_ratio(current_rect, rect) >= 0.3:
            current_rect = current_rect | rect
            current_chunks.append(markdown)
        else:
            grouped_markdowns.append("\n\n".join(current_chunks))
            current_rect = rect
            current_chunks = [markdown]

    if current_chunks:
        grouped_markdowns.append("\n\n".join(current_chunks))

    return grouped_markdowns


def process_pdf_for_chunks(
    pdf_path: str,
    gemini_client: Optional[object],
    extra_metadata: Optional[dict] = None,
) -> List[Document]:
    """Procesa un PDF y devuelve los fragmentos enriquecidos listos para indexar."""
    # Anteponer el nombre del archivo como primera línea, igual que HTML hace con <title>.
    filename_no_ext = os.path.splitext(os.path.basename(pdf_path))[0]
    enriched_text = filename_no_ext + "\n\n"
    print(f"\nProcesando archivo: {os.path.basename(pdf_path)}")

    try:
        doc = fitz.open(pdf_path)
    except Exception:
        print(f"ERROR: No se pudo abrir el archivo PDF en {pdf_path}.")
        return []

    try:
        for page_index in range(len(doc)):
            page = doc.load_page(page_index)
            page_number = page_index + 1

            # Marcador de página: se extrae después del chunking para fijar
            # page_number en los metadatos y se elimina del page_content.
            enriched_text += f"\n[__PÁG{page_number}__]\n"

            try:
                images = page.get_images(full=True)
                if images:
                    print(f"  • Página {page_number}: {len(images)} imagen(es) detectada(s).")
                for pix in images:
                    xref = pix[0]
                    img_w = pix[2]  # width en píxeles
                    img_h = pix[3]  # height en píxeles
                    if img_w < MIN_IMAGE_DIMENSION or img_h < MIN_IMAGE_DIMENSION:
                        print(f"    - Imagen {img_w}x{img_h}px omitida (demasiado pequeña, umbral {MIN_IMAGE_DIMENSION}px).")
                        continue
                    base_image = doc.extract_image(xref)
                    image_bytes = base_image.get("image")
                    if not image_bytes:
                        continue
                    ext = (base_image.get("ext") or "").lower()
                    mime_type = {
                        "jpg": "image/jpeg",
                        "jpeg": "image/jpeg",
                        "png": "image/png",
                        "gif": "image/gif",
                        "bmp": "image/bmp",
                        "tif": "image/tiff",
                        "tiff": "image/tiff",
                        "webp": "image/webp",
                        "jp2": "image/jp2",
                    }.get(ext, f"image/{ext}" if ext else "image/png")
                    print(f"    - Generando descripción con {IA_SERVICE.capitalize()} ({mime_type})...")
                    image_b64 = encode_image(image_bytes)
                    description = describe_image(image_b64, mime_type=mime_type, gemini_client=gemini_client)
                    if description:
                        enriched_text += (
                            f"\n[DESCRIPCIÓN DE IMAGEN (pág. {page_number})]: {description}]\n"
                        )
                        print("      -> Descripción añadida al texto.")
                    else:
                        print("      -> No se obtuvo descripción (API deshabilitada o respuesta vacía).")
            except Exception:
                pass

            bordered_groups = _extract_bordered_text(page)
            for block_index, group_text in enumerate(bordered_groups, start=1):
                enriched_text += (
                    f"\n[BLOQUE DELIMITADO {block_index} INICIO PÁGINA {page_number}]\n"
                    f"{group_text}\n"
                    f"[BLOQUE DELIMITADO {block_index} FIN PÁGINA {page_number}]\n"
                )

            try:
                page_text = page.get_text("text", flags=_TEXT_EXTRACTION_FLAGS) or ""
                enriched_text += page_text
            except Exception:
                pass
    finally:
        doc.close()

    text_splitter = RecursiveCharacterTextSplitter(
        chunk_size=1500,
        chunk_overlap=300,
        # Priorizar cortes en límites de bloques de tabla para no partir
        # Markdown de tablas a mitad. El splitter intenta estas separaciones
        # en orden: primero ante un BLOQUE DELIMITADO, luego párrafo, línea...
        separators=["\n[BLOQUE DELIMITADO", "\n\n", "\n", " ", ""],
    )

    metadata = {"source": normalize_source_reference(pdf_path)}
    if extra_metadata:
        metadata.update(extra_metadata)

    document = Document(page_content=enriched_text, metadata=metadata)
    chunks = text_splitter.split_documents([document])

    # Extraer page_number del último marcador visible en cada chunk,
    # limpiar los marcadores del page_content y añadir chunk_index.
    for i, chunk in enumerate(chunks):
        page_matches = _RE_PAGE_MARKER.findall(chunk.page_content)
        if page_matches:
            chunk.metadata["page_number"] = int(page_matches[-1])
        chunk.page_content = _RE_PAGE_MARKER.sub("", chunk.page_content).strip()
        chunk.metadata["chunk_index"] = i

    print(f"Archivo dividido en {len(chunks)} trozos enriquecidos.")
    return chunks
