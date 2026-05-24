import importlib
import os
import re
import subprocess
import sys
import datetime
import threading
import requests as _requests

# Forzar UTF-8 en stdout/stderr del proceso uvicorn para evitar UnicodeEncodeError
# en producción (Windows) cuando los módulos de ingesta hacen print() con emojis.
# Sin esto, el servicio falla con 'charmap' codec can't encode character '\U0001f4c4'.
if hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
if hasattr(sys.stderr, "reconfigure"):
    try:
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
from fastapi import FastAPI, HTTPException, BackgroundTasks, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import PlainTextResponse, HTMLResponse, JSONResponse, FileResponse
import langchain
from typing import Optional, List

from app_logging import logger
from env_loader import load_host_env
from ingesta.query_core import (
    MODELO_CONSULTA,
    PROMPT_TEMPLATE,
    build_context,
    create_embedding_model,
    ensure_gemini_model,
    get_db_path,
    get_documents_folder,
    load_system_prompt,
    normalize_and_expand_query,
    open_vectorstore,
    SPACY_AVAILABLE,
    SPACY_DISABLED,
    _strip_accents,
)
from ingesta.utils import (
    build_source_token_index,
    file_fingerprint,
    get_chroma_collection,
    indexed_source_info,
    list_documents_recursive,
    normalize_source_reference,
)

# ── Módulos extraídos ───────────────────────────────────────────────────────
from endpoint_models import (
    ConsultaRequest,
    ConsultaResponse,
    DocumentoDetalle,
    DocumentoInfo,
    DocumentoPendiente,
    DocumentosResponse,
    EliminarDocumentoResponse,
    IngestaRequest,
    IngestaStatusResponse,
    PendientesResponse,
    TreeNode,
)
from endpoint_keywords import (
    _extract_keywords,
    _keyword_forms,
    _keyword_match_count,
    _source_keyword_match_count,
)
from endpoint_retrieval import (
    _normalize_source_input,
    _prioritize_docs,
    _augment_with_source_candidates,
    _augment_with_compound_code_search,
    _augment_with_priority_keyword_search,
    _augment_with_literal_keyword_search,
    init_retrieval_state,
)
from endpoint_helpers import (
    _is_debug_responses,
    _detectar_saludo,
    _es_busqueda_documentos,
    _build_fuentes,
    _GREETING_RESPONSES,
    _format_conversation_for_prompt,
    check_no_info,
    check_clarification,
)

# ── Inicialización ──────────────────────────────────────────────────────────

load_host_env()

# Cargar system prompt una vez al iniciar (cache)
SYSTEM_PROMPT = load_system_prompt()
logger.info("System prompt cargado (%d caracteres)", len(SYSTEM_PROMPT))

# Compatibilidad con versiones antiguas de LangChain
if not hasattr(langchain, "debug"):
    langchain.debug = False  # type: ignore[attr-defined]

try:
    DB_PATH = get_db_path()
except RuntimeError as exc:
    raise RuntimeError(f"No se pudo inicializar DB_PATH: {exc}") from exc

try:
    DOCUMENTS_FOLDER = get_documents_folder("manuales")
except RuntimeError as exc:
    raise RuntimeError(f"No se pudo resolver la carpeta de documentos: {exc}") from exc

# Importar la sincronización incremental desde ingesta.py de forma dinámica para evitar
# dependencias duras cuando se ejecute este archivo como script independiente.
sincronizar_pdf_con_chroma = None  # type: ignore[assignment]

try:
    ingesta_mod = importlib.import_module("ingesta")
    sincronizar_pdf_con_chroma = getattr(ingesta_mod, "sincronizar_pdf_con_chroma", None)
    DOCUMENTS_FOLDER = getattr(ingesta_mod, "DOCUMENTS_FOLDER", DOCUMENTS_FOLDER)
except Exception:
    sincronizar_pdf_con_chroma = None  # type: ignore[assignment]

# ── Vectorstore y modelos ───────────────────────────────────────────────────

embedding_model = create_embedding_model()

try:
    db = open_vectorstore(DB_PATH, embedding_model)
except Exception as exc:
    raise RuntimeError(
        "La base de datos Chroma no está disponible. Ejecuta 'ingesta_masiva.py' primero."
    ) from exc

_COLLECTION = get_chroma_collection(db)
SOURCE_TOKEN_INDEX = build_source_token_index(_COLLECTION)

# Conjunto de TODAS las fuentes indexadas (rutas normalizadas).
# Se usa para resolver prefix_filters a listas exactas de sources
# y pasarlas como filtro nativo $in a Chroma.
ALL_SOURCES: set[str] = set()
for _sources in SOURCE_TOKEN_INDEX.values():
    ALL_SOURCES.update(_sources)
logger.info("ALL_SOURCES: %d fuentes únicas indexadas.", len(ALL_SOURCES))

# Inicializar estado del módulo de retrieval
init_retrieval_state(_COLLECTION, SOURCE_TOKEN_INDEX)

llm = ensure_gemini_model(MODELO_CONSULTA)

RETRIEVAL_K = 20
FALLBACK_K = 150
TARGET_KEYWORD_DOCS = 3
MAX_CONTEXT_DOCS = 5
MAX_FALLBACK_APPEND = 20
SERVER_PORT = int(os.environ.get("RAG_PORT", "8888"))

# Lock threading para impedir que /ingesta/sync corra en paralelo consigo mismo.
# sync_ingesta.py (proceso externo) usa ingesta_lock.py para coordinarse entre procesos.
_ingesta_lock = threading.Lock()

if SPACY_AVAILABLE:
    logger.info("spaCy activo: se usan lemas para normalizar las preguntas.")
elif SPACY_DISABLED:
    logger.info("spaCy deshabilitado por RAG_DISABLE_SPACY; heurísticas básicas en uso.")
else:
    logger.info("spaCy no disponible; se mantienen heurísticas básicas de lematización.")


# ── Resolución de filtros de ruta a filtros nativos Chroma ──────────────────

def _resolve_allowed_sources(root_norm: str, prefix_filters: list[str]) -> set[str] | None:
    """Resuelve root_folder y query_paths a un set exacto de sources.

    Consulta ALL_SOURCES (construido al startup) para convertir prefijos
    a listas de fuentes concretas.  Devuelve None si no hay filtros.
    """
    if not root_norm and not prefix_filters:
        return None
    return {
        src for src in ALL_SOURCES
        if (not root_norm or src.startswith(root_norm))
        and (not prefix_filters or any(src.startswith(p) for p in prefix_filters))
    }


