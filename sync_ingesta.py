"""Script auxiliar para sincronizar PDFs con Chroma usando el paquete `ingesta`."""
import datetime
import os
import sys

# Forzar UTF-8 en stdout/stderr para evitar UnicodeEncodeError en terminales Windows cp1252
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

import requests

from env_loader import load_host_env
from ingesta.chroma_sync import sincronizar_pdf_con_chroma
from ingesta.ingesta_lock import acquire_lock, release_lock

load_host_env()

INGESTA_NOTIFY_URL = os.environ.get("SMIRAG_INGESTA_NOTIFY_URL", "").strip()


def _notify_ingesta(folder: str, documentos: list, eliminados: list) -> None:
    """Envía notificación POST a smi_api con los documentos procesados y eliminados."""
    if not INGESTA_NOTIFY_URL:
        print("⚠️  SMIRAG_INGESTA_NOTIFY_URL no configurado; omitiendo notificación.")
        return
    if not documentos and not eliminados:
        print("ℹ️  Ingesta sin cambios; omitiendo notificación.")
        return

    payload = {
        "folder": folder,
        "documentos": documentos,
        "eliminados": eliminados,
        "timestamp": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    }
    total = len(documentos) + len(eliminados)
    print(f"📧 Enviando notificación de ingesta ({len(documentos)} procesados, {len(eliminados)} eliminados) → {INGESTA_NOTIFY_URL}")
    try:
        resp = requests.post(INGESTA_NOTIFY_URL, json=payload, timeout=30)
        if resp.ok:
            print(f"✅ Notificación enviada correctamente. Respuesta: {resp.json()}")
        else:
            print(f"⚠️  Notificación respondió con status {resp.status_code}: {resp.text[:300]}")
    except Exception as exc:
        print(f"❌ Error al enviar notificación: {exc}")


def main() -> None:
    if not acquire_lock():
        sys.exit(1)  # mensaje ya impreso por acquire_lock
    try:
        result = sincronizar_pdf_con_chroma() or {}
        processed_docs = result.get("procesados", [])
        deleted_docs = result.get("eliminados", [])
        print(f"📄 Documentos procesados: {len(processed_docs)}")
        for doc in processed_docs:
            print(f"   • {doc.get('nombre', '?')} → {doc.get('ruta', '?')}")
        if deleted_docs:
            print(f"🗑️  Documentos eliminados: {len(deleted_docs)}")
            for doc in deleted_docs:
                print(f"   • {doc.get('nombre', '?')} → {doc.get('ruta', '?')}")
    finally:
        release_lock()

    # Notificar a smi_api (fuera del lock para no bloquear)
    if processed_docs or deleted_docs:
        _notify_ingesta(
            folder=os.environ.get("DOCS_PATH", "manuales"),
            documentos=processed_docs,
            eliminados=deleted_docs,
        )


if __name__ == "__main__":
    main()
