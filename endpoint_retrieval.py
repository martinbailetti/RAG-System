"""Funciones de recuperación, priorización y augmentación de documentos para el pipeline RAG."""

import os
import re
from typing import List, Dict, Any, Tuple

from app_logging import logger
from ingesta.utils import normalize_source_reference
from endpoint_keywords import (
    _keyword_match_count,
    _source_keyword_match_count,
    _keyword_match_quality,
    _strip_accents,
    PRIORITY_KEYWORDS,
    STOPWORDS,
)


# ── Constantes ──────────────────────────────────────────────────────────────

SOURCE_FALLBACK_TOKEN_LIMIT = 12
SOURCE_FALLBACK_DOCS_PER_SOURCE = 2
SOURCE_FALLBACK_MAX_DOCS = 12

# ── Estado del módulo (inicializado desde endpoint.py) ──────────────────────
_COLLECTION = None
SOURCE_TOKEN_INDEX: dict = {}
_SOURCE_CHUNK_CACHE: Dict[str, List[Tuple[str, Dict[str, Any]]]] = {}


def init_retrieval_state(collection, source_token_index):
    """Inicializa el estado del módulo con la colección Chroma y el índice de tokens.

    Debe llamarse una vez desde endpoint.py tras abrir el vectorstore.
    """
    global _COLLECTION, SOURCE_TOKEN_INDEX
    _COLLECTION = collection
    SOURCE_TOKEN_INDEX = source_token_index


# ── Contenedor ligero ───────────────────────────────────────────────────────

class _InlineDoc:
    """Contenedor ligero para documentos añadidos por heurísticas."""

    def __init__(self, content: str, metadata: Dict[str, Any]):
        self.page_content = content
        self.metadata = metadata


# ── Utilidades de normalización de rutas ────────────────────────────────────

def _norm_path(path: str | None) -> str:
    if not path:
        return ""
    return os.path.normcase(os.path.abspath(path))


def _normalize_source_input(value: str | None) -> str:
    if not value:
        return ""
    normalized = normalize_source_reference(value.strip())
    return normalized.strip()


# ── Densidad de contenido útil ──────────────────────────────────────────────

# Patrones de marcadores de imagen generados durante la ingesta (HTML y PDF).
# Se usan para estimar qué proporción de un chunk es descripción visual vs.
# texto técnico real.
_RE_IMG_BLOCK = re.compile(
    r"\[DESCRIPCI[ÓO]N DE IMAGEN[^\]]*\]:?\s*.*?\.\]",
    re.DOTALL | re.IGNORECASE,
)
_RE_IMG_NO_DESC = re.compile(
    r"\[IMAGEN[^\]]*\]:?\s*sin descripción disponible\.",
    re.IGNORECASE,
)
# Cuerpo huérfano de descripción de imagen: párrafo (≥50 chars) que termina con
# ``.]``.  Patrón exclusivo de textos IA — aparece cuando el text-splitter
# parte un bloque y el chunk siguiente recibe solo el body sin cabecera.
_RE_ORPHAN_IMG_BODY = re.compile(
    r"(?:(?<=\n\n)|(?<=\A))[^\n](?:(?!\n\n).){49,}?\.\](?=\s*\n|\s*\Z)",
    re.DOTALL,
)


def _useful_content_ratio(doc) -> float:
    """Devuelve la proporción de texto útil (0.0 – 1.0) en un chunk.

    Estima cuánto del chunk es contenido técnico real frente a descripciones
    de imagen generadas automáticamente durante la ingesta.  Chunks dominados
    por descripciones visuales de capturas de pantalla reciben una ratio baja.
    """
    raw = getattr(doc, "page_content", "") or ""
    if not raw:
        return 1.0
    cleaned = _RE_IMG_BLOCK.sub("", raw)
    cleaned = _RE_IMG_NO_DESC.sub("", cleaned)
    cleaned = _RE_ORPHAN_IMG_BODY.sub("", cleaned)
    cleaned_len = len(cleaned.strip())
    raw_len = len(raw)
    if raw_len == 0:
        return 1.0
    return cleaned_len / raw_len


# ── Priorización de documentos ──────────────────────────────────────────────