def _build_chroma_filter(
    rutas_norm: list[str],
    allowed_sources: set[str] | None,
) -> dict | None:
    """Construye un filtro Chroma nativo combinando rutas exactas y scope.

    Prioridad: rutas exactas > scope (root_folder/query_paths).
    Si la intersección es vacía, devuelve un sentinel que no matcheará nada.
    """
    if rutas_norm:
        if allowed_sources is not None:
            intersection = [r for r in rutas_norm if r in allowed_sources]
            if not intersection:
                return {"source": {"$eq": "__RAG_NO_MATCH__"}}
            return {"source": {"$in": intersection}}
        return {"source": {"$in": rutas_norm}}
    if allowed_sources is not None:
        if not allowed_sources:
            return {"source": {"$eq": "__RAG_NO_MATCH__"}}
        if len(allowed_sources) == len(ALL_SOURCES):
            return None  # Todas las fuentes coinciden, filtro innecesario
        return {"source": {"$in": list(allowed_sources)}}
    return None


# ── FastAPI ─────────────────────────────────────────────────────────────────

app = FastAPI(title="Consulta Técnica RAG", version="1.0.0")

ALLOWED_ORIGINS = [
    "http://127.0.0.1:8000",
    "http://192.168.13.162:8000",
    "http://81.33.21.33:6767",
]

