# Guía Rápida para Agentes

## Propósito
- Pipeline RAG para manuales técnicos (PDF/HTML/Markdown): embeddings locales (HuggingFace), vectorstore Chroma, generación Gemini u Ollama.
- Enriquecimiento multimodal: descripción automática de imágenes y tablas antes de indexar.
- Servicios: `sync_ingesta.py` (ingesta), `endpoint.py` (API FastAPI producción).
- `system_prompt.txt`: personaliza respuestas (saludos, terminología, desambiguación con prefijo `[ACLARACIÓN]`).

## Configuración Esencial (.env)
```
GEMINI_API_KEY=...
DB_PATH=docs_db
DOCS_PATH=docs
ROOT_FOLDER=                              # Subcarpeta opcional dentro de DOCS_PATH
IA_SERVICE=gemini|ollama
GEMINI_MODEL_QUERY=gemini-2.5-flash
GEMINI_MODEL_IMAGE=gemini-2.0-flash
OLLAMA_URL=http://localhost:11434
OLLAMA_MODEL=llama2
SYSTEM_PROMPT_FILE=system_prompt.txt
RAG_KEYWORD_CONFIG_DIR=keyword_config      # Carpeta con stopwords/sinónimos/priority keywords (JSON)
RAG_DISABLE_SPACY=0
RAG_SPACY_MODEL=es_core_news_sm
RAG_MAX_CONVERSATION_CHARS=4000            # Cap máximo: 20000
RAG_DEBUG_RESPONSES=0
RAG_INCLUDE_IMAGE_DESCRIPTION=true
RAG_PORT=8888
RAG_LOG_LEVEL=INFO                         # DEBUG | INFO | WARNING | ERROR
RAG_LOG_FILE=rag.log
RAG_INGESTA_NOTIFY_URL=                    # POST opcional tras ingesta con docs procesados/eliminados
FAQS_RELATIVE_PATH=                        # Ruta relativa a DOCS_PATH del archivo de FAQs (ej. faqs.txt)
```
- Rutas relativas se resuelven desde la raíz del repo.
- Override por host: `.env.<COMPUTERNAME>` (ej. `.env.PCFitxatge`); `env_loader.py` aplica `.env` y luego el override.
- Sin `GEMINI_API_KEY`: descripciones de imágenes omitidas; texto sigue funcionando.
- **Defaults sin `.env`**: `DB_PATH=chroma_db_manuales`, `DOCS_PATH=manuales`, `GEMINI_MODEL_QUERY=gemini-2.5-flash`, `GEMINI_MODEL_IMAGE=gemini-2.0-flash`, `IA_SERVICE=gemini`, `OLLAMA_MODEL=llama2`.

## Estructura Relevante
```
endpoint.py              # FastAPI: rutas, orquestación del pipeline de consulta
endpoint_keywords.py     # Extracción keywords, stopwords, sinónimos (carga JSON al importar)
endpoint_retrieval.py    # Priorización, augmentaciones, scoring de chunks
endpoint_helpers.py      # Saludos, conversación, debug, detección no-info del LLM
endpoint_models.py       # Modelos Pydantic de request/response
app_logging.py           # Logger centralizado (consola + rag.log con rotación)
env_loader.py            # Cargador .env multi-host
sync_ingesta.py          # Script CLI de sincronización incremental
system_prompt.txt
keyword_config/          # JSON de keywords (ruta configurable vía RAG_KEYWORD_CONFIG_DIR)
  stopwords.json
  domain_synonyms.json
  priority_keywords.json
ingesta/
  config.py              # Rutas, modelos, flags de ingesta
  chroma_sync.py         # Sincronización incremental (batch=150, persist por lote)
  query_core.py          # Embeddings, expansión de queries, prompts, system prompt
  pdf_processing.py / html_processing.py / md_processing.py
  image_descriptions.py  # Descripción imágenes (Gemini/Ollama) + caché SHA1 en memoria
  image_cache.py         # Caché imágenes remotas HTML (.ingesta_image_cache/)
  ingesta_lock.py        # Lock inter-proceso (.ingesta.lock) para evitar ingestas concurrentes
  utils.py               # Fingerprints, normalización, índices de tokens por ruta
```

## keyword_config/ (JSON)
Cargados al arrancar por `endpoint_keywords.py`. Los tres archivos son **obligatorios**.