def _prioritize_docs(
    docs: list,
    keyword_forms: dict[str, set[str]],
    required_matches: int,
    limit: int,
) -> list:
    if not docs:
        return []
    if not keyword_forms:
        return sorted(docs, key=lambda d: d.metadata.get("_retrieval_rank", 0))[:limit]

    def is_strong(doc) -> bool:
        matches = doc.metadata.get("_keyword_matches", 0)
        path_matches = doc.metadata.get("_path_keyword_matches", 0)
        combined = matches + path_matches
        if combined < required_matches:
            return False
        if path_matches > 0:
            return True
        needed = min(len(keyword_forms), 3)
        return matches >= needed

    def doc_score(doc) -> float:
        matches = doc.metadata.get("_keyword_matches", 0)
        path_matches = doc.metadata.get("_path_keyword_matches", 0)
        rank = doc.metadata.get("_retrieval_rank", 0)

        # Bonus por densidad de contenido útil: chunks dominados por
        # descripciones de imagen (generadas en la ingesta) tienen mucho
        # ruido visual que no aporta información procedimental.
        # Un chunk con 80% de imagen descripciones aporta poco valor
        # comparado con uno que es 100% texto técnico real.
        useful_ratio = _useful_content_ratio(doc)
        # Rango: 0.0 (todo imagen) – 1.0 (nada imagen).
        # Multiplicado por 500 da un bonus significativo pero no
        # predominante sobre keyword matches (1000 cada uno).
        density_bonus = useful_ratio * 500

        # Bonus de calidad: distingue matches directos (palabra real del
        # usuario en el texto) de matches solo por sinónimo.
        # Direct=1.0 por keyword, synonym-only=0.5.
        # Multiplicado por 1500 genera diferencia significativa.
        quality = _keyword_match_quality(doc, keyword_forms)
        quality_bonus = quality * 1500

        # Bonus por keyword prioritario en ruta: cuando el doc es específicamente
        # sobre ese protocolo/sistema (ej. "sas" en "configuracion-sas.html"),
        # recibe un bonus mayor que un path_match genérico para garantizar que
        # siempre supere a docs que solo mencionan el término de pasada.
        priority_src = _strip_accents(str(doc.metadata.get("source", "")).lower())
        priority_bonus = 6000 * sum(
            1 for kw in keyword_forms
            if kw in PRIORITY_KEYWORDS
            and any(form in priority_src for form in keyword_forms[kw])
        )

        score = (path_matches * 5000) + (matches * 1000) + density_bonus + quality_bonus + priority_bonus - rank
        if path_matches == 0 and priority_bonus == 0:
            score -= 2000
        return score

    strong_docs = [doc for doc in docs if is_strong(doc)]
    weak_docs = [doc for doc in docs if doc not in strong_docs]

    ordered: list = []
    ordered.extend(sorted(strong_docs, key=doc_score, reverse=True))
    if len(ordered) < limit:
        ordered.extend(sorted(weak_docs, key=doc_score, reverse=True))

    # Deduplicación por similitud de contenido: chunks casi idénticos
    # (de documentos duplicados entre regiones, ej. 008/055) aportan redundancia.
    # Se conserva el de mayor score y se reemplaza el duplicado por el siguiente
    # candidato con contenido diferente.
    _SIMILARITY_THRESHOLD = 0.6
    selected: list = []
    selected_tokens: list[set[str]] = []
    for doc in ordered:
        if len(selected) >= limit:
            break
        tokens = set(re.findall(r"\w{3,}", (doc.page_content or "").lower()))
        if not tokens:
            selected.append(doc)
            selected_tokens.append(tokens)
            continue
        is_dup = False
        for prev_tokens in selected_tokens:
            if not prev_tokens:
                continue
            intersection = len(tokens & prev_tokens)
            union = len(tokens | prev_tokens)
            if union > 0 and intersection / union > _SIMILARITY_THRESHOLD:
                is_dup = True
                break
        if not is_dup:
            selected.append(doc)
            selected_tokens.append(tokens)

    return selected


# ── Carga de chunks por source ──────────────────────────────────────────────