app.add_middleware(
    CORSMiddleware,
    allow_origins=ALLOWED_ORIGINS,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# ── /health ────────────────────────────────────────────────────────────────

_START_TIME = datetime.datetime.now(datetime.timezone.utc)


@app.get("/health")
def health(type: Optional[str] = Query(default=None)):
    """Estado del servidor. Añade ?type=json para respuesta JSON."""
    uptime_seconds = int((datetime.datetime.now(datetime.timezone.utc) - _START_TIME).total_seconds())
    uptime_str = str(datetime.timedelta(seconds=uptime_seconds))

    # Comprobar vectorstore
    try:
        doc_count = _COLLECTION.count()
        vs_ok = True
    except Exception:
        doc_count = -1
        vs_ok = False

    spacy_status = "activo" if SPACY_AVAILABLE else ("deshabilitado" if SPACY_DISABLED else "no disponible")

    data = {
        "status": "ok" if vs_ok else "degraded",
        "uptime": uptime_str,
        "uptime_seconds": uptime_seconds,
        "vectorstore": "ok" if vs_ok else "error",
        "documents_indexed": doc_count,
        "model": MODELO_CONSULTA,
        "db_path": DB_PATH,
        "spacy": spacy_status,
        "started_at": _START_TIME.isoformat(),
    }

    if type == "json":
        return JSONResponse(content=data)

    color = "#2d7a2d" if vs_ok else "#b03030"
    rows = "".join(
        f"<tr><td style='padding:6px 12px;font-weight:bold;color:#555'>{k}</td>"
        f"<td style='padding:6px 12px'>{v}</td></tr>"
        for k, v in data.items()
    )
    html = f"""<!DOCTYPE html>
<html lang='es'>
<head><meta charset='UTF-8'><title>RAG – Health</title>
<style>body{{font-family:sans-serif;background:#f5f5f5;display:flex;justify-content:center;padding:40px}}
table{{background:#fff;border-radius:8px;box-shadow:0 2px 8px #0002;border-collapse:collapse}}
th{{background:{color};color:#fff;padding:10px 18px;text-align:left}}
tr:nth-child(even){{background:#f9f9f9}}</style>
</head>
<body>
<table>
  <tr><th colspan='2'>RAG – Estado del servidor</th></tr>
  {rows}
</table>
</body></html>"""
    return HTMLResponse(content=html)


# ── /consulta ───────────────────────────────────────────────────────────────

@app.post("/consulta", response_model=ConsultaResponse)
def consultar(req: ConsultaRequest):
    if not req.pregunta.strip():
        raise HTTPException(status_code=400, detail="La pregunta no puede estar vacía.")

    # Detectar saludos/agradecimientos/despedidas antes del RAG
    tipo_saludo = _detectar_saludo(req.pregunta)
    if tipo_saludo:
        return ConsultaResponse(
            respuesta=_GREETING_RESPONSES[tipo_saludo],
            fuentes=[],
            found=True,
            greeting=True,
        )

    normalized_query = normalize_and_expand_query(req.pregunta)
    keywords = _extract_keywords(_strip_accents(req.pregunta))
    all_sources_flag = _es_busqueda_documentos(req.pregunta)
    logger.info("Consulta: %s", req.pregunta[:200])
    logger.info("Keywords: %s", keywords)

    # Nota: La conversación se envía en el prompt a Gemini para coherencia,
    # pero NO se usa para la búsqueda vectorial (evita contaminación entre temas)

    keyword_forms = {keyword: _keyword_forms(keyword) for keyword in keywords}
    required_matches = 1 if len(keywords) <= 1 else 2
    root_norm = _normalize_source_input(req.root_folder)
    prefix_filters: list[str] = []
    if req.query_paths:
        raw_parts = [part.strip() for part in req.query_paths.split(",")]
        for part in raw_parts:
            normalized = _normalize_source_input(part)
            if normalized:
                prefix_filters.append(normalized)
        if prefix_filters:
            logger.debug("Filtro query_paths normalizado: %s", prefix_filters)

    allowed_sources = _resolve_allowed_sources(root_norm, prefix_filters)
    if allowed_sources is not None:
        logger.debug("Filtro nativo: %d fuentes de %d totales.", len(allowed_sources), len(ALL_SOURCES))

    # Si se especifican rutas, filtrar por metadato 'source' en Chroma
    rutas = (req.rutas or [])
    rutas_norm: list[str] = []
    if rutas:
        rutas_norm = [_normalize_source_input(p) for p in rutas if p]
        rutas_norm = [ruta for ruta in rutas_norm if ruta]
        if not rutas_norm:
            raise HTTPException(
                status_code=400,
                detail="Las rutas proporcionadas no coinciden con ROOT_FOLDER. Verifica tu selección.",
            )

    source_filter = _build_chroma_filter(rutas_norm, allowed_sources)

    try:
        documentos = db.similarity_search(
            normalized_query,
            k=RETRIEVAL_K,
            filter=source_filter,
        )
        if not documentos:
            documentos = db.similarity_search(
                req.pregunta,
                k=RETRIEVAL_K,
                filter=source_filter,
            )
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"Filtro inválido o error en la búsqueda: {exc}")

    documentos = documentos or []
    for idx, doc in enumerate(documentos):
        doc.metadata["_retrieval_rank"] = idx
        doc.metadata["_keyword_matches"] = _keyword_match_count(doc, keyword_forms) if keywords else 0
        doc.metadata["_path_keyword_matches"] = _source_keyword_match_count(doc, keyword_forms) if keywords else 0

    def is_strong(doc) -> bool:
        if not keywords:
            return True
        matches = doc.metadata.get("_keyword_matches", 0)
        path_matches = doc.metadata.get("_path_keyword_matches", 0)
        combined = matches + path_matches
        if combined < required_matches:
            return False
        if path_matches > 0:
            return True
        needed = min(len(keyword_forms), 3)
        return matches >= needed

    appended = 0
    if keywords:
        target_keyword_docs = min(TARGET_KEYWORD_DOCS, len(keyword_forms) or 1)
        strong_sources = {doc.metadata.get("source") for doc in documentos if is_strong(doc)}
        qualified = len(strong_sources)
        if qualified < target_keyword_docs:
            extra_docs = db.similarity_search(req.pregunta, k=FALLBACK_K, filter=source_filter)
            existing_signatures = {
                (
                    doc.metadata.get("source"),
                    doc.metadata.get("source_hash"),
                    doc.page_content,
                )
                for doc in documentos
            }
            for idx_extra, extra_doc in enumerate(extra_docs):
                signature = (
                    extra_doc.metadata.get("source"),
                    extra_doc.metadata.get("source_hash"),
                    extra_doc.page_content,
                )
                if signature in existing_signatures:
                    continue
                matches = _keyword_match_count(extra_doc, keyword_forms)
                path_matches = _source_keyword_match_count(extra_doc, keyword_forms)
                if matches + path_matches < required_matches:
                    continue
                if path_matches == 0 and matches < min(len(keyword_forms), 3):
                    continue
                extra_doc.metadata["_retrieval_rank"] = len(documentos) + idx_extra
                extra_doc.metadata["_keyword_matches"] = matches
                extra_doc.metadata["_path_keyword_matches"] = path_matches
                documentos.append(extra_doc)
                existing_signatures.add(signature)
                appended += 1
                if is_strong(extra_doc):
                    strong_sources.add(extra_doc.metadata.get("source"))
                    qualified = len(strong_sources)
                if appended >= MAX_FALLBACK_APPEND:
                    break
    if appended:
        logger.info("Añadidos %d fragmentos por coincidencia de palabras clave.", appended)

    source_appended = _augment_with_source_candidates(documentos, keyword_forms, required_matches, allowed_sources)
    if source_appended:
        logger.info("Añadidos %d fragmentos por coincidencia de ruta.", source_appended)

    _augment_with_compound_code_search(documentos, keyword_forms, req.pregunta, allowed_sources)
    _augment_with_priority_keyword_search(documentos, keyword_forms, allowed_sources)

    # Guardar estado pre-retry para poder restaurar si el retry no mejora.
    _pre_retry_docs = list(documentos) if documentos else []
    _pre_retry_kw = list(keywords)
    _pre_retry_kf = dict(keyword_forms)
    _pre_retry_rm = required_matches

    # Limpiar docs sin ningún match de keywords (comportamiento existente).
    if keywords and documentos and not any(
        doc.metadata.get("_keyword_matches", 0) > 0 or doc.metadata.get("_path_keyword_matches", 0) > 0
        for doc in documentos
    ):
        documentos = []

    # Evaluar cobertura: ¿cuántas keywords distintas matchean en algún doc?
    # Si pocas keywords están representadas, los docs probablemente sean off-topic
    # (ej. matchean "actualizar" pero no "borrar"/"disco"/"memoria").
    _tried_order_keywords = False
    _needs_retry = not documentos
    if not _needs_retry and keywords and len(keyword_forms) >= 4 and documentos:
        _covered = sum(
            1 for forms in keyword_forms.values()
            if any(
                any(
                    f in (d.page_content or "").lower() or f in str(d.metadata.get("source", "")).lower()
                    for f in forms
                )
                for d in documentos
            )
        )
        if _covered / len(keyword_forms) < 0.5:
            logger.info(
                "[retry] Cobertura keywords baja (%d/%d=%.0f%%); evaluando retry con orden de aparición.",
                _covered, len(keyword_forms), _covered / len(keyword_forms) * 100,
            )
            _needs_retry = True

    # --- Retry pre-LLM con keywords por orden de aparición ---
    if _needs_retry and keywords:
        keywords_by_order = _extract_keywords(_strip_accents(req.pregunta), prioritize_length=False)
        if set(keywords_by_order) != set(keywords):
            _tried_order_keywords = True
            logger.info("[pre-retry] Keywords originales: %s", keywords)
            logger.info("[pre-retry] Reintentando con keywords por orden de aparición: %s", keywords_by_order)
            keywords = keywords_by_order
            keyword_forms = {kw: _keyword_forms(kw) for kw in keywords}
            required_matches = 1 if len(keywords) <= 1 else 2

            documentos = db.similarity_search(normalized_query, k=RETRIEVAL_K, filter=source_filter)
            if not documentos:
                documentos = db.similarity_search(req.pregunta, k=RETRIEVAL_K, filter=source_filter)
            documentos = documentos or []

            for idx, doc in enumerate(documentos):
                doc.metadata["_retrieval_rank"] = idx
                doc.metadata["_keyword_matches"] = _keyword_match_count(doc, keyword_forms)
                doc.metadata["_path_keyword_matches"] = _source_keyword_match_count(doc, keyword_forms)

            # Fallback ampliado
            target_kw_docs = min(TARGET_KEYWORD_DOCS, len(keyword_forms) or 1)
            qualified_r = sum(1 for doc in documentos if doc.metadata.get("_keyword_matches", 0) >= required_matches)
            if qualified_r < target_kw_docs:
                extra_docs = db.similarity_search(req.pregunta, k=FALLBACK_K, filter=source_filter)
                existing_sigs = {
                    (d.metadata.get("source"), d.metadata.get("source_hash"), d.page_content)
                    for d in documentos
                }
                retry_appended = 0
                for idx_e, extra_doc in enumerate(extra_docs):
                    sig = (extra_doc.metadata.get("source"), extra_doc.metadata.get("source_hash"), extra_doc.page_content)
                    if sig in existing_sigs:
                        continue
                    matches = _keyword_match_count(extra_doc, keyword_forms)
                    path_matches = _source_keyword_match_count(extra_doc, keyword_forms)
                    if matches + path_matches < required_matches:
                        continue
                    if path_matches == 0 and matches < min(len(keyword_forms), 3):
                        continue
                    extra_doc.metadata["_retrieval_rank"] = len(documentos) + idx_e
                    extra_doc.metadata["_keyword_matches"] = matches
                    extra_doc.metadata["_path_keyword_matches"] = path_matches
                    documentos.append(extra_doc)
                    existing_sigs.add(sig)
                    retry_appended += 1
                    if retry_appended >= MAX_FALLBACK_APPEND:
                        break
                if retry_appended:
                    logger.info("[retry] Añadidos %d fragmentos en segunda pasada.", retry_appended)

            source_appended_r = _augment_with_source_candidates(documentos, keyword_forms, required_matches, allowed_sources)
            if source_appended_r:
                logger.info("[retry] Añadidos %d fragmentos por ruta en segunda pasada.", source_appended_r)

            _augment_with_compound_code_search(documentos, keyword_forms, req.pregunta, allowed_sources)
            _augment_with_priority_keyword_search(documentos, keyword_forms, allowed_sources)

            if keywords and not any(
                doc.metadata.get("_keyword_matches", 0) > 0 or doc.metadata.get("_path_keyword_matches", 0) > 0
                for doc in documentos
            ):
                documentos = []

            # Si el retry no mejoró y teníamos docs originales, restaurar.
            if not documentos and _pre_retry_docs:
                logger.info("[retry] Reintento sin resultados; restaurando documentos originales.")
                documentos = _pre_retry_docs
                keywords = _pre_retry_kw
                keyword_forms = _pre_retry_kf
                required_matches = _pre_retry_rm

    documentos = _prioritize_docs(documentos, keyword_forms, required_matches, MAX_CONTEXT_DOCS)
    contexto = build_context(documentos)

    # ── Fallback de último recurso: búsqueda literal por keyword ─────────
    # Solo se ejecuta si tras todo el pipeline no quedó contexto utilizable.
    # Garantiza zero impact sobre preguntas que ya funcionan: si hay docs,
    # esta rama no se toca.
    if not contexto and keywords:
        added = _augment_with_literal_keyword_search(
            documentos, keyword_forms, allowed_sources,
        )
        if added:
            documentos = _prioritize_docs(documentos, keyword_forms, required_matches, MAX_CONTEXT_DOCS)
            contexto = build_context(documentos)

    if not contexto:
        debug_mode = _is_debug_responses()
        _debug_info = None
        if debug_mode:
            _debug_info = {
                "keywords": keywords,
                "keyword_forms": {k: sorted(v) for k, v in keyword_forms.items()},
                "docs_retrieved": 0,
                "reason": "Sin contexto: no se encontraron documentos relevantes tras filtrado.",
            }
        return ConsultaResponse(
            respuesta="No encuentro información relevante en la base de conocimiento.",
            fuentes=[],
            found=True if debug_mode else False,
            debug_info=_debug_info,
        )

    question_text = req.pregunta
    conversation_block = _format_conversation_for_prompt(req.conversation, req.pregunta)
    if conversation_block:
        question_text = (
            "Historial de conversación (solo para coherencia y desambiguación, no como fuente de verdad):\n"
            f"{conversation_block}\n\n"
            "Pregunta actual:\n"
            f"{req.pregunta}"
        )

    prompt_text = PROMPT_TEMPLATE.format(
        system_prompt=SYSTEM_PROMPT,
        context=contexto,
        question=question_text
    )
    try:
        respuesta = llm.generate_content(prompt_text)
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"Error al invocar Gemini: {exc}")

    fuentes = _build_fuentes(documentos, keyword_forms, all_sources_flag)

    usage_data: dict[str, int] | None = None
    usage_obj = getattr(respuesta, "usage_metadata", None)
    if usage_obj:
        if isinstance(usage_obj, dict):
            usage_data = {
                key: int(value)
                for key, value in usage_obj.items()
                if isinstance(value, (int, float))
            }
        else:
            prompt_tokens = getattr(usage_obj, "prompt_token_count", None)
            candidates_tokens = getattr(usage_obj, "candidates_token_count", None)
            total_tokens = getattr(usage_obj, "total_token_count", None)
            usage_data = {
                key: value
                for key, value in {
                    "prompt_token_count": prompt_tokens,
                    "candidates_token_count": candidates_tokens,
                    "total_token_count": total_tokens,
                }.items()
                if isinstance(value, (int, float))
            }
    if usage_data == {}:
        usage_data = None

    contenido = getattr(respuesta, "text", None) or str(respuesta)

    # --- Detección de petición de aclaración (query ambigua) ---
    needs_clarification, contenido = check_clarification(contenido)
    if needs_clarification:
        fuentes = _build_fuentes(documentos, keyword_forms, all_sources_flag)
        debug_mode = _is_debug_responses()
        _debug_info = None
        if debug_mode:
            _debug_info = {
                "keywords": keywords,
                "keyword_forms": {k: sorted(v) for k, v in keyword_forms.items()},
                "docs_retrieved": len(documentos),
                "needs_clarification": True,
            }
        logger.info("Respuesta de aclaración: el LLM pidió desambiguación al usuario.")
        return ConsultaResponse(
            respuesta=contenido, fuentes=fuentes, usage=usage_data,
            found=True, needs_clarification=True, all_sources=all_sources_flag, debug_info=_debug_info,
        )

    found = check_no_info(contenido)

    # --- Post-LLM retry: si found=False, reintentar con keywords por orden de aparición ---
    # Solo si no se intentó ya en el retry pre-LLM.
    if not found and not _tried_order_keywords and keywords:
        _plr_kw = _extract_keywords(_strip_accents(req.pregunta), prioritize_length=False)
        if set(_plr_kw) != set(keywords):
            logger.info("[post-llm-retry] found=False con keywords %s; reintentando con orden: %s", keywords, _plr_kw)
            _plr_kf = {kw: _keyword_forms(kw) for kw in _plr_kw}
            _plr_rm = 1 if len(_plr_kw) <= 1 else 2

            _plr_docs = db.similarity_search(normalized_query, k=RETRIEVAL_K, filter=source_filter)
            if not _plr_docs:
                _plr_docs = db.similarity_search(req.pregunta, k=RETRIEVAL_K, filter=source_filter)
            _plr_docs = _plr_docs or []

            for idx, doc in enumerate(_plr_docs):
                doc.metadata["_retrieval_rank"] = idx
                doc.metadata["_keyword_matches"] = _keyword_match_count(doc, _plr_kf)
                doc.metadata["_path_keyword_matches"] = _source_keyword_match_count(doc, _plr_kf)

            # Fallback ampliado
            _plr_target = min(TARGET_KEYWORD_DOCS, len(_plr_kf) or 1)
            _plr_qual = sum(1 for d in _plr_docs if d.metadata.get("_keyword_matches", 0) >= _plr_rm)
            if _plr_qual < _plr_target:
                _plr_extra = db.similarity_search(req.pregunta, k=FALLBACK_K, filter=source_filter)
                _plr_sigs = {
                    (d.metadata.get("source"), d.metadata.get("source_hash"), d.page_content)
                    for d in _plr_docs
                }
                _plr_app = 0
                for idx_e, ed in enumerate(_plr_extra):
                    sig = (ed.metadata.get("source"), ed.metadata.get("source_hash"), ed.page_content)
                    if sig in _plr_sigs:
                        continue
                    m = _keyword_match_count(ed, _plr_kf)
                    if m < _plr_rm:
                        continue
                    pm = _source_keyword_match_count(ed, _plr_kf)
                    if pm == 0 and m < min(max(_plr_rm + 1, len(_plr_kf)), 3):
                        continue
                    ed.metadata["_retrieval_rank"] = len(_plr_docs) + idx_e
                    ed.metadata["_keyword_matches"] = m
                    ed.metadata["_path_keyword_matches"] = pm
                    _plr_docs.append(ed)
                    _plr_sigs.add(sig)
                    _plr_app += 1
                    if _plr_app >= MAX_FALLBACK_APPEND:
                        break
                if _plr_app:
                    logger.info("[post-llm-retry] Añadidos %d fragmentos fallback.", _plr_app)

            _augment_with_source_candidates(_plr_docs, _plr_kf, _plr_rm, allowed_sources)
            _augment_with_compound_code_search(_plr_docs, _plr_kf, req.pregunta, allowed_sources)
            _augment_with_priority_keyword_search(_plr_docs, _plr_kf, allowed_sources)

            _plr_docs = _prioritize_docs(_plr_docs, _plr_kf, _plr_rm, MAX_CONTEXT_DOCS)
            _plr_ctx = build_context(_plr_docs)

            if _plr_ctx:
                _plr_prompt = PROMPT_TEMPLATE.format(
                    system_prompt=SYSTEM_PROMPT,
                    context=_plr_ctx,
                    question=question_text,
                )
                try:
                    _plr_resp = llm.generate_content(_plr_prompt)
                    _plr_content = getattr(_plr_resp, "text", None) or str(_plr_resp)
                    _plr_found = check_no_info(_plr_content)
                    if _plr_found:
                        logger.info("[post-llm-retry] Retry exitoso con keywords por orden de aparición.")
                        contenido = _plr_content
                        found = True
                        documentos = _plr_docs
                        keywords = _plr_kw
                        keyword_forms = _plr_kf
                        fuentes = _build_fuentes(documentos, keyword_forms, all_sources_flag)
                        # Actualizar usage con los datos del retry
                        _plr_usage = getattr(_plr_resp, "usage_metadata", None)
                        if _plr_usage:
                            if isinstance(_plr_usage, dict):
                                usage_data = {k: int(v) for k, v in _plr_usage.items() if isinstance(v, (int, float))}
                            else:
                                _ru = {
                                    "prompt_token_count": getattr(_plr_usage, "prompt_token_count", None),
                                    "candidates_token_count": getattr(_plr_usage, "candidates_token_count", None),
                                    "total_token_count": getattr(_plr_usage, "total_token_count", None),
                                }
                                usage_data = {k: v for k, v in _ru.items() if isinstance(v, (int, float))} or None
                    else:
                        logger.info("[post-llm-retry] Retry también resultó en found=False.")
                except Exception:
                    logger.warning("[post-llm-retry] Error al invocar Gemini en retry.", exc_info=True)

    debug_mode = _is_debug_responses()
    _debug_info = None
    if debug_mode:
        _debug_info = {
            "keywords": keywords,
            "keyword_forms": {k: sorted(v) for k, v in keyword_forms.items()},
            "docs_retrieved": len(documentos),
            "docs_detail": [
                {
                    "source": doc.metadata.get("source", ""),
                    "keyword_matches": doc.metadata.get("_keyword_matches", 0),
                    "path_matches": doc.metadata.get("_path_keyword_matches", 0),
                    "rank": doc.metadata.get("_retrieval_rank", 0),
                    "snippet": (doc.page_content or "")[:200],
                }
                for doc in documentos
            ],
            "found_original": found,
        }
        found = True  # Forzar para que el frontend muestre la respuesta real

    # Log de resultado
    sources_short = list({doc.metadata.get("source", "") for doc in documentos})
    logger.info("Resultado: found=%s, docs=%d, fuentes=%s", found, len(documentos), sources_short)

    return ConsultaResponse(respuesta=contenido, fuentes=fuentes, usage=usage_data, found=found, needs_clarification=False, all_sources=all_sources_flag, debug_info=_debug_info)


