import os

from ingesta.query_core import (
    create_embedding_model,
    open_vectorstore,
    get_db_path,
    normalize_and_expand_query,
)

from env_loader import load_host_env


def main() -> None:
    load_host_env()

    embedding = create_embedding_model()
    db = open_vectorstore(get_db_path(), embedding)

    query = "ordenadores de barra"
    normalized = normalize_and_expand_query(query)
    print("Normalized query:", normalized)

    results = db.similarity_search_with_score(normalized, k=8)
    if not results:
        print("No results for normalized query; retrying literal query...\n")
        results = db.similarity_search_with_score(query, k=8)

    for idx, (doc, score) in enumerate(results, start=1):
        text = (doc.page_content or "")
        lower = text.lower()
        contains = "ordenadores de barra" in lower
        print(f"\nResult {idx} | score={score:.4f} | contains_phrase={contains}")
        print("Source:", doc.metadata.get("source"))
        if contains:
            pos = lower.index("ordenadores de barra")
            snippet = text[max(0, pos - 120) : pos + 120]
        else:
            snippet = text[:240]
        print(snippet.replace("\n", " "))


if __name__ == "__main__":
    main()
