import langchain
import os
import re
from typing import Dict, Any, List, Tuple

from env_loader import load_host_env

from ingesta.query_core import (
    MODELO_CONSULTA,
    PROMPT_TEMPLATE,
    build_context,
    create_embedding_model,
    ensure_gemini_model,
    get_db_path,
    normalize_and_expand_query,
    open_vectorstore,
    SPACY_AVAILABLE,
    SPACY_DISABLED,
)
from ingesta.utils import build_source_token_index, get_chroma_collection

load_host_env()

if not hasattr(langchain, "debug"):
    langchain.debug = False  # type: ignore[attr-defined]

try:
    DB_PATH = get_db_path()
except RuntimeError as exc:
    print(f"ERROR: {exc}")
    exit()

embedding_model = create_embedding_model()

try:
    db = open_vectorstore(DB_PATH, embedding_model)
except Exception as exc:
    print("ERROR: La base de datos no existe o no es compatible. Ejecuta 'ingesta_masiva.py' primero.")
    print(f"Detalle técnico: {exc}")
    exit()

_COLLECTION = get_chroma_collection(db)
SOURCE_TOKEN_INDEX = build_source_token_index(_COLLECTION)
_SOURCE_CHUNK_CACHE: Dict[str, List[Tuple[str, Dict[str, Any]]]] = {}
SOURCE_FALLBACK_TOKEN_LIMIT = 12
SOURCE_FALLBACK_DOCS_PER_SOURCE = 2
SOURCE_FALLBACK_MAX_DOCS = 12


class _InlineDoc:
    """Contenedor ligero de documento para heurísticas adicionales."""

    def __init__(self, content: str, metadata: Dict[str, Any]):
        self.page_content = content
        self.metadata = metadata

try:
    llm = ensure_gemini_model(MODELO_CONSULTA)
except RuntimeError as exc:
    print(f"ERROR: {exc}")
    exit()

RETRIEVAL_K = 20
FALLBACK_K = 150
TARGET_KEYWORD_DOCS = 2

retriever = db.as_retriever(search_kwargs={"k": RETRIEVAL_K})

if SPACY_AVAILABLE:
    print("Lematizador spaCy activo para normalizar consultas.")
elif SPACY_DISABLED:
    print("spaCy deshabilitado por SMIRAG_DISABLE_SPACY; se aplican heurísticas básicas.")
else:
    print("spaCy no está instalado; se aplican heurísticas básicas de lematización.")


STOPWORDS = {
    "el",
    "la",
    "los",
    "las",
    "un",
    "una",
    "unos",
    "unas",
    "lo",
    "al",
    "del",
    "de",
    "que",
    "y",
    "o",
    "u",
    "a",
    "en",
    "con",
    "por",
    "para",
    "como",
    "se",
    "es",
    "son",
    "ser",
    "soy",
    "eres",
    "somos",
    "estoy",
    "estas",
    "esta",
    "está",
    "están",
    "esta",
    "estamos",
    "estan",
    "hay",
    "habia",
    "había",
    "tenia",
    "tenía",
    "tengo",
    "tienes",
    "tienen",
    "hacer",
    "hace",
    "hacen",
    "si",
    "no",
    "mas",
    "más",
    "menos",
    "pero",
    "sin",
    "sobre",
    "entre",
    "cuanto",
    "cuánto",
    "cual",
    "cuál",
    "cuales",
    "cuáles",
    "donde",
    "dónde",
    "cuando",
    "cuándo",
    "quien",
    "quién",
    "quienes",
    "quiénes",
    "que",
    "qué",
    "este",
    "esta",
    "estos",
    "estas",
    "ese",
    "esa",
    "eso",
    "esos",
    "esas",
    "aquel",
    "aquella",
    "aquellos",
    "aquellas",
    "mi",
    "mis",
    "tu",
    "tus",
    "su",
    "sus",
    "nuestro",
    "nuestra",
    "nuestros",
    "nuestras",
    "vuestro",
    "vuestra",
    "vuestros",
    "vuestras",
    "yo",
    "tu",
    "tú",
    "usted",
    "ustedes",
    "nosotros",
    "nosotras",
    "vosotros",
    "vosotras",
    "ellos",
    "ellas",
    "me",
    "te",
    "se",
    "nos",
    "les",
    "le",
    "este",
    "esta",
    "etc",
    "etcetera",
    "etcétera",
}