def _load_source_records(source: str) -> List[Tuple[str, Dict[str, Any]]]:
    if source in _SOURCE_CHUNK_CACHE:
        return _SOURCE_CHUNK_CACHE[source]
    if _COLLECTION is None:
        return []

    records = _COLLECTION.get(where={"source": {"$eq": source}}, include=["documents", "metadatas"])
    documents = records.get("documents") or []
    metadatas = records.get("metadatas") or []

    prepared: List[Tuple[str, Dict[str, Any]]] = []
    for text, metadata in zip(documents, metadatas):
        meta_copy: Dict[str, Any] = dict(metadata or {})
        meta_copy["source"] = meta_copy.get("source") or source
        prepared.append((text or "", meta_copy))

    _SOURCE_CHUNK_CACHE[source] = prepared
    return prepared


# ── Augmentación por coincidencia de ruta ───────────────────────────────────

def _augment_with_source_candidates(
    docs: List[Any],
    keyword_forms: dict[str, set[str]],
    required_matches: int,
    allowed_sources: set[str] | None = None,
) -> int:
    if not keyword_forms or not SOURCE_TOKEN_INDEX:
        return 0

    candidate_tokens: List[str] = []
    seen_tokens: set[str] = set()
    for keyword, forms in keyword_forms.items():
        for token in {keyword, *forms}:
            token_lower = token.lower()
            if token_lower in seen_tokens:
                continue
            if token_lower in SOURCE_TOKEN_INDEX:
                candidate_tokens.append(token_lower)
                seen_tokens.add(token_lower)
            elif token_lower.endswith("s") and token_lower[:-1] in SOURCE_TOKEN_INDEX:
                base = token_lower[:-1]
                if base not in seen_tokens:
                    candidate_tokens.append(base)
                    seen_tokens.add(base)
        if len(candidate_tokens) >= SOURCE_FALLBACK_TOKEN_LIMIT:
            break

    # Matching por prefijo: keywords truncadas (ej. "instala") deben
    # encontrar tokens indexados completos (ej. "instalacion").
    # Sin esto, el augmentation no puede encontrar documentos con
    # "instalación" en el path cuando la keyword es "instala".
    _PREFIX_MIN_LEN = 5
    for keyword, forms in keyword_forms.items():
        for form in {keyword, *forms}:
            form_lower = form.lower()
            if len(form_lower) < _PREFIX_MIN_LEN:
                continue
            for indexed_token in SOURCE_TOKEN_INDEX:
                if indexed_token.startswith(form_lower) and indexed_token not in seen_tokens:
                    candidate_tokens.append(indexed_token)
                    seen_tokens.add(indexed_token)

    # Probar concatenaciones de keywords adyacentes para detectar nombres de
    # archivo donde las palabras van unidas sin separador (p. ej. 'smartpayout'
    # cuando el usuario escribe 'smart payout' con espacio).
    keywords_list = list(keyword_forms.keys())
    for i in range(len(keywords_list) - 1):
        kw_a = keywords_list[i]
        kw_b = keywords_list[i + 1]
        for compound in (kw_a + kw_b, kw_b + kw_a):
            compound_lower = compound.lower()
            if compound_lower not in seen_tokens and compound_lower in SOURCE_TOKEN_INDEX:
                candidate_tokens.append(compound_lower)
                seen_tokens.add(compound_lower)
        if len(candidate_tokens) >= SOURCE_FALLBACK_TOKEN_LIMIT:
            break

    if not candidate_tokens:
        return 0

    # ── Selección de fuentes por scoring multi-token ────────────────────
    # En lugar de iterar token por token (donde un token genérico agota
    # todas las plazas), contamos cuántos tokens candidatos matchean en
    # cada fuente. Fuentes con más tokens coincidentes son más relevantes.
    # Ej: "013 instalación mdc magic panama.pdf" matchea "instalacion" Y
    # "mdc" → score=2, mientras "nt210 ... ccm.html" solo matchea "ccm" → score=1.
    source_scores: Dict[str, int] = {}
    for token in candidate_tokens:
        for source in SOURCE_TOKEN_INDEX.get(token, ()):
            source_scores[source] = source_scores.get(source, 0) + 1

    existing_sources = {
        _normalize_source_input(doc.metadata.get("source"))
        for doc in docs
        if doc.metadata.get("source")
    }

    # Ordenar por score (más tokens coincidentes primero), luego por nombre.
    # Procesar primero fuentes ya presentes en K=20 (supplemental) para que
    # obtengan su chunk extra antes de que se agoten los slots.
    # Filtrar por fuentes permitidas (filtro nativo de rutas).
    if allowed_sources is not None:
        source_scores = {s: sc for s, sc in source_scores.items() if s in allowed_sources}

    candidate_sources = sorted(
        source_scores.keys(),
        key=lambda s: (
            0 if _normalize_source_input(s) in existing_sources else 1,
            -source_scores[s],
            s,
        ),
    )[:SOURCE_FALLBACK_TOKEN_LIMIT]

    if not candidate_sources:
        return 0

    # Prefijos de contenido ya incluido para evitar duplicados.
    existing_prefixes = {doc.page_content[:100] for doc in docs if doc.page_content}

    appended = 0
    for source in candidate_sources:
        is_existing = source in existing_sources
        # Fuentes nuevas: hasta SOURCE_FALLBACK_DOCS_PER_SOURCE chunks.
        # Fuentes ya presentes en K=20: hasta 1 chunk suplementario
        # (permite encontrar contenido diferente dentro del mismo documento).
        max_for_source = 1 if is_existing else SOURCE_FALLBACK_DOCS_PER_SOURCE

        records = _load_source_records(source)
        if not records:
            continue

        candidates: List[Tuple[_InlineDoc, int, int]] = []
        for text, metadata in records:
            # Saltar chunks cuyo contenido ya está en la lista.
            if text[:100] in existing_prefixes:
                continue
            document = _InlineDoc(content=text, metadata=dict(metadata))
            matches = _keyword_match_count(document, keyword_forms)
            path_matches = _source_keyword_match_count(document, keyword_forms)
            if matches == 0 and path_matches == 0:
                continue
            if matches < max(1, required_matches - 1) and path_matches == 0:
                continue
            candidates.append((document, matches, path_matches))

        if not candidates:
            continue

        candidates.sort(
            key=lambda entry: (
                entry[2],  # path matches first
                entry[1],  # total keyword matches
                # Densidad sobre contenido útil: descontar descripciones de
                # imagen para que chunks con texto técnico + imagen larga no
                # queden penalizados frente a chunks de texto puro.
                entry[1] / max(
                    int(len(entry[0].page_content or "") * _useful_content_ratio(entry[0])),
                    1,
                ),
            ),
            reverse=True,
        )

        added_for_source = 0
        for document, matches, path_matches in candidates[:max_for_source]:
            document.metadata["_retrieval_rank"] = len(docs)
            document.metadata["_keyword_matches"] = matches
            document.metadata["_path_keyword_matches"] = path_matches
            docs.append(document)
            existing_prefixes.add(document.page_content[:100])
            appended += 1
            added_for_source += 1
            if appended >= SOURCE_FALLBACK_MAX_DOCS:
                break

        if added_for_source:
            existing_sources.add(source)

        if appended >= SOURCE_FALLBACK_MAX_DOCS:
            break

    return appended