| Archivo | Formato | Uso |
|---------|---------|-----|
| `stopwords.json` | `string[]` | Palabras descartadas al extraer keywords (artículos, modales, preposiciones…). |
| `domain_synonyms.json` | `{ "term": ["syn1", ...] }` | Sinónimos técnicos para matching post-retrieval y bonus de priorización. |
| `priority_keywords.json` | `string[]` | Términos que embeddings infravaloran; bonus en scoring y búsqueda por ruta (`_augment_with_priority_keyword_search`). |

## API (`endpoint.py`) — Endpoints
| Ruta | Descripción |
|------|-------------|
| `GET /health` | Estado del servidor (`?type=json` para JSON) |
| `GET /config/env` | Archivo `.env` activo, última modificación y valores efectivos (sin `GEMINI_API_KEY`) |
| `POST /consulta` | Pipeline RAG completo |
| `GET /estructura` | Árbol de carpetas (`base` opcional) |
| `GET /ingesta/status` | Estado de ingesta en curso |
| `GET /ingesta/pendientes` | Dry-run: nuevos/modificados/eliminados/ocultos sin tocar Chroma |
| `POST /ingesta/sync` | Sincronización en background; 409 si ya hay ingesta; `restart` opcional (default true) |
| `POST /restart` | Reinicia uvicorn vía `restart.bat` |
| `GET /documentos` | Listado indexado (`limit`, `carpeta`, `texto`) |
| `DELETE /documentos` | Elimina documento del índice por `ruta` |
| `GET /documentos/detalle` | Texto completo concatenado por `ruta` |
| `GET /documentos/archivo` | Descarga el archivo original del disco (`?ruta=...`) |
| `GET /faqs` | Lee el archivo de FAQs (`FAQS_RELATIVE_PATH`) |
| `PUT /faqs` | Sobrescribe el archivo de FAQs |
| `GET /log` | Contenido de `rag.log` (`?tail=N` para últimas N líneas) |

### Descarga de archivos (`GET /documentos/archivo`)
- Parámetro obligatorio `ruta`: tal como aparece en `GET /documentos` (ej. `docs/tecnico/manual.pdf`).
- Acepta ruta relativa a `DOCS_PATH` o absoluta ya indexada así.
- **Seguridad**: la ruta resuelta debe quedar dentro de `DOCUMENTS_FOLDER`; intentos de path traversal → HTTP 403.
- Respuesta: `FileResponse` con `Content-Disposition` y MIME según extensión (`application/octet-stream` si no se detecta).
- Errores: 422 (sin `ruta`), 403 (fuera de `DOCS_PATH`), 404 (archivo inexistente).

### Gestión de documentos indexados
- `GET /documentos`: filtro `texto` busca en el contenido de chunks (insensible a mayúsculas/acentos).
- `DELETE /documentos`: purga todos los chunks cuya metadata `source` coincide exactamente con `ruta`.
- `GET /documentos/detalle`: concatena chunks ordenados por `page` + `chunk_index`.
- `GET /documentos/archivo`: sirve el binario original en disco (no el texto indexado).

**CORS**: orígenes en `ALLOWED_ORIGINS` dentro de `endpoint.py`.

## Pipelines Clave

### Ingesta (`ingesta/chroma_sync.py`)
- Detecta PDF/HTML/Markdown nuevos o modificados (SHA1 + mtime + size).
- Marcadores `.hidden` excluyen documentos temporalmente.
- Chunking: `RecursiveCharacterTextSplitter` (1500/300). Embeddings: `paraphrase-multilingual-mpnet-base-v2`.
- Persistencia por lote (`MAX_CHROMA_BATCH=150`); si se interrumpe, solo se pierde el último lote.
- Lock: `ingesta_lock.py` (entre procesos) + `_ingesta_lock` threading (en `/ingesta/sync`).
- Tras sync con cambios: notificación opcional (`RAG_INGESTA_NOTIFY_URL`) y reinicio automático del servidor (salvo `restart=false`).

