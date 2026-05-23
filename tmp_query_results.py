import os

from ingesta.query_core import (
    create_embedding_model,
    open_vectorstore,
    get_db_path,
    normalize_and_expand_query,
)

from env_loader import load_host_env


def run(query: str) -> None:
    embedding = create_embedding_model()
    db = open_vectorstore(get_db_path(), embedding)

    norm = normalize_and_expand_query(query)
    print(f"Original query: {query}")
    print(f"Normalized query: {norm}\n")

    results = db.similarity_search_with_score(norm, k=120)
    if not results:
        print("No results for normalized query.")
        return

    for idx, (doc, score) in enumerate(results, start=1):
        text = doc.page_content or ""
        has_phrase = "ordenadores de barra" in text.lower()
        snippet = text[:200].replace("\n", " ")
        print(f"Result {idx:02d} | score={score:.4f} | has_phrase={has_phrase}")
        print(f"Source: {doc.metadata.get('source')}")
        if has_phrase:
            pos = text.lower().index("ordenadores de barra")
            around = text[max(0, pos - 120): pos + 120]
            print("Phrase context:")
            print(around.replace("\n", " "))
        else:
            print("Snippet:", snippet)
        print()


def main() -> None:
    load_host_env()
    run("ordenadores de barra")


if __name__ == "__main__":
    main()