# ── Augmentación por código compuesto (ej. PNM-P01) ────────────────────────

# Límite de chunks a recuperar por código compuesto.
_COMPOUND_CODE_LIMIT = 12

# Patrón para detectar códigos compuestos con guiones.
_RE_COMPOUND_CODE_RET = re.compile(
    r"\b[a-z0-9]{2,}(?:-[a-z0-9]{1,})+\b",
    re.IGNORECASE,
)


def _augment_with_compound_code_search(
    docs: List[Any],
    keyword_forms: dict[str, set[str]],
    raw_query: str,
    allowed_sources: set[str] | None = None,
) -> int:
    """Busca chunks que contienen códigos compuestos directamente en Chroma.

    Los códigos técnicos compuestos (ej. PNM-P01, BNR-S01) suelen no aparecer
    en los primeros resultados de la búsqueda vectorial porque el modelo de
    embeddings no da suficiente peso a tokens alfanuméricos cortos.

    Esta función extrae los códigos del query original (preservando mayúsculas)
    y hace una búsqueda directa por contenido con ``$contains`` en la
    colección Chroma.  Solo actúa cuando ningún doc existente contiene el
    código, evitando trabajo redundante.
    """
    if _COLLECTION is None:
        return 0

    codes = _RE_COMPOUND_CODE_RET.findall(raw_query)
    if not codes:
        return 0

    existing_contents: set[str] = {doc.page_content or "" for doc in docs}
    appended = 0

    for code in codes:
        code_lower = code.lower()

        # Si algún doc ya contiene el código, no hace falta buscar más.
        if any(code_lower in (doc.page_content or "").lower() for doc in docs):
            continue

        # Probar variantes de case: original del usuario, UPPER, lower.
        # Chroma $contains es case-sensitive.
        variants = list(dict.fromkeys([code, code.upper(), code.lower()]))

        found_records: List[Tuple[str, Dict[str, Any]]] = []
        for variant in variants:
            try:
                _get_kwargs: dict = {
                    "where_document": {"$contains": variant},
                    "include": ["documents", "metadatas"],
                    "limit": _COMPOUND_CODE_LIMIT,
                }
                if allowed_sources is not None:
                    _get_kwargs["where"] = {"source": {"$in": list(allowed_sources)}}
                results = _COLLECTION.get(**_get_kwargs)
            except Exception:
                continue

            result_docs = results.get("documents") or []
            result_metas = results.get("metadatas") or []
            for text, meta in zip(result_docs, result_metas):
                if text and text not in existing_contents:
                    found_records.append((text, meta or {}))
                    existing_contents.add(text)

            if found_records:
                break  # Encontrados con esta variante, no probar más.

        for text, metadata in found_records:
            document = _InlineDoc(content=text, metadata=dict(metadata))
            matches = _keyword_match_count(document, keyword_forms)
            path_matches = _source_keyword_match_count(document, keyword_forms)
            document.metadata["_retrieval_rank"] = len(docs)
            document.metadata["_keyword_matches"] = matches
            document.metadata["_path_keyword_matches"] = path_matches
            docs.append(document)
            appended += 1

    if appended:
        logger.info(
            "Añadidos %d fragmentos por búsqueda directa de código compuesto.",
            appended,
        )

    return appended


