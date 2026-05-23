import os

from langchain_chroma import Chroma
from langchain_community.embeddings import HuggingFaceEmbeddings
from typing import List

from env_loader import load_host_env

load_host_env()


def _norm_path(path: str) -> str:
    return os.path.normcase(os.path.abspath(path))


def _resolve_db_path() -> str:
    db_path = os.environ.get("DB_PATH")
    if not db_path:
        raise RuntimeError("DB_PATH no está definido en el entorno ni en .env")
    if not os.path.isabs(db_path):
        db_path = os.path.join(os.path.dirname(__file__), db_path)
    return db_path


def _load_vectorstore(db_path: str) -> Chroma:
    embedding_model = HuggingFaceEmbeddings(
        model_name="paraphrase-multilingual-mpnet-base-v2",
        model_kwargs={"device": "cpu"},
    )
    return Chroma(persist_directory=db_path, embedding_function=embedding_model)


def _fetch_document_chunks(vectorstore: Chroma, source_path: str) -> list[str]:
    collection = getattr(vectorstore, "_collection", None)
    if collection is None:
        raise RuntimeError("No se pudo acceder a la colección interna de Chroma")

    normalized = _norm_path(source_path)
    result = collection.get(
        where={"source": {"$eq": normalized}},
        include=["documents"],
    )
    return result.get("documents") or []


def main() -> None:
    ruta = input("Ruta completa del PDF (exactamente como se indexó): ").strip()
    if not ruta:
        print("No se ingresó ninguna ruta.")
        return

    if not os.path.exists(ruta):
        print("Advertencia: la ruta indicada no existe en el sistema de archivos. Se buscará igualmente en Chroma.")

    db_path = _resolve_db_path()
    vectorstore = _load_vectorstore(db_path)
    chunks = _fetch_document_chunks(vectorstore, ruta)

    if not chunks:
        print("No se encontraron fragmentos asociados a la ruta proporcionada.")
        return

    proyecto_raiz = os.path.dirname(__file__)
    doc_path = os.path.join(proyecto_raiz, "doc.txt")

    normalized = _norm_path(ruta)
    try:
        with open(doc_path, "a", encoding="utf-8") as f:
            f.write(f"\n=== Fuente: {normalized} ===\n")
            for idx, chunk in enumerate(chunks, start=1):
                f.write(f"\n--- Fragmento {idx} ---\n{chunk}\n")
    except OSError as exc:
        print(f"No se pudo escribir en {doc_path}: {exc}")
        return

    # Se guarda silenciosamente; solo se muestran errores previos si los hay.


if __name__ == "__main__":
    main()