def extract_keywords(text: str) -> list[str]:
    keywords: list[str] = []
    seen: set[str] = set()
    for token in re.findall(r"\w+", text.lower()):
        if len(token) < 4 or token in seen or token in STOPWORDS:
            continue
        keywords.append(token)
        seen.add(token)
    filtered: list[str] = []
    for token in keywords:
        if any(len(other) > len(token) and token in other for other in keywords):
            continue
        filtered.append(token)
    return filtered


def _keyword_forms(keyword: str) -> set[str]:
    forms = {keyword}
    if len(keyword) >= 5:
        forms.add(keyword[:-1])
    if keyword.endswith("es") and len(keyword) >= 6:
        forms.add(keyword[:-2])
    if keyword.endswith("os") or keyword.endswith("as"):
        stem = keyword[:-2]
        if len(stem) >= 3:
            forms.add(stem)
    return {form for form in forms if len(form) >= 3}


def keyword_match_count(doc, keyword_forms: dict[str, set[str]]) -> int:
    if not keyword_forms:
        return 0
    text = (doc.page_content or "").lower()
    return sum(
        1
        for forms in keyword_forms.values()
        if any(form in text for form in forms)
    )


def source_keyword_match_count(doc, keyword_forms: dict[str, set[str]]) -> int:
    if not keyword_forms:
        return 0
    source = str(doc.metadata.get("source", "")).lower()
    return sum(
        1
        for forms in keyword_forms.values()
        if any(form in source for form in forms)
    )

def group_docs_by_source(docs):
    groups = {}
    for doc in docs:
        source = doc.metadata.get("source", "(origen desconocido)")
        groups.setdefault(source, []).append(doc)
    return groups


def score_docs(docs):
    # Prioriza el contenido con más longitud y refuerza coincidencias de palabras clave y ruta.
    total = 0.0
    for doc in docs:
        rank = doc.metadata.get("_retrieval_rank", 0)
        matches = doc.metadata.get("_keyword_matches", 0)
        path_matches = doc.metadata.get("_path_keyword_matches", 0)
        weight = 1.0 / (rank + 1)
        boost = 1.0 + (0.4 * matches) + (1.5 * path_matches)
        if path_matches == 0:
            boost *= 0.4
        total += weight * len(doc.page_content or "") * boost
    return total


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


