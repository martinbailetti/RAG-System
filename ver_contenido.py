"""
Script para ver el contenido indexado de un archivo específico en Chroma.

Uso:
    python ver_contenido.py <ruta_del_archivo>

Ejemplo:
    python ver_contenido.py "C:\\Projects\\smidocs.root\\manual.pdf"
"""

import os
import sys
from typing import List, Tuple

from langchain_chroma import Chroma
from langchain_community.embeddings import HuggingFaceEmbeddings

from env_loader import load_host_env

load_host_env()

_db_path = os.environ.get("DB_PATH")
if not _db_path:
    raise RuntimeError("DB_PATH no está definido en el entorno ni en .env")

DB_PATH = (
    _db_path
    if os.path.isabs(_db_path)
    else os.path.join(os.path.dirname(__file__), _db_path)
)


def get_content_by_source(source_path: str) -> List[Tuple[str, dict]]:
    """
    Obtiene todos los chunks indexados para un archivo específico.
    
    Args:
        source_path: Ruta del archivo a buscar (puede ser parcial o completa)
    
    Returns:
        Lista de tuplas (contenido, metadatos) para cada chunk encontrado
    """
    embedding_model = HuggingFaceEmbeddings(
        model_name="paraphrase-multilingual-mpnet-base-v2",
        model_kwargs={"device": "cpu"},
    )
    vectorstore = Chroma(persist_directory=DB_PATH, embedding_function=embedding_model)
    collection = getattr(vectorstore, "_collection", None)
    if not collection:
        return []

    # Normalizar la ruta de búsqueda
    search_norm = os.path.normcase(source_path)
    
    results: List[Tuple[str, dict]] = []
    offset = 0
    batch_size = 500

    while True:
        try:
            res = collection.get(
                include=["metadatas", "documents"],
                limit=batch_size,
                offset=offset
            )
        except TypeError:
            res = collection.get(include=["metadatas", "documents"])
        
        metadatas = res.get("metadatas") or []
        documents = res.get("documents") or []
        
        if not metadatas:
            break
            
        for md, doc in zip(metadatas, documents):
            src = (md or {}).get("source", "")
            src_norm = os.path.normcase(src)
            
            # Coincidencia exacta o parcial (la ruta buscada está contenida)
            if search_norm in src_norm or src_norm.endswith(search_norm):
                results.append((doc, md))
        
        if len(metadatas) < batch_size:
            break
        offset += batch_size

    return results


def main():
    if len(sys.argv) < 2:
        print("Uso: python ver_contenido.py <ruta_del_archivo>")
        print("\nEjemplo:")
        print('  python ver_contenido.py "C:\\Projects\\smidocs.root\\manual.pdf"')
        print('  python ver_contenido.py manual.pdf')
        sys.exit(1)
    
    source_path = sys.argv[1]
    print(f"Buscando contenido para: {source_path}")
    print(f"Base de datos: {DB_PATH}")
    print("-" * 60)
    
    chunks = get_content_by_source(source_path)
    
    if not chunks:
        print(f"\nNo se encontraron chunks para: {source_path}")
        print("\nVerifica que la ruta sea correcta. Usa 'python listado.py' para ver los archivos indexados.")
        sys.exit(0)
    
    print(f"\nEncontrados {len(chunks)} chunk(s):\n")
    
    for idx, (content, metadata) in enumerate(chunks, start=1):
        print(f"{'='*60}")
        print(f"CHUNK {idx}")
        print(f"{'='*60}")
        print(f"Fuente: {metadata.get('source', 'N/A')}")
        if 'page' in metadata:
            print(f"Página: {metadata.get('page')}")
        if 'chunk_index' in metadata:
            print(f"Índice: {metadata.get('chunk_index')}")
        
        # Mostrar otros metadatos relevantes
        otros = {k: v for k, v in metadata.items() 
                 if k not in ('source', 'page', 'chunk_index')}
        if otros:
            print(f"Otros metadatos: {otros}")
        
        print("-" * 60)
        print("CONTENIDO:")
        print("-" * 60)
        print(content)
        print()


if __name__ == "__main__":
    main()
