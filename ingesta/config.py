"""Configuración común para la ingesta y sincronización de manuales."""

from __future__ import annotations

import os
from functools import lru_cache

from langchain_community.embeddings import HuggingFaceEmbeddings

from env_loader import load_host_env

PACKAGE_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_ROOT = os.path.dirname(PACKAGE_DIR)

load_host_env(PROJECT_ROOT)

DEFAULT_DOCUMENTS_DIR = "manuales"
_pdf_path = os.environ.get("DOCS_PATH") or DEFAULT_DOCUMENTS_DIR
DOCUMENTS_FOLDER = (
    _pdf_path
    if os.path.isabs(_pdf_path)
    else os.path.join(PROJECT_ROOT, _pdf_path)
)

_root_folder_raw = (os.environ.get("ROOT_FOLDER") or "").strip()
_root_folder_clean = _root_folder_raw.replace("/", os.sep).strip()
if _root_folder_clean:
    _root_folder_clean = _root_folder_clean.strip(os.sep)

if _root_folder_clean:
    _root_candidate_primary = os.path.normcase(_root_folder_clean)
    _root_candidate_secondary = os.path.basename(_root_candidate_primary)
    if _root_candidate_secondary == _root_candidate_primary:
        _root_folder_candidates = [_root_candidate_primary]
    else:
        _root_folder_candidates = [_root_candidate_primary, _root_candidate_secondary]
else:
    _root_folder_candidates = []

ROOT_FOLDER_CANDIDATES = tuple(dict.fromkeys(_root_folder_candidates))

DEFAULT_DB_DIR = "chroma_db_manuales"
_db_path = os.environ.get("DB_PATH", DEFAULT_DB_DIR)
DB_PATH = _db_path if os.path.isabs(_db_path) else os.path.join(PROJECT_ROOT, _db_path)

EMBEDDING_MODEL = "paraphrase-multilingual-mpnet-base-v2"
GEMINI_MODEL = os.environ.get("GEMINI_MODEL_QUERY", "gemini-2.5-flash")
GEMINI_MODEL_IMAGE = os.environ.get("GEMINI_MODEL_IMAGE", "gemini-2.0-flash")
IA_SERVICE = os.environ.get("IA_SERVICE", "gemini").lower()
OLLAMA_URL = os.environ.get("OLLAMA_URL", "http://localhost:11434")
OLLAMA_MODEL = os.environ.get("OLLAMA_MODEL", "llama2")

# Controla si se describen imágenes durante la ingesta.
# Por defecto True (comportamiento previo). Desactivar con:
#   RAG_INCLUDE_IMAGE_DESCRIPTION=false  en el .env
_rag_include_img_raw = os.environ.get("RAG_INCLUDE_IMAGE_DESCRIPTION", "true").strip().lower()
INCLUDE_IMAGE_DESCRIPTION: bool = _rag_include_img_raw not in {"0", "false", "no", "off"}


def ensure_documents_folder(folder: str | None = None) -> str:
    """Garantiza que la carpeta de documentos exista y devuelve su ruta."""
    target = folder or DOCUMENTS_FOLDER
    if not os.path.exists(target):
        os.makedirs(target, exist_ok=True)
    return target


@lru_cache(maxsize=1)
def init_embedding_model(device: str = "cpu") -> HuggingFaceEmbeddings:
    """Inicializa y memoriza el modelo de embeddings usado por Chroma."""
    return HuggingFaceEmbeddings(
        model_name=EMBEDDING_MODEL,
        model_kwargs={"device": device},
    )
