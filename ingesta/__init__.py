"""Utilidades de ingesta y sincronización de manuales en Chroma."""

import numpy as _np

if not hasattr(_np, "float_"):
    _np.float_ = _np.float64  # type: ignore[attr-defined]
if not hasattr(_np, "int_"):
    _np.int_ = _np.int64  # type: ignore[attr-defined]
if not hasattr(_np, "uint"):
    _np.uint = _np.uint64  # type: ignore[attr-defined]
if not hasattr(_np, "NaN"):
    _np.NaN = _np.nan  # type: ignore[attr-defined]

from .config import (
    DOCUMENTS_FOLDER,
    DB_PATH,
    EMBEDDING_MODEL,
    GEMINI_MODEL,
    IA_SERVICE,
    OLLAMA_MODEL,
    OLLAMA_URL,
    ensure_documents_folder,
    init_embedding_model,
)
from .pdf_processing import process_pdf_for_chunks
from .html_processing import process_html_for_chunks
from .chroma_sync import sincronizar_pdf_con_chroma, create_vectorstore
from .image_descriptions import init_gemini_client

__all__ = [
    "DOCUMENTS_FOLDER",
    "DB_PATH",
    "EMBEDDING_MODEL",
    "GEMINI_MODEL",
    "IA_SERVICE",
    "OLLAMA_MODEL",
    "OLLAMA_URL",
    "ensure_documents_folder",
    "init_embedding_model",
    "process_pdf_for_chunks",
    "process_html_for_chunks",
    "sincronizar_pdf_con_chroma",
    "create_vectorstore",
    "init_gemini_client",
]
