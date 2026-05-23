"""Procesamiento de archivos Markdown para ingesta en Chroma."""

from __future__ import annotations

import os
from typing import List, Optional

from langchain_core.documents import Document
from langchain_text_splitters import RecursiveCharacterTextSplitter

from .utils import normalize_source_reference


def process_md_for_chunks(
    md_path: str,
    extra_metadata: Optional[dict] = None,
) -> List[Document]:
    """Lee un archivo Markdown y devuelve fragmentos listos para indexar."""
    filename_no_ext = os.path.splitext(os.path.basename(md_path))[0]
    print(f"\nProcesando archivo Markdown: {os.path.basename(md_path)}")

    try:
        with open(md_path, "r", encoding="utf-8") as f:
            content = f.read()
    except Exception:
        print(f"ERROR: No se pudo leer el archivo Markdown en {md_path}.")
        return []

    if not content.strip():
        print("  Archivo vacío, omitido.")
        return []

    # Anteponer el nombre del archivo como contexto, igual que PDF y HTML.
    enriched_text = filename_no_ext + "\n\n" + content

    text_splitter = RecursiveCharacterTextSplitter(
        chunk_size=1500,
        chunk_overlap=300,
        separators=["\n## ", "\n### ", "\n#### ", "\n\n", "\n", " ", ""],
    )

    metadata = {"source": normalize_source_reference(md_path)}
    if extra_metadata:
        metadata.update(extra_metadata)

    document = Document(page_content=enriched_text, metadata=metadata)
    chunks = text_splitter.split_documents([document])

    for i, chunk in enumerate(chunks):
        chunk.metadata["chunk_index"] = i

    print(f"Archivo dividido en {len(chunks)} trozos.")
    return chunks