# ── Augmentación por keywords prioritarios ──────────────────────────────────

def _augment_with_priority_keyword_search(
    docs: List[Any],
    keyword_forms: dict[str, set[str]],
    allowed_sources: set[str] | None = None,
) -> int:
    """Fuerza la inclusión de docs cuya ruta contiene un keyword prioritario.

    Para términos muy específicos (ej. 'sas', 'soja', 'ticketserver') que el
    modelo de embeddings infrapondera, este paso busca directamente en
    SOURCE_TOKEN_INDEX todas las fuentes cuyo nombre/ruta contiene el término
    y las añade al pool de recuperación.

    Solo actúa cuando el keyword prioritario está en SOURCE_TOKEN_INDEX, es
    decir, cuando hay al menos un documento indexado con ese término en su ruta.
    """
    if not keyword_forms or not SOURCE_TOKEN_INDEX:
        return 0

    priority_tokens = [
        kw for kw in keyword_forms
        if kw in PRIORITY_KEYWORDS and kw in SOURCE_TOKEN_INDEX
    ]
    if not priority_tokens:
        return 0

    existing_sources = {
        _normalize_source_input(doc.metadata.get("source"))
        for doc in docs
        if doc.metadata.get("source")
    }

    appended = 0
    for token in priority_tokens:
        for source in SOURCE_TOKEN_INDEX.get(token, ()):
            if source in existing_sources:
                continue
            if allowed_sources is not None and source not in allowed_sources:
                continue
            records = _load_source_records(source)
            if not records:
                continue

            candidates: List[Tuple[_InlineDoc, int, int]] = []
            for text, metadata in records:
                document = _InlineDoc(content=text, metadata=dict(metadata))
                matches = _keyword_match_count(document, keyword_forms)
                path_matches = _source_keyword_match_count(document, keyword_forms)
                if matches + path_matches == 0:
                    continue
                candidates.append((document, matches, path_matches))

            if not candidates:
                continue

            candidates.sort(
                key=lambda e: (e[2], e[1], e[1] / max(len(e[0].page_content or ""), 1)),
                reverse=True,
            )

            for document, matches, path_matches in candidates[:SOURCE_FALLBACK_DOCS_PER_SOURCE]:
                document.metadata["_retrieval_rank"] = len(docs)
                document.metadata["_keyword_matches"] = matches
                document.metadata["_path_keyword_matches"] = path_matches
                docs.append(document)
                appended += 1

            existing_sources.add(source)

    if appended:
        logger.info("Añadidos %d fragmentos por keyword prioritario en ruta.", appended)

    return appended