### Consulta API (`endpoint.py` + módulos `endpoint_*`)
1. Saludos/despedidas (regex, ≤60 chars) → respuesta enlatada sin RAG.
2. Query expandida (`normalize_and_expand_query`) → **solo** similarity search.
3. Keywords del query **original** (`_extract_keywords`, máx. 7) → scoring/filtro post-retrieval.
4. Filtro nativo Chroma `$in` por `root_folder`/`query_paths` vía `ALL_SOURCES`.
5. Búsqueda inicial `RETRIEVAL_K=20`; fallback `FALLBACK_K=150` si faltan docs "strong".
6. Augmentaciones (con `allowed_sources`): source candidates, códigos compuestos (`$contains`), priority keywords en ruta, búsqueda literal de último recurso.
7. `_prioritize_docs`: `is_strong` capeado a **máx. 3** matches; scoring con densidad de contenido útil, calidad de match y bonus priority.
8. Contexto al LLM: hasta `MAX_CONTEXT_DOCS=5`. Conversación en prompt (no en búsqueda), cap `RAG_MAX_CONVERSATION_CHARS`.
9. Retry pre-LLM: si <50% keywords con match → reintento por orden de aparición.
10. Retry post-LLM: si `found=False` y LLM dice no-info → reintento completo (solo si pre-LLM no corrió).
11. `[ACLARACIÓN]` → `needs_clarification=True`, retorno inmediato sin retries.
12. Preguntas de listado de documentos (`_es_busqueda_documentos`) → `all_sources=True` en la respuesta; fuentes deduplicadas por ruta y filtradas por keywords específicas.

## Constantes Clave
| Constante | Valor | Ubicación |
|-----------|-------|-----------|
| `RETRIEVAL_K` | 20 | `endpoint.py` |
| `FALLBACK_K` | 150 | `endpoint.py` |
| `MAX_CONTEXT_DOCS` | 5 | `endpoint.py` |
| `TARGET_KEYWORD_DOCS` | 3 | `endpoint.py` |
| `MAX_EXTRACT_KEYWORDS` | 7 | `endpoint_keywords.py` |
| `is_strong` cap | 3 | `endpoint_retrieval.py` |
| `_GREETING_MAX_LENGTH` | 60 | `endpoint_helpers.py` |

## Flujos Operativos
1. **Indexar**: depositar docs en `DOCS_PATH` → `python sync_ingesta.py` (o `POST /ingesta/sync`).
2. **Consultar**: `uvicorn endpoint:app --host 0.0.0.0 --port 8888` (puerto configurable con `RAG_PORT`).
3. **Dry-run ingesta**: `GET /ingesta/pendientes` (API) — no existe `pre_ingesta.py` en el repo.
4. **Forzar reindexado**: `python force_reindex.py <fragmento_ruta>` elimina chunks y deja que sync los reingeste.
5. **Producción Windows**: `start.bat` / `stop.bat` / `restart.bat` (puerto 8888 hardcoded en .bat).

## Utilidades Existentes
- `ver_contenido.py`: contenido indexado de un archivo en Chroma.
- `batch_prompt_eval.py`: evaluación batch de prompts.
- `force_reindex.py`: purga chunks por ruta parcial para forzar reingesta.
- `check_health.ps1`: health check contra `API_URL` del `.env`.
- `check_versions.py`: verificación de versiones de dependencias.

## Gotchas Críticos
- **Keywords vs expansión**: nunca extraer keywords del query expandido; infla `is_strong` y descarta chunks relevantes.
- **Códigos con guión** (`PNM-P01`): preservados enteros; partes como formas; `_augment_with_compound_code_search` con `$contains`.
- **Priorización**: candidatos ordenados por `len(token) + 3` si están en `DOMAIN_SYNONYMS`.
- **Caché imágenes ingesta**: SHA1 en memoria por sesión; logos repetidos se describen una vez.
- **`RAG_MAX_CONVERSATION_CHARS`** (no `TURNS`): `RAG_MAX_CONVERSATION_TURNS` en `.env` no tiene efecto.
- **Chroma rutas normalizadas**: mover manuales sin reindexar deja huérfanos hasta próximo sync.
- **Objetos globales en endpoint**: tras sync externo usar `/ingesta/sync` o reiniciar; `_refresh_vectorstore()` recarga `ALL_SOURCES`.
- **Mensaje obsoleto**: código referencia `ingesta_masiva.py` que no existe; usar `sync_ingesta.py`.
- **HTML imágenes**: descarga remota con caché; límite 5 MB por imagen.

## Checklist para Nuevos Agentes
1. Validar `.env` y disponibilidad Gemini/Ollama.
2. Confirmar `keyword_config/` existe (o ruta en `RAG_KEYWORD_CONFIG_DIR`).
3. Verificar contenido indexado: `GET /documentos` o `ver_contenido.py`.
4. Sync de prueba con un manual pequeño; revisar `rag.log`.
5. Para cambios de keywords: editar JSON en `keyword_config/` y **reiniciar** el servidor (se cargan al importar).
6. Producción: siempre `endpoint.py` (no hay CLI de consulta en el repo).