# ── /estructura ─────────────────────────────────────────────────────────────

def _build_tree(root: str) -> TreeNode:
    root_path = os.path.abspath(root)
    if not os.path.exists(root_path):
        raise HTTPException(status_code=400, detail=f"La ruta no existe: {root_path}")

    def walk(current: str) -> TreeNode:
        children: list[TreeNode] = []
        try:
            entries = sorted(os.listdir(current))
        except PermissionError:
            entries = []
        for entry in entries:
            full_path = os.path.join(current, entry)
            if os.path.isdir(full_path):
                children.append(walk(full_path))
        return TreeNode(
            name=os.path.basename(current) or current,
            path=current,
            type="directory",
            children=children or None,
        )

    return walk(root_path)


@app.get("/estructura", response_model=TreeNode)
def obtener_estructura(base: Optional[str] = None):
    target = base or DOCUMENTS_FOLDER
    return _build_tree(target)


# ── /ingesta/sync ───────────────────────────────────────────────────────────

# URL del endpoint de api para notificar tras ingesta (ej: http://localhost:8000/api/docs/ingesta/notify)
INGESTA_NOTIFY_URL = os.environ.get("RAG_INGESTA_NOTIFY_URL", "").strip()

def _refresh_vectorstore():
    """Reinicializa el vectorstore, colección, token index y ALL_SOURCES tras una ingesta."""
    global db, _COLLECTION, SOURCE_TOKEN_INDEX, ALL_SOURCES
    try:
        db = open_vectorstore(DB_PATH, embedding_model)
        _COLLECTION = get_chroma_collection(db)
        SOURCE_TOKEN_INDEX = build_source_token_index(_COLLECTION)
        ALL_SOURCES = set()
        for _sources in SOURCE_TOKEN_INDEX.values():
            ALL_SOURCES.update(_sources)
        init_retrieval_state(_COLLECTION, SOURCE_TOKEN_INDEX)
        logger.info(
            "Vectorstore refrescado: %d tokens en índice de rutas, %d fuentes únicas.",
            len(SOURCE_TOKEN_INDEX), len(ALL_SOURCES),
        )
    except Exception as exc:
        # No interrumpir si hay un problema; quedará el estado anterior
        logger.error("No se pudo refrescar el vectorstore: %s", exc)


