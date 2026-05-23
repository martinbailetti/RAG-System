"""
force_reindex.py
Elimina de Chroma todos los chunks de uno o varios documentos (por ruta parcial),
forzando que la próxima ejecución de sync_ingesta.py los re-indexe desde cero.

Uso:
    python force_reindex.py "NT - COMO CONFIGURAR"
    python force_reindex.py "Manuales/NT" "otro_fragmento_de_ruta"
"""

import sys
from pathlib import Path

# Cargar .env antes de cualquier import del proyecto
from env_loader import load_host_env
load_host_env()

from ingesta.config import DB_PATH
from langchain_chroma import Chroma
from ingesta.config import init_embedding_model

def main():
    if len(sys.argv) < 2:
        print("Uso: python force_reindex.py <fragmento_de_ruta> [<fragmento2> ...]")
        sys.exit(1)

    patterns = [p.lower().replace("\\", "/") for p in sys.argv[1:]]

    db_path = str(Path(DB_PATH).resolve())
    print(f"Conectando a Chroma en: {db_path}")

    embeddings = init_embedding_model()
    vectorstore = Chroma(persist_directory=db_path, embedding_function=embeddings)

    from ingesta.utils import get_chroma_collection, indexed_source_info

    collection = get_chroma_collection(vectorstore)
    indexed = indexed_source_info(collection)

    # Buscar entradas que coincidan con los patrones
    to_delete_ids = []
    matched_sources = []
    for norm, entry in indexed.items():
        source = (entry.get("source") or "").replace("\\", "/")
        for pat in patterns:
            if pat.lower() in source.lower():
                to_delete_ids.extend(entry.get("ids", []))
                matched_sources.append(entry.get("source"))
                break

    if not to_delete_ids:
        print("No se encontraron documentos que coincidan con los patrones indicados.")
        print("Patrones buscados:", patterns)
        print(f"\nFuentes indexadas ({len(indexed)} únicas):")
        for norm in sorted(indexed)[:30]:
            print(f"  {indexed[norm].get('source')}")
        return

    unique_sources = sorted(set(matched_sources))
    print(f"\nDocumento(s) a eliminar ({len(unique_sources)} fuente(s)):")
    for s in unique_sources:
        print(f"  {s}")
    print(f"Chunks a eliminar: {len(to_delete_ids)}")

    confirm = input("\n¿Confirmar eliminación? [s/N]: ").strip().lower()
    if confirm != "s":
        print("Operación cancelada.")
        return

    vectorstore.delete(ids=to_delete_ids)
    print(f"\n✓ {len(to_delete_ids)} chunk(s) eliminado(s).")
    print("Ahora ejecuta 'python sync_ingesta.py' para re-indexar el documento.")

if __name__ == "__main__":
    main()