# ── Fallback literal por keyword en contenido ──────────────────────────────

# Tope de chunks recuperados por cada keyword en la búsqueda literal.
_LITERAL_KEYWORD_LIMIT_PER_TERM = 10
# Tope total de chunks añadidos por esta función en una sola llamada.
_LITERAL_KEYWORD_MAX_APPEND = 20
# Longitud mínima de keyword para activar el fallback literal.
# Términos cortos producen demasiados falsos positivos por substring.
_LITERAL_KEYWORD_MIN_LEN = 4


def _augment_with_literal_keyword_search(
    docs: List[Any],
    keyword_forms: dict[str, set[str]],
    allowed_sources: set[str] | None = None,
) -> int:
    """Fallback de último recurso: busca chunks que contienen literalmente una keyword.

    Diseñado para palabras raras (jerga, neologismos, nombres propios) que el
    modelo de embeddings multilingüe infrapondera y que por tanto no aparecen
    en el top-K vectorial, aunque estén presentes literalmente en algún chunk.

    Garantías de no-regresión (ver llamador en endpoint.py):
    - Solo se invoca cuando el flujo principal no produjo ningún doc útil
      (lista vacía o sin keyword matches). Si hay docs con matches, no se llama.
    - Respeta `allowed_sources` (filtrado por permisos del usuario) usando el
      mismo mecanismo que `_augment_with_compound_code_search`.
    - Filtra keywords cortas (< _LITERAL_KEYWORD_MIN_LEN) y stopwords para
      evitar falsos positivos por substring (ej. 'sas' dentro de 'casas').
    - Tope total de _LITERAL_KEYWORD_MAX_APPEND chunks; el ranking final lo
      hace `_prioritize_docs` con MAX_CONTEXT_DOCS, así que el LLM solo ve
      los mejores.
    """
    if _COLLECTION is None or not keyword_forms:
        return 0

    # Filtrar keywords elegibles: longitud mínima y no stopwords.
    eligible_keywords = [
        kw for kw in keyword_forms
        if len(kw) >= _LITERAL_KEYWORD_MIN_LEN and kw not in STOPWORDS
    ]
    if not eligible_keywords:
        return 0

    existing_contents: set[str] = {doc.page_content or "" for doc in docs}
    appended = 0

    for keyword in eligible_keywords:
        if appended >= _LITERAL_KEYWORD_MAX_APPEND:
            break

        # Probar variantes de case (Chroma $contains es case-sensitive).
        variants = list(dict.fromkeys([keyword, keyword.lower(), keyword.title(), keyword.upper()]))

        found_records: List[Tuple[str, Dict[str, Any]]] = []
        for variant in variants:
            try:
                _get_kwargs: dict = {
                    "where_document": {"$contains": variant},
                    "include": ["documents", "metadatas"],
                    "limit": _LITERAL_KEYWORD_LIMIT_PER_TERM,
                }
                if allowed_sources is not None:
                    _get_kwargs["where"] = {"source": {"$in": list(allowed_sources)}}
                results = _COLLECTION.get(**_get_kwargs)
            except Exception as exc:
                logger.debug("[literal-keyword] error buscando '%s': %s", variant, exc)
                continue

            result_docs = results.get("documents") or []
            result_metas = results.get("metadatas") or []
            for text, meta in zip(result_docs, result_metas):
                if text and text not in existing_contents:
                    found_records.append((text, meta or {}))
                    existing_contents.add(text)

            if found_records:
                break  # Encontrado con esta variante; no probar las demás.

        for text, metadata in found_records:
            document = _InlineDoc(content=text, metadata=dict(metadata))
            matches = _keyword_match_count(document, keyword_forms)
            path_matches = _source_keyword_match_count(document, keyword_forms)
            document.metadata["_retrieval_rank"] = len(docs)
            document.metadata["_keyword_matches"] = matches
            document.metadata["_path_keyword_matches"] = path_matches
            docs.append(document)
            appended += 1
            if appended >= _LITERAL_KEYWORD_MAX_APPEND:
                break

    if appended:
        logger.info(
            "[literal-keyword] Añadidos %d fragmentos por búsqueda literal de keywords %s.",
            appended, eligible_keywords,
        )

    return appended