def _notify_ingesta(folder: str, documentos: list, eliminados: list | None = None):
    """Envía notificación POST a api con los documentos procesados y eliminados."""
    eliminados = eliminados or []
    if not INGESTA_NOTIFY_URL:
        logger.debug("RAG_INGESTA_NOTIFY_URL no configurado; omitiendo notificación de ingesta.")
        return
    if not documentos and not eliminados:
        logger.info("Ingesta sin cambios; omitiendo notificación.")
        return
    payload = {
        "folder": folder,
        "documentos": documentos,
        "eliminados": eliminados,
        "timestamp": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    }
    try:
        resp = _requests.post(INGESTA_NOTIFY_URL, json=payload, timeout=30)
        if resp.ok:
            logger.info(
                "Notificación de ingesta enviada correctamente (%d procesados, %d eliminados) → %s",
                len(documentos),
                len(eliminados),
                INGESTA_NOTIFY_URL,
            )
        else:
            logger.warning(
                "Notificación de ingesta respondió con status %d: %s",
                resp.status_code,
                resp.text[:300],
            )
    except Exception as exc:
        logger.error("Error al enviar notificación de ingesta: %s", exc)


def _restart_server():
    """Lanza un proceso desacoplado que mata y relanza uvicorn en SERVER_PORT."""
    repo_root = os.path.dirname(os.path.abspath(__file__))
    python_exe = sys.executable
    # Usar pythonw.exe si está disponible (ejecución sin ventana de consola)
    pythonw = python_exe.replace("python.exe", "pythonw.exe")
    if not os.path.isfile(pythonw):
        pythonw = python_exe

    # Comando: esperar 2s → matar procesos en el puerto → esperar 1s → relanzar uvicorn
    kill_cmd = (
        f'for /f "tokens=5" %a in '
        f"('netstat -ano ^| findstr :{SERVER_PORT}') "
        f"do taskkill /PID %a /F >nul 2>&1"
    )
    start_cmd = (
        f'start "" /B "{pythonw}" '
        f"-m uvicorn endpoint:app --host 0.0.0.0 --port {SERVER_PORT} "
        f'>> docs.log 2>&1'
    )
    full_cmd = (
        f"timeout /t 2 /nobreak >nul & "
        f"{kill_cmd} & "
        f"timeout /t 1 /nobreak >nul & "
        f'cd /d "{repo_root}" & '
        f"{start_cmd}"
    )

    logger.info("[restart] Lanzando reinicio desacoplado del servidor en puerto %d...", SERVER_PORT)
    # Flags Windows para proceso completamente desacoplado
    CREATE_NEW_PROCESS_GROUP = 0x00000200
    DETACHED_PROCESS = 0x00000008
    CREATE_NO_WINDOW = 0x08000000
    subprocess.Popen(
        ["cmd.exe", "/c", full_cmd],
        creationflags=CREATE_NEW_PROCESS_GROUP | DETACHED_PROCESS | CREATE_NO_WINDOW,
        close_fds=True,
    )