def _augment_with_source_candidates(
    documentos: List[Any],
    keyword_forms: dict[str, set[str]],
    required_matches: int,
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

    if not candidate_tokens:
        return 0

    candidate_sources: List[str] = []
    seen_sources: set[str] = set()
    for token in candidate_tokens:
        for source in SOURCE_TOKEN_INDEX.get(token, ()):  # type: ignore[arg-type]
            if source in seen_sources:
                continue
            seen_sources.add(source)
            candidate_sources.append(source)
            if len(candidate_sources) >= SOURCE_FALLBACK_TOKEN_LIMIT:
                break
        if len(candidate_sources) >= SOURCE_FALLBACK_TOKEN_LIMIT:
            break

    if not candidate_sources:
        return 0

    existing_sources = {
        os.path.normcase(os.path.abspath(doc.metadata.get("source")))
        for doc in documentos
        if doc.metadata.get("source")
    }

    appended = 0
    for source in candidate_sources:
        if source in existing_sources:
            continue

        records = _load_source_records(source)
        if not records:
            continue

        candidates: List[Tuple[_InlineDoc, int, int]] = []
        for text, metadata in records:
            document = _InlineDoc(content=text, metadata=dict(metadata))
            matches = keyword_match_count(document, keyword_forms)
            path_matches = source_keyword_match_count(document, keyword_forms)
            if matches == 0 and path_matches == 0:
                continue
            if matches < max(1, required_matches - 1) and path_matches == 0:
                continue
            candidates.append((document, matches, path_matches))

        if not candidates:
            continue

        candidates.sort(
            key=lambda entry: (
                entry[2],
                entry[1],
                len(entry[0].page_content or ""),
            ),
            reverse=True,
        )

        added_for_source = 0
        for document, matches, path_matches in candidates[:SOURCE_FALLBACK_DOCS_PER_SOURCE]:
            document.metadata["_retrieval_rank"] = len(documentos)
            document.metadata["_keyword_matches"] = matches
            document.metadata["_path_keyword_matches"] = path_matches
            documentos.append(document)
            appended += 1
            added_for_source += 1
            if appended >= SOURCE_FALLBACK_MAX_DOCS:
                break

        if added_for_source:
            existing_sources.add(source)

        if appended >= SOURCE_FALLBACK_MAX_DOCS:
            break

    return appended

print(f"Sistema de consulta {MODELO_CONSULTA} listo. Escribe tu pregunta.")

while True:
    pregunta = input("\nTú: ")
    if pregunta.lower() in ['salir', 'exit', 'quit']:
        break

    normalized_question = normalize_and_expand_query(pregunta)
    keywords = extract_keywords(normalized_question)
    keyword_forms = {keyword: _keyword_forms(keyword) for keyword in keywords}
    required_matches = 1 if len(keywords) <= 1 else 2

    documentos = retriever.invoke(normalized_question)
    if not documentos:
        documentos = retriever.invoke(pregunta)

    for idx, doc in enumerate(documentos):
        doc.metadata["_retrieval_rank"] = idx
        doc.metadata["_keyword_matches"] = keyword_match_count(doc, keyword_forms) if keywords else 0
        doc.metadata["_path_keyword_matches"] = source_keyword_match_count(doc, keyword_forms) if keywords else 0

    appended = 0

    def is_strong(doc) -> bool:
        matches = doc.metadata.get("_keyword_matches", 0)
        path_matches = doc.metadata.get("_path_keyword_matches", 0)
        if matches < required_matches:
            return False
        if path_matches > 0:
            return True
        needed = max(required_matches + 1, len(keyword_forms))
        return matches >= needed

    if keywords:
        target_keyword_docs = min(TARGET_KEYWORD_DOCS, len(keyword_forms) or 1)
        qualified = sum(1 for doc in documentos if is_strong(doc))
        if qualified < target_keyword_docs:
            extra_docs = db.similarity_search(pregunta, k=FALLBACK_K)
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
                matches = keyword_match_count(extra_doc, keyword_forms)
                if matches < required_matches:
                    continue
                path_matches = source_keyword_match_count(extra_doc, keyword_forms)
                if path_matches == 0 and matches < max(required_matches + 1, len(keyword_forms)):
                    continue
                extra_doc.metadata["_retrieval_rank"] = len(documentos) + idx_extra
                extra_doc.metadata["_keyword_matches"] = matches
                extra_doc.metadata["_path_keyword_matches"] = path_matches
                documentos.append(extra_doc)
                existing_signatures.add(signature)
                appended += 1
                qualified += 1
                if qualified >= target_keyword_docs:
                    break
    if appended:
        print(f"-> Añadidos {appended} fragmentos por coincidencia de palabras clave.")

    extra_by_source = _augment_with_source_candidates(documentos, keyword_forms, required_matches)
    if extra_by_source:
        print(f"-> Añadidos {extra_by_source} fragmentos por coincidencia de ruta.")

    selected_docs = documentos
    if keywords:
        strong_docs = [doc for doc in documentos if is_strong(doc)]
        if strong_docs:
            selected_docs = strong_docs

    if keywords and not any(
        doc.metadata.get("_keyword_matches", 0) > 0 or doc.metadata.get("_path_keyword_matches", 0) > 0
        for doc in selected_docs
    ):
        selected_docs = []

    grupos = group_docs_by_source(selected_docs)
    if grupos:
        mejor_fuente, docs_seleccionados = max(
            grupos.items(),
            key=lambda item: score_docs(item[1])
        )
    else:
        mejor_fuente, docs_seleccionados = None, []

    documentos_usados = docs_seleccionados if docs_seleccionados else selected_docs or []
    contexto = build_context(documentos_usados)

    prompt_text = PROMPT_TEMPLATE.format(context=contexto, question=pregunta)
    try:
        respuesta = llm.generate_content(prompt_text)
    except Exception as exc:
        print(f"ERROR: Falló la invocación a Gemini: {exc}")
        continue

    print("\n--- Respuesta de la IA ---")
    print(getattr(respuesta, "text", None) or respuesta)

    print("\n--- Fuentes (Incluye referencias a descripciones de imágenes) ---")
    if not documentos_usados:
        print("No se encontraron documentos relevantes.")
    else:
        fuente = mejor_fuente if mejor_fuente else documentos_usados[0].metadata.get("source")
        fragmento = documentos_usados[0].page_content[:150].replace("\n", " ")
        print(
            "Fuente principal: "
            f"{fuente} (fragmentos utilizados: {len(documentos_usados)})"
            f" (Fragmento: {fragmento}...)"
        )