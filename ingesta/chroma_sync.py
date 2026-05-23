"""Sincronización de PDFs con Chroma, separada de la lógica del script original."""

from __future__ import annotations

import os
from typing import Dict, List, Optional

MAX_CHROMA_BATCH = 150  # Límite seguro (<166) para las inserciones en Chroma

from langchain_chroma import Chroma

from .config import DB_PATH, IA_SERVICE, INCLUDE_IMAGE_DESCRIPTION, ensure_documents_folder, init_embedding_model
from .image_descriptions import init_gemini_client
from .pdf_processing import process_pdf_for_chunks
from .html_processing import process_html_for_chunks
from .md_processing import process_md_for_chunks
from .utils import (
    file_fingerprint,
    get_chroma_collection,
    indexed_source_info,
    list_documents_recursive,
    normalize_source_reference,
)


def create_vectorstore(persist_directory: Optional[str] = None, embedding_model=None) -> Chroma:
    """Crea una instancia de Chroma usando la ruta persistida y embedding suministrado."""
    persist_dir = persist_directory or DB_PATH
    embeddings = embedding_model or init_embedding_model()
    return Chroma(persist_directory=persist_dir, embedding_function=embeddings)


def _delete_previous_entries(vectorstore: Chroma, stored_source: str | None, ids: Optional[List[str]] = None) -> None:
    """Elimina los fragmentos previos asociados a un documento en batches seguros.

    Chroma rechaza operaciones delete con más de 166 IDs a la vez.
    Si se pasan IDs directamente se usan; si no, se obtienen primero via get()
    filtrando por source para luego borrar en batches de MAX_CHROMA_BATCH.
    """
    try:
        delete_ids: List[str] = []
        if ids:
            delete_ids = list(ids)
        elif stored_source:
            # Obtener todos los IDs del documento antes de borrar
            result = vectorstore.get(where={"source": stored_source}, include=[])
            delete_ids = result.get("ids") or []

        if not delete_ids:
            return

        # Borrar en batches para respetar el límite de Chroma (≤166 por llamada)
        for i in range(0, len(delete_ids), MAX_CHROMA_BATCH):
            batch = delete_ids[i : i + MAX_CHROMA_BATCH]
            vectorstore.delete(ids=batch)

        if len(delete_ids) > MAX_CHROMA_BATCH:
            print(f"    -> {len(delete_ids)} fragmentos borrados en {-(-len(delete_ids) // MAX_CHROMA_BATCH)} batches.")
    except Exception as exc:
        print(f"No se pudo borrar registros para {stored_source}: {exc}")


