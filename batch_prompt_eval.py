from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Dict, Any, List, Tuple

import langchain

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

from env_loader import load_host_env


def extract_keywords(text: str) -> list[str]:
    import re

    keywords: list[str] = []
    seen: set[str] = set()
    for token in re.findall(r"\w+", text.lower()):
        if len(token) < 4 or token in seen:
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


def _load_source_records(source: str, cache: Dict[str, List[Tuple[str, Dict[str, Any]]]], collection) -> List[Tuple[str, Dict[str, Any]]]:
    if source in cache:
        return cache[source]
    if collection is None:
        return []

    records = collection.get(where={"source": {"$eq": source}}, include=["documents", "metadatas"])
    documents = records.get("documents") or []
    metadatas = records.get("metadatas") or []

    prepared: List[Tuple[str, Dict[str, Any]]] = []
    for text, metadata in zip(documents, metadatas):
        meta_copy: Dict[str, Any] = dict(metadata or {})
        meta_copy["source"] = meta_copy.get("source") or source
        prepared.append((text or "", meta_copy))

    cache[source] = prepared
    return prepared


class _InlineDoc:
    def __init__(self, content: str, metadata: Dict[str, Any]):
        self.page_content = content
        self.metadata = metadata


def _augment_with_source_candidates(
    documentos: List[Any],
    keyword_forms: dict[str, set[str]],
    required_matches: int,
    source_token_index,
    cache,
    collection,
) -> int:
    SOURCE_FALLBACK_TOKEN_LIMIT = 12
    SOURCE_FALLBACK_DOCS_PER_SOURCE = 2
    SOURCE_FALLBACK_MAX_DOCS = 12

    if not keyword_forms or not source_token_index:
        return 0

    candidate_tokens: List[str] = []
    seen_tokens: set[str] = set()
    for keyword, forms in keyword_forms.items():
        for token in {keyword, *forms}:
            token_lower = token.lower()
            if token_lower in seen_tokens:
                continue
            if token_lower in source_token_index:
                candidate_tokens.append(token_lower)
                seen_tokens.add(token_lower)
            elif token_lower.endswith("s") and token_lower[:-1] in source_token_index:
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
        for source in source_token_index.get(token, ()):  # type: ignore[arg-type]
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

        records = _load_source_records(source, cache, collection)
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