def _sync_job(folder: str, restart: bool = False):
    """Ejecuta la ingesta in-process.

    Mantenemos la llamada in-process para compartir el cliente SQLite de
    Chroma con el proceso uvicorn (entre procesos distintos compiten por el
    lock). Envolvemos todo en try/except con logging exhaustivo para que
    cualquier fallo (antes silencioso al ser BackgroundTask) quede registrado
    en el log de la app y sea diagnosticable en producción.
    """
    if sincronizar_pdf_con_chroma is None:
        logger.warning("[ingesta/sync] Función de sincronización no disponible. Verifica 'ingesta'.")
        return
    if not _ingesta_lock.acquire(blocking=False):
        logger.warning("[ingesta/sync] Ya hay una ingesta en curso; ignorando solicitud paralela.")
        return

    processed_docs: list = []
    deleted_docs: list = []
    try:
        logger.info("[ingesta/sync] Iniciando ingesta in-process. folder=%s", folder)
        try:
            result = sincronizar_pdf_con_chroma(folder) or {}
        except Exception as exc:
            logger.exception("[ingesta/sync] Excepción durante sincronizar_pdf_con_chroma: %s", exc)
            return
        if isinstance(result, dict):
            processed_docs = result.get("procesados", []) or []
            deleted_docs = result.get("eliminados", []) or []
        else:
            processed_docs = list(result) if result else []
        logger.info(
            "[ingesta/sync] Ingesta completada. procesados=%d eliminados=%d",
            len(processed_docs), len(deleted_docs),
        )
    finally:
        _ingesta_lock.release()
        try:
            _refresh_vectorstore()
        except Exception as exc:
            logger.exception("[ingesta/sync] Error en _refresh_vectorstore: %s", exc)

    # Notificar a api en un hilo aparte para no bloquear
    if processed_docs or deleted_docs:
        threading.Thread(
            target=_notify_ingesta,
            args=(folder, processed_docs, deleted_docs),
            daemon=True,
        ).start()

    # Reiniciar SOLO si se solicitó y hubo cambios reales. El restart es caro
    # (interrumpe conexiones, log roto unos segundos); evitamos hacerlo si la
    # ingesta no procesó ni eliminó nada.
    if restart and (processed_docs or deleted_docs):
        logger.info(
            "[ingesta/sync] Reiniciando servidor tras ingesta con cambios (procesados=%d eliminados=%d).",
            len(processed_docs), len(deleted_docs),
        )
        _restart_server()
    elif restart:
        logger.info("[ingesta/sync] Restart solicitado pero sin cambios; se omite reinicio.")


@app.get("/ingesta/status", response_model=IngestaStatusResponse)
def ingesta_status():
    """Devuelve si hay una ingesta en curso."""
    return IngestaStatusResponse(running=_ingesta_lock.locked())


@app.get("/ingesta/pendientes", response_model=PendientesResponse)
def ingesta_pendientes(carpeta: str | None = None):
    """Lista los documentos que se procesarían en la próxima ingesta (dry-run).

    Replica la detección de `sincronizar_pdf_con_chroma` sin insertar nada en Chroma.
    Clasifica los documentos en:
      - nuevos: presentes en disco pero ausentes del vectorstore.
      - modificados: presentes en ambos pero con hash distinto.
      - eliminados: indexados en Chroma pero ya no existen en disco.
      - ocultos: archivos con marcador `.hidden` que se eliminarían del índice.
    """
    folder = carpeta or DOCUMENTS_FOLDER
    if not os.path.isabs(folder):
        folder = os.path.abspath(folder)
    if not os.path.exists(folder):
        raise HTTPException(status_code=400, detail=f"La carpeta no existe: {folder}")

    collection = get_chroma_collection(db)
    indexed = indexed_source_info(collection)

    all_files = list_documents_recursive(folder)
    visible_files: list[str] = []
    hidden_paths: list[str] = []
    for path in all_files:
        if os.path.exists(f"{path}.hidden"):
            hidden_paths.append(path)
        else:
            visible_files.append(path)

    def _build(path: str, estado: str, fp: dict | None = None) -> DocumentoPendiente:
        norm = normalize_source_reference(path)
        return DocumentoPendiente(
            ruta=norm,
            nombre=os.path.basename(path),
            carpeta=os.path.dirname(norm),
            estado=estado,
            hash=(fp.get("source_hash") if fp else None),
            size=(fp.get("source_size") if fp else None),
            mtime=(fp.get("source_mtime") if fp else None),
        )

    nuevos: list[DocumentoPendiente] = []
    modificados: list[DocumentoPendiente] = []
    current_norm_set: set[str] = set()

    for path in visible_files:
        fp = file_fingerprint(path)
        norm = fp["source"]
        current_norm_set.add(norm)
        prev = indexed.get(norm)
        if prev is None:
            nuevos.append(_build(path, "nuevo", fp))
        elif prev.get("hash") != fp["source_hash"]:
            modificados.append(_build(path, "modificado", fp))

    eliminados: list[DocumentoPendiente] = []
    for norm, entry in indexed.items():
        if norm in current_norm_set:
            continue
        source = entry.get("source") or norm
        eliminados.append(
            DocumentoPendiente(
                ruta=norm,
                nombre=os.path.basename(source),
                carpeta=os.path.dirname(norm),
                estado="eliminado",
            )
        )

    ocultos: list[DocumentoPendiente] = []
    for path in hidden_paths:
        norm = normalize_source_reference(path)
        ocultos.append(
            DocumentoPendiente(
                ruta=norm,
                nombre=os.path.basename(path),
                carpeta=os.path.dirname(norm),
                estado="oculto",
            )
        )

    total = len(nuevos) + len(modificados) + len(eliminados) + len(ocultos)
    return PendientesResponse(
        carpeta=folder,
        nuevos=nuevos,
        modificados=modificados,
        eliminados=eliminados,
        ocultos=ocultos,
        total=total,
    )