def sincronizar_pdf_con_chroma(folder_path: Optional[str] = None) -> dict:
    """Sincroniza los PDFs del disco con el almacenamiento persistente de Chroma.

    Retorna un dict con dos listas:
    {
        "procesados": [{"nombre": "archivo.pdf", "ruta": "Carpeta/archivo.pdf"}, ...],
        "eliminados": [{"nombre": "otro.pdf", "ruta": "Carpeta/otro.pdf"}, ...],
    }
    """
    folder = ensure_documents_folder(folder_path)

    gemini_client = None
    if INCLUDE_IMAGE_DESCRIPTION and IA_SERVICE != "ollama":
        gemini_client = init_gemini_client()
    elif not INCLUDE_IMAGE_DESCRIPTION:
        print("[Ingesta] RAG_INCLUDE_IMAGE_DESCRIPTION=false — descripción de imágenes desactivada.")

    print("Inicializando modelo de embeddings...")
    embedding_model = init_embedding_model()

    vectorstore = create_vectorstore(embedding_model=embedding_model)
    collection = get_chroma_collection(vectorstore)

    all_files = list_documents_recursive(folder)
    visible_files: List[str] = []
    hidden_blocked: List[str] = []
    for path in all_files:
        hidden_path = f"{path}.hidden"
        if os.path.exists(hidden_path):
            print(
                "Omitiendo archivo visible y marcando para eliminación en Chroma:",
                os.path.basename(path),
            )
            hidden_blocked.append(path)
            continue
        visible_files.append(path)

    current_fingerprints: Dict[str, dict] = {path: file_fingerprint(path) for path in visible_files}
    current_norm_set = {fp["source"] for fp in current_fingerprints.values()}
    print(
        "Se encontraron",
        len(visible_files),
        "documentos visibles (PDF/HTML) tras descartar pares ocultos.",
    )

    indexed = indexed_source_info(collection)

    deleted_sources: List[str] = []  # rutas normalizadas de docs eliminados

    for blocked_path in hidden_blocked:
        norm = normalize_source_reference(blocked_path)
        entry = indexed.pop(norm, None)
        if not entry:
            continue
        print("  -> Eliminando entradas previas de Chroma por archivo oculto:", entry.get("source"))
        _delete_previous_entries(vectorstore, entry.get("source"), entry.get("ids"))
        deleted_sources.append(entry.get("source", norm))

    indexed_norm_set = set(indexed.keys())

    removed_norm = indexed_norm_set - current_norm_set
    if removed_norm:
        print(f"Eliminando {len(removed_norm)} PDFs inexistentes en Chroma...")
        for norm_src in removed_norm:
            entry = indexed.get(norm_src)
            if not entry:
                continue
            _delete_previous_entries(vectorstore, entry.get("source"), entry.get("ids"))
            deleted_sources.append(entry.get("source", norm_src))

    # Detectar qué documentos necesitan ingesta (hash distinto o nuevos)
    pending_files = []
    for path in visible_files:
        fingerprint = current_fingerprints[path]
        norm = fingerprint["source"]
        prev = indexed.get(norm)
        prev_hash = prev.get("hash") if prev else None
        if prev_hash != fingerprint["source_hash"]:
            pending_files.append(path)

    if pending_files:
        print(f"📄 {len(pending_files)} documento(s) pendientes de ingesta:")
        for p in pending_files:
            print(f"   • {os.path.basename(p)}")
    else:
        print("No hay documentos nuevos ni modificados.")

    to_upsert = []
    for path in pending_files:
        fingerprint = current_fingerprints[path]
        norm = fingerprint["source"]
        prev = indexed.get(norm)

        prev_ids = prev.get("ids") if prev else []
        stored_source = prev.get("source") if prev else norm
        _delete_previous_entries(vectorstore, stored_source, prev_ids)

        # Dispatch by extension: .pdf -> pdf processor, .html -> html processor
        lower_ext = path.lower()
        if lower_ext.endswith(".pdf"):
            chunks = process_pdf_for_chunks(
                path,
                gemini_client,
                extra_metadata={
                    "source_hash": fingerprint["source_hash"],
                    "source_mtime": fingerprint["source_mtime"],
                    "source_size": fingerprint["source_size"],
                },
            )
        elif lower_ext.endswith(".html") or lower_ext.endswith(".htm"):
            chunks = process_html_for_chunks(
                path,
                gemini_client,
                extra_metadata={
                    "source_hash": fingerprint["source_hash"],
                    "source_mtime": fingerprint["source_mtime"],
                    "source_size": fingerprint["source_size"],
                },
            )
        elif lower_ext.endswith(".md"):
            chunks = process_md_for_chunks(
                path,
                extra_metadata={
                    "source_hash": fingerprint["source_hash"],
                    "source_mtime": fingerprint["source_mtime"],
                    "source_size": fingerprint["source_size"],
                },
            )
        else:
            chunks = []
        if chunks:
            to_upsert.extend(chunks)

    # Helper para persistir de forma segura
    def _safe_persist():
        try:
            persist_fn = getattr(vectorstore, "persist", None)
            if callable(persist_fn):
                persist_fn()
            else:
                client = getattr(vectorstore, "client", None) or getattr(vectorstore, "_client", None)
                if client and hasattr(client, "persist"):
                    client.persist()
        except Exception as exc:
            print(f"Error al persistir la base de datos Chroma: {exc}")

    if to_upsert:
        total_chunks = len(to_upsert)
        print(f"Generando embeddings y almacenando {total_chunks} trozos actualizados...")
        total_batches = (total_chunks + MAX_CHROMA_BATCH - 1) // MAX_CHROMA_BATCH
        for batch_idx in range(0, total_chunks, MAX_CHROMA_BATCH):
            batch = to_upsert[batch_idx : batch_idx + MAX_CHROMA_BATCH]
            current_batch = (batch_idx // MAX_CHROMA_BATCH) + 1
            print(f"  -> Subiendo lote {current_batch}/{total_batches} con {len(batch)} fragmentos...")
            vectorstore.add_documents(batch)
            # Persistir después de cada batch para minimizar pérdida si se interrumpe
            _safe_persist()

    # Persist final por seguridad
    _safe_persist()

    print("✅ Sincronización completada.")

    # Retornar listas de documentos procesados y eliminados para notificaciones
    processed = []
    for p in pending_files:
        rel = os.path.relpath(p, folder)
        processed.append({"nombre": os.path.basename(p), "ruta": rel.replace("\\", "/")})

    deleted = []
    for src in deleted_sources:
        # src ya es una ruta normalizada (ej: "Carpeta/archivo.pdf")
        deleted.append({"nombre": os.path.basename(src), "ruta": src})

    return {"procesados": processed, "eliminados": deleted}