def main(limit: int | None = None) -> None:
    load_host_env()

    if not hasattr(langchain, "debug"):
        langchain.debug = False  # type: ignore[attr-defined]

    try:
        db_path = get_db_path()
    except RuntimeError as exc:
        raise SystemExit(f"ERROR: {exc}")

    embedding_model = create_embedding_model()

    try:
        db = open_vectorstore(db_path, embedding_model)
    except Exception as exc:
        raise SystemExit(
            "ERROR: La base de datos no existe o no es compatible. Ejecuta 'ingesta_masiva.py' primero.\n"
            f"Detalle técnico: {exc}"
        )

    collection = get_chroma_collection(db)
    source_token_index = build_source_token_index(collection)
    source_chunk_cache: Dict[str, List[Tuple[str, Dict[str, Any]]]] = {}

    try:
        llm = ensure_gemini_model(MODELO_CONSULTA)
    except RuntimeError as exc:
        raise SystemExit(f"ERROR: {exc}")

    retriever = db.as_retriever(search_kwargs={"k": 20})
    fallback_k = 150
    target_keyword_docs = 2

    if SPACY_AVAILABLE:
        print("Lematizador spaCy activo para normalizar consultas.")
    elif SPACY_DISABLED:
        print("spaCy deshabilitado por SMIRAG_DISABLE_SPACY; se aplican heurísticas básicas.")
    else:
        print("spaCy no está instalado; se aplican heurísticas básicas de lematización.")

    prompt_path = Path("prompt.txt")
    if not prompt_path.exists():
        raise SystemExit("No se encuentra prompt.txt en el directorio raíz.")

    questions: List[str] = []
    for raw_line in prompt_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line:
            continue
        if ". " in line:
            parts = line.split(". ", 1)
            try:
                int(parts[0])
                line = parts[1]
            except ValueError:
                line = raw_line
        questions.append(line.strip())

    if limit is not None:
        questions = questions[:limit]

    results: List[Dict[str, Any]] = []

    for idx, question in enumerate(questions, start=1):
        print(f"Procesando pregunta {idx}/{len(questions)}...")
        normalized_question = normalize_and_expand_query(question)
        keywords = extract_keywords(normalized_question)
        keyword_forms = {keyword: _keyword_forms(keyword) for keyword in keywords}
        required_matches = 1 if len(keywords) <= 1 else 2

        documentos = retriever.invoke(normalized_question)
        if not documentos:
            documentos = retriever.invoke(question)

        for doc_idx, doc in enumerate(documentos):
            doc.metadata["_retrieval_rank"] = doc_idx
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
            target_keyword_docs_local = min(target_keyword_docs, len(keyword_forms) or 1)
            qualified = sum(1 for doc in documentos if is_strong(doc))
            if qualified < target_keyword_docs_local:
                extra_docs = db.similarity_search(question, k=fallback_k)
                existing_signatures = {
                    (
                        doc.metadata.get("source"),
                        doc.metadata.get("source_hash"),
                        doc.page_content,
                    )
                    for doc in documentos
                }
                for extra_idx, extra_doc in enumerate(extra_docs):
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
                    extra_doc.metadata["_retrieval_rank"] = len(documentos) + extra_idx
                    extra_doc.metadata["_keyword_matches"] = matches
                    extra_doc.metadata["_path_keyword_matches"] = path_matches
                    documentos.append(extra_doc)
                    existing_signatures.add(signature)
                    appended += 1
                    qualified += 1
                    if qualified >= target_keyword_docs_local:
                        break

        appended_by_source = 0
        if keywords:
            appended_by_source = _augment_with_source_candidates(
                documentos,
                keyword_forms,
                required_matches,
                source_token_index,
                source_chunk_cache,
                collection,
            )

        selected_docs = documentos
        if keywords:
            strong_docs = [doc for doc in documentos if is_strong(doc)]
            if strong_docs:
                selected_docs = strong_docs

        grupos = group_docs_by_source(selected_docs)
        if grupos:
            mejor_fuente, docs_seleccionados = max(grupos.items(), key=lambda item: score_docs(item[1]))
        else:
            mejor_fuente, docs_seleccionados = None, []

        documentos_usados = docs_seleccionados if docs_seleccionados else selected_docs or documentos
        contexto = build_context(documentos_usados)

        prompt_text = PROMPT_TEMPLATE.format(context=contexto, question=question)
        try:
            respuesta = llm.generate_content(prompt_text)
            texto_respuesta = getattr(respuesta, "text", None) or str(respuesta)
        except Exception as exc:
            texto_respuesta = f"ERROR: Falló la invocación a Gemini: {exc}"

        fuentes = sorted({str(doc.metadata.get("source", "")) for doc in documentos_usados})
        fragmentos = [doc.page_content[:200].replace("\n", " ") for doc in documentos_usados]

        results.append(
            {
                "question": question,
                "answer": texto_respuesta,
                "sources": fuentes,
                "documents_used": len(documentos_usados),
                "appended_fallback": appended,
                "appended_by_source": appended_by_source,
                "fragments": fragmentos,
            }
        )

    output_lines = ["# Resultados de Consultas\n"]
    for idx, result in enumerate(results, start=1):
        output_lines.append(f"## {idx}. {result['question']}\n")
        output_lines.append("**Respuesta:**\n")
        output_lines.append(f"{result['answer']}\n")
        output_lines.append("**Fuentes:** " + (", ".join(result["sources"]) or "(sin fuentes)") + "\n")
        output_lines.append(f"**Fragmentos utilizados:** {result['documents_used']}\n")
        if result["appended_fallback"]:
            output_lines.append(f"**Fragmentos añadidos por similitud:** {result['appended_fallback']}\n")
        if result["appended_by_source"]:
            output_lines.append(f"**Fragmentos añadidos por ruta:** {result['appended_by_source']}\n")
        output_lines.append("\n")

    output_path = Path("prompt_responses.md")
    output_path.write_text("\n".join(output_lines), encoding="utf-8")

    jsonl_path = Path("prompt_responses.jsonl")
    with jsonl_path.open("w", encoding="utf-8") as fh:
        for result in results:
            fh.write(json.dumps(result, ensure_ascii=False) + "\n")

    print("Listo. Resultados guardados en", output_path, "y", jsonl_path)


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Evalúa las preguntas de prompt.txt")
    parser.add_argument("--limit", type=int, default=None, help="Número máximo de preguntas a procesar")
    args = parser.parse_args()

    main(limit=args.limit)