@app.post("/ingesta/sync")
def ingesta_sync(background_tasks: BackgroundTasks, req: IngestaRequest | None = None):
    if sincronizar_pdf_con_chroma is None:
        raise HTTPException(status_code=500, detail="Sincronización no disponible. Revisa 'ingesta.py'.")
    if _ingesta_lock.locked():
        return JSONResponse(
            status_code=409,
            content={"status": "busy", "detail": "Ya hay una ingesta en curso. Inténtalo de nuevo cuando termine."},
        )
    folder = (req.carpeta if req else None) or DOCUMENTS_FOLDER
    # Default: reiniciar tras ingesta si hubo cambios. El cliente puede
    # pasar restart=false explícitamente para suprimirlo.
    restart = True if (req is None or req.restart is None) else bool(req.restart)
    if not os.path.isabs(folder):
        folder = os.path.abspath(folder)
    if not os.path.exists(folder):
        raise HTTPException(status_code=400, detail=f"La carpeta no existe: {folder}")

    background_tasks.add_task(_sync_job, folder, restart)
    return {"status": "enqueued", "folder": folder, "restart": restart}


# ── /restart ────────────────────────────────────────────────────────────────

@app.post("/restart")
def restart_server():
    """Reinicia el servicio uvicorn lanzando restart.bat de forma desacoplada.

    El .bat mata el proceso actual (puerto 8888) y arranca uno nuevo. La
    respuesta se envía antes de que el proceso muera porque se devuelve
    inmediatamente y restart.bat espera 2 segundos antes de reactivar.
    """
    bat_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "restart.bat")
    if not os.path.isfile(bat_path):
        raise HTTPException(status_code=500, detail="restart.bat no encontrado.")

    try:
        # DETACHED_PROCESS (0x00000008) + CREATE_NEW_PROCESS_GROUP (0x00000200)
        # para que el .bat sobreviva al kill del proceso uvicorn actual.
        creationflags = 0x00000008 | 0x00000200
        subprocess.Popen(
            ["cmd.exe", "/c", bat_path],
            creationflags=creationflags,
            close_fds=True,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    except Exception as exc:
        logger.exception("Error al lanzar restart.bat: %s", exc)
        raise HTTPException(status_code=500, detail=f"No se pudo lanzar restart.bat: {exc}")

    return {"status": "restarting", "bat": bat_path}


# ── /documentos ─────────────────────────────────────────────────────────────

@app.get("/documentos", response_model=DocumentosResponse)
def listar_documentos(
    limit: int | None = None,
    carpeta: str | None = None,
    texto: str | None = None,
):
    """Retorna el listado detallado de documentos indexados en Chroma.

    Parámetros:
    - limit: número máximo de documentos a retornar.
    - carpeta: filtra documentos cuya carpeta contenga esta subcadena.
    - texto: filtra documentos que tengan al menos un chunk cuyo contenido
             contenga esta subcadena (búsqueda insensible a mayúsculas).
    """
    collection = get_chroma_collection(db)
    if not collection:
        return DocumentosResponse(documentos=[], total=0, total_chunks=0)

    # Acumular por fuente única: metadatos del primer chunk + contador de chunks
    seen_norm: dict[str, str] = {}          # norm_key → ruta original
    chunks_count: dict[str, int] = {}       # norm_key → número de chunks
    meta_per_source: dict[str, dict] = {}   # norm_key → metadatos del primer chunk
    # Fuentes con al menos un chunk que contiene el texto buscado
    texto_match_norms: set[str] = set()

    carpeta_norm = os.path.normcase(carpeta.strip()).replace("\\", "/") if carpeta else None
    texto_norm = _strip_accents(texto.strip().lower()) if texto else None

    # Si hay búsqueda de texto incluimos el contenido de los chunks
    includes = ["metadatas", "documents"] if texto_norm else ["metadatas"]

    offset = 0
    batch_size = 500
    while True:
        try:
            res = collection.get(include=includes, limit=batch_size, offset=offset)
        except TypeError:
            res = collection.get(include=includes)
        metadatas = res.get("metadatas") or []
        if not metadatas:
            break
        documents = res.get("documents") or []
        for i, md in enumerate(metadatas):
            src = (md or {}).get("source")
            if not src:
                continue
            norm = os.path.normcase(os.path.abspath(src))
            if norm not in seen_norm:
                seen_norm[norm] = src
                chunks_count[norm] = 1
                meta_per_source[norm] = md
            else:
                chunks_count[norm] += 1
            # Comprobamos si este chunk contiene el texto buscado
            if texto_norm:
                chunk_text = (documents[i] if i < len(documents) else "") or ""
                if texto_norm in _strip_accents(chunk_text.lower()):
                    texto_match_norms.add(norm)
        if len(metadatas) < batch_size:
            break
        offset += batch_size

    # Construir lista de DocumentoInfo, aplicando filtros de carpeta y texto
    result: list[DocumentoInfo] = []
    total_chunks = 0
    for norm, src in seen_norm.items():
        carpeta_doc = os.path.dirname(src).replace("\\", "/")
        if carpeta_norm and carpeta_norm not in os.path.normcase(carpeta_doc).replace("\\", "/"):
            continue
        if texto_norm and norm not in texto_match_norms:
            continue
        md = meta_per_source[norm]
        n_chunks = chunks_count[norm]
        total_chunks += n_chunks
        result.append(DocumentoInfo(
            ruta=src,
            nombre=os.path.basename(src),
            carpeta=carpeta_doc,
            chunks=n_chunks,
            hash=md.get("source_hash") or None,
            mtime=md.get("source_mtime") or None,
            size=md.get("source_size") or None,
        ))
        if limit is not None and len(result) >= limit:
            break

    result.sort(key=lambda d: d.ruta.lower())
    return DocumentosResponse(documentos=result, total=len(result), total_chunks=total_chunks)


# ── DELETE /documentos ──────────────────────────────────


@app.delete("/documentos", response_model=EliminarDocumentoResponse)
def eliminar_documento(ruta: str):
    """Elimina todos los chunks indexados de un documento por su ruta exacta.

    Parámetros:
    - ruta: ruta del documento tal como está almacenada en los metadatos de Chroma.
    """
    collection = get_chroma_collection(db)
    if not collection:
        raise HTTPException(status_code=500, detail="Vectorstore no disponible.")

    # Buscar todos los chunk IDs cuyo metadato 'source' coincida con la ruta
    offset = 0
    batch_size = 500
    ids_to_delete: list[str] = []
    while True:
        try:
            res = collection.get(include=["metadatas"], limit=batch_size, offset=offset)
        except TypeError:
            res = collection.get(include=["metadatas"])
        metadatas = res.get("metadatas") or []
        chunk_ids = res.get("ids") or []
        if not metadatas:
            break
        for cid, md in zip(chunk_ids, metadatas):
            src = (md or {}).get("source", "")
            if src == ruta:
                ids_to_delete.append(cid)
        if len(metadatas) < batch_size:
            break
        offset += batch_size

    if not ids_to_delete:
        raise HTTPException(status_code=404, detail=f"No se encontraron chunks para la ruta: {ruta}")

    collection.delete(ids=ids_to_delete)
    logger.info("Eliminados %d chunks de '%s'", len(ids_to_delete), ruta)
    return EliminarDocumentoResponse(deleted=len(ids_to_delete), ruta=ruta)


# ── /documentos/detalle ────────────────────────────────────────

@app.get("/documentos/detalle", response_model=DocumentoDetalle)
def detalle_documento(ruta: str):
    """Retorna el texto completo (todos los chunks concatenados) de un documento indexado.

    Parámetros:
    - ruta: ruta exacta del documento tal como aparece en /documentos (ej. docs/tecnico/manual.pdf)
    """
    collection = get_chroma_collection(db)
    if not collection:
        raise HTTPException(status_code=503, detail="Vectorstore no disponible.")

    ruta_norm = os.path.normcase(os.path.abspath(ruta.strip()))

    # Recuperar todos los chunks de esta fuente recorriendo la colección en batches
    chunks_text: list[tuple[int, int, str]] = []  # (page_number, chunk_index, text)
    first_metadata: dict | None = None
    original_ruta: str | None = None
    offset = 0
    batch_size = 500

    while True:
        try:
            res = collection.get(include=["metadatas", "documents"], limit=batch_size, offset=offset)
        except TypeError:
            res = collection.get(include=["metadatas", "documents"])
        metadatas = res.get("metadatas") or []
        documents = res.get("documents") or []
        if not metadatas:
            break
        for md, text in zip(metadatas, documents):
            src = (md or {}).get("source")
            if not src:
                continue
            if os.path.normcase(os.path.abspath(src)) != ruta_norm:
                continue
            if first_metadata is None:
                first_metadata = md
                original_ruta = src
            page = int((md or {}).get("page", 0) or 0)
            chunk_index = int((md or {}).get("chunk_index", 0) or 0)
            chunks_text.append((page, chunk_index, text or ""))
        if len(metadatas) < batch_size:
            break
        offset += batch_size

    if not chunks_text:
        raise HTTPException(status_code=404, detail=f"No se encontró ninguna fuente con la ruta: {ruta}")

    # Ordenar por número de página y chunk_index para preservar el orden de lectura
    chunks_text.sort(key=lambda x: (x[0], x[1]))
    texto_completo = "\n\n".join(t for _, _, t in chunks_text)

    md = first_metadata or {}
    src = original_ruta or ruta
    return DocumentoDetalle(
        ruta=src,
        nombre=os.path.basename(src),
        carpeta=os.path.dirname(src).replace("\\", "/"),
        chunks=len(chunks_text),
        hash=md.get("source_hash") or None,
        mtime=md.get("source_mtime") or None,
        size=md.get("source_size") or None,
        texto=texto_completo,
    )


# ── /documentos/archivo ─────────────────────────────────────────────────────

@app.get("/documentos/archivo")
def descargar_archivo(ruta: str):
    """Descarga el archivo original del disco.

    Parámetros:
    - ruta: ruta tal como aparece en /documentos (ej. docs/tecnico/manual.pdf)

    El archivo debe estar dentro de DOCUMENTS_FOLDER. Se rechaza cualquier
    intento de salir de esa carpeta (path traversal).
    """
    from ingesta.config import DOCUMENTS_FOLDER

    if not ruta or not ruta.strip():
        raise HTTPException(status_code=422, detail="El parámetro ruta es obligatorio.")

    # Resolver la ruta candidata: puede ser absoluta (ya indexada así) o relativa
    ruta_clean = ruta.strip()
    if os.path.isabs(ruta_clean):
        candidate = os.path.normcase(os.path.normpath(ruta_clean))
    else:
        candidate = os.path.normcase(os.path.normpath(os.path.join(DOCUMENTS_FOLDER, ruta_clean)))

    docs_root = os.path.normcase(os.path.normpath(DOCUMENTS_FOLDER))

    # Seguridad: la ruta resuelta debe estar dentro de DOCUMENTS_FOLDER
    if not candidate.startswith(docs_root + os.sep) and candidate != docs_root:
        raise HTTPException(status_code=403, detail="Acceso denegado.")

    if not os.path.isfile(candidate):
        raise HTTPException(status_code=404, detail=f"Archivo no encontrado: {ruta}")

    filename = os.path.basename(candidate)

    # Detectar mime-type según extensión
    import mimetypes
    mime, _ = mimetypes.guess_type(candidate)
    media_type = mime or "application/octet-stream"

    return FileResponse(
        path=candidate,
        media_type=media_type,
        filename=filename,
    )


# ── /log ────────────────────────────────────────────────

@app.get("/log", response_class=PlainTextResponse)
def obtener_log(tail: int | None = None):
    """Retorna el contenido del archivo de log.

    Parámetros:
        tail: si se indica, retorna solo las últimas N líneas.
    """
    from app_logging import _LOG_FILE

    if not os.path.isfile(_LOG_FILE):
        raise HTTPException(status_code=404, detail="El archivo de log no existe todavía.")

    try:
        with open(_LOG_FILE, "r", encoding="utf-8", errors="replace") as f:
            if tail is not None and tail > 0:
                lines = f.readlines()
                return "".join(lines[-tail:])
            return f.read()
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Error al leer el log: {exc}")
