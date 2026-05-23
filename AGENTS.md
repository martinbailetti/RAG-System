# Guía Rápida para Agentes

## Propósito del Proyecto
- Pipeline RAG económico para manuales técnicos (PDF/HTML) con embeddings locales y generación en Gemini u Ollama.
- Enriquecimiento multimodal: descripción automática de imágenes y tablas antes de indexar.
- Servicios principales: sincronización de documentos, consulta interactiva por consola y API REST vía FastAPI.
- System prompt configurable para personalizar respuestas del asistente (saludos, despedidas, terminología).

## Configuración Esencial (.env)
```
GEMINI_API_KEY=...
DB_PATH=smidocs_db            # Ruta (relativa o absoluta) donde persiste Chroma
DOCS_PATH=smidocs              # Carpeta base con PDFs/HTML a indexar
ROOT_FOLDER=                   # Subcarpeta opcional dentro de DOCS_PATH para restringir ingesta
IA_SERVICE=gemini|ollama      # Controla si las imágenes se describen con Gemini u Ollama
OLLAMA_URL=http://...         # Solo si IA_SERVICE=ollama
OLLAMA_MODEL=llama2           # Modelo utilizado por Ollama para describir imágenes
GEMINI_MODEL_QUERY=gemini-2.5-flash   # Modelo Gemini para responder consultas
GEMINI_MODEL_IMAGE=gemini-2.0-flash   # Modelo Gemini para describir imágenes en ingesta
SYSTEM_PROMPT_FILE=system_prompt.txt  # Archivo con instrucciones del asistente (opcional)
SMIRAG_DISABLE_SPACY=0        # Desactivar spaCy para lematización (1/true/yes/on)
SMIRAG_MAX_CONVERSATION_CHARS=4000  # Máximo caracteres de historial conversacional en prompt
SMIRAG_DEBUG_RESPONSES=0      # Modo debug: found siempre True, incluye debug_info en respuesta (1/true/yes/on)
RAG_INCLUDE_IMAGE_DESCRIPTION=true  # Describir imágenes durante la ingesta (false=omitir, ahorra coste Gemini/Ollama)
```
- Las rutas relativas se resuelven desde la raíz del repo (`c:\Projects\smirag`).
- Cada agente puede definir un `.env.<COMPUTERNAME>` (por ejemplo `.env.ONLINE1`, `.env.PCFitxatge`) para sobreescribir valores locales; el loader (`env_loader.py`) intenta `.env`, luego `.env.<COMPUTERNAME>` (ignorando mayúsculas) y aplica override.
- Sin `GEMINI_API_KEY` las descripciones de imágenes se omiten; el flujo de texto sigue funcionando.
- **Defaults si no hay `.env`**: `DB_PATH=chroma_db_manuales`, `DOCS_PATH=manuales`, `GEMINI_MODEL_QUERY=gemini-2.5-flash`, `GEMINI_MODEL_IMAGE=gemini-2.0-flash`, `IA_SERVICE=gemini`, `OLLAMA_MODEL=llama2`.

## Estructura Relevante
- `ingesta/` contiene la lógica reusable:
  - `config.py`: configuración centralizada de rutas, modelos y servicios IA.
  - `chroma_sync.py`: sincronización incremental con Chroma (batch=150, persist por lote).
  - `query_core.py`: utilidades de consulta, embeddings, expansión de queries y prompts.
  - `pdf_processing.py` / `html_processing.py` / `md_processing.py`: extracción de texto, tablas e imágenes (PDF/HTML) o texto plano (Markdown).
  - `image_descriptions.py`: descripción de imágenes vía Gemini u Ollama con **caché en memoria por SHA1** (evita re-describir logos repetidos).
  - `image_cache.py`: caché de imágenes remotas en `.ingesta_image_cache/` (para HTML).
  - `utils.py`: utilidades comunes (fingerprints, normalización, tablas→markdown, índices).
- `env_loader.py`: cargador de `.env` con soporte multi-host (detecta COMPUTERNAME).
- `sync_ingesta.py` llama a `ingesta.chroma_sync.sincronizar_pdf_con_chroma` para mantener Chroma alineado con el árbol de documentos.
- `consulta.py` ofrece CLI de preguntas/respuestas sobre el vectorstore persistido.
- `endpoint.py` expone FastAPI con `/consulta`, `/estructura`, `/ingesta/sync` y `/documentos`.
- `system_prompt.txt`: instrucciones del asistente (terminología, saludos, idioma, desambiguación).
- `smidocs_chroma_dev_database/` mantiene la base Chroma de desarrollo (incluye `chroma.sqlite3`).

## Pipelines Clave
- **Ingesta** (`ingesta/chroma_sync.py`):
  - Detecta PDFs/HTML/Markdown nuevos o modificados usando huellas SHA1+mtime+size.
  - Soporta marcadores `.hidden` para excluir documentos temporalmente.
  - Procesa tablas (`pdf_processing.py` / `html_processing.py`) y describe imágenes vía `image_descriptions.py` (con caché por hash para no re-describir logos).
  - Divide contenido con `RecursiveCharacterTextSplitter` (chunk_size=1500, overlap=300) y persiste en Chroma usando embeddings HuggingFace (`paraphrase-multilingual-mpnet-base-v2`).
  - **Persistencia por lote**: `MAX_CHROMA_BATCH=150`, se llama `_safe_persist()` después de cada batch. Si se interrumpe, solo se pierde el último lote.
- **Consulta CLI** (`consulta.py`):
  - Herramienta básica de diagnóstico. Usa `RETRIEVAL_K=3`, sin `MAX_CONTEXT_DOCS`.
  - ⚠️ **Bug conocido**: no pasa `system_prompt` al `PROMPT_TEMPLATE` (posible KeyError). Tampoco capea `is_strong`.
  - Extrae keywords del query expandido (diferente del endpoint que usa el original).
- **API** (`endpoint.py`):
  - `/consulta`: filtros opcionales por `rutas`, `root_folder`, `query_paths`; incluye detección automática de saludos/despedidas (regex, ≤60 chars).
  - **Filtrado nativo Chroma por scope**: `root_folder` y `query_paths` se resuelven al startup a filtros `$in` nativos usando `ALL_SOURCES` (set de fuentes únicas del vectorstore). Chroma solo busca en documentos autorizados, no en toda la base. Las augmentaciones también reciben `allowed_sources` para restringir sus búsquedas.
  - `/estructura`: árbol de carpetas navegable para frontends (param `base` opcional).
  - `/ingesta/sync`: ejecuta sincronización en background y refresca el vectorstore.
  - `/documentos`: listado detallado de documentos indexados (metadatos, número de chunks, filtro `carpeta`, `limit`).
  - `/documentos/detalle`: texto completo (todos los chunks concatenados, ordenados por página) de un documento identificado por `ruta` exacta. Devuelve 404 si no se encuentra.
  - Carga `system_prompt.txt` al iniciar para personalizar respuestas del asistente.
  - **Contexto conversacional**: se envía en el prompt para coherencia, pero NO se usa para búsqueda vectorial. Máximo controlado por `SMIRAG_MAX_CONVERSATION_CHARS` (default 4000, cap 20000).
  - **Filtrado de keywords post-retrieval**: los keywords para scoring/filtro se extraen del query original (con `_strip_accents`), NO del query expandido, para evitar inflación de umbral. La query expandida solo se usa para la búsqueda vectorial (similarity search).
  - **Stopwords ampliadas**: incluyen verbos modales/interrogativos (`debo`, `saber`, `puedo`, `quiero`, `necesito`, `explicar`, `indicar`, `ayudar`, etc.) que no aportan al matching con documentos técnicos.
  - El umbral `is_strong` está capeado a **máximo 3** matches independientemente del número de keywords, evitando que queries con muchos términos descarten chunks relevantes.
  - `MAX_CONTEXT_DOCS = 5`: el LLM recibe hasta 5 fragmentos de contexto.
  - `RETRIEVAL_K = 20` (búsqueda inicial), `FALLBACK_K = 150` (ampliada si faltan docs "strong").
  - **Retry pre-LLM** (cobertura): si menos del 50% de las keywords tienen match en algún doc recuperado, se reintenta con keywords por orden de aparición (en vez de por longitud+dominio). Evita enviar docs off-topic al LLM.
  - **Retry post-LLM** (`found=False`): si el LLM indica que no tiene información (`_NO_INFO_REGEXES`), se reintenta la búsqueda completa con keywords por orden de aparición y se vuelve a llamar al LLM. Solo se ejecuta si el retry pre-LLM no se ejecutó ya. Si el retry tampoco produce `found=True`, se mantiene la respuesta original.
  - **Desambiguación (needs_clarification)**: el system prompt instruye al LLM a prefixar su respuesta con `[ACLARACIÓN]` cuando la pregunta es ambigua y el contexto contiene múltiples opciones. `check_clarification()` en `endpoint_helpers.py` detecta el prefijo, lo elimina del texto y activa `needs_clarification=True` en `ConsultaResponse`. Cuando se detecta, se retorna inmediatamente con `found=True` sin ejecutar retries post-LLM. El frontend usa el campo booleano para renderizar la pregunta de forma distinta.

## Constantes clave (endpoint.py)
| Constante | Valor | Descripción |
|-----------|-------|-------------|
| `RETRIEVAL_K` | 20 | Docs recuperados en búsqueda inicial |
| `FALLBACK_K` | 150 | Docs en búsqueda ampliada |
| `MAX_CONTEXT_DOCS` | 5 | Fragmentos enviados al LLM |
| `TARGET_KEYWORD_DOCS` | 3 | Mínimo docs "strong" deseados antes de fallback |
| `is_strong` cap | 3 | Máximo keyword matches requeridos |
| `_GREETING_MAX_LENGTH` | 60 | Máximo chars para detección de saludos |

## Flujos Operativos
1. **Actualizar manuales**
   - Deposita nuevos PDFs/HTML en `DOCS_PATH`.
   - Ejecuta `python sync_ingesta.py` (PowerShell) para reindexar. Usa Gemini u Ollama según `IA_SERVICE`.
   - Dry-run disponible con `python pre_ingesta.py`.
2. **Consultar manuales**
   - `python consulta.py` inicia modo interactivo.
   - Comandos de salida: `salir`, `exit` o `quit`.
3. **Levantar API FastAPI**
   - Requiere vectorstore listo y `.env` configurado.
   - Arranca con `uvicorn endpoint:app --host 0.0.0.0 --port 8888 --reload`.
   - Producción: `start.bat` / `stop.bat` (hardcoded a `D:\smidocs`, puerto 8888).
4. **Respaldar base Chroma**
   - `python backup_and_recreate_chroma.py` mueve el directorio actual y crea uno limpio.

## Utilidades Complementarias
- `listado.py`: lista rutas únicas actualmente indexadas en Chroma (normalizadas).
- `listado_raw.py`: lista rutas EXACTAS tal como están almacenadas (sin normalizar).
- `tools_test_ollama.py`: smoke test de disponibilidad y respuesta del servidor Ollama.
- `pre_ingesta.py`: replica la detección de documentos nuevos/modificados SIN insertar en Chroma (dry-run).
- `debug_query.py`: diagnóstico detallado de recuperación vectorial para comparar entre PCs.
- `inspect_source_chunks.py`: inspecciona chunks específicos de una fuente indexada.
- `ver_contenido.py`: muestra el contenido indexado de un archivo específico en Chroma.
- `batch_prompt_eval.py`: evaluación batch de prompts contra el vectorstore.
- `start.bat` / `stop.bat`: scripts para iniciar/detener el servidor uvicorn en producción (puerto 8888, `D:\smidocs`).

## Dependencias
- Ver `requirements.txt`. Núcleo: LangChain 0.2.x, Chroma 0.4.x, PyMuPDF 1.23.x, Google Generative AI 0.7.x.
- Dependencias opcionales: `spacy>=3.7.0` (lematización española), `snowballstemmer` (stemming).
- ⚠️ `uvicorn` no está en `requirements.txt`; debe instalarse aparte.
- Ejecutar `pip install -r requirements.txt` en un entorno Python 3.9+ (Windows 11).

## Consideraciones y Gotchas
- **Keyword extraction vs query expansion**: `_extract_keywords` se ejecuta sobre el query original (sin acentos) para obtener hasta `MAX_EXTRACT_KEYWORDS` (7) keywords; `_keyword_forms` ya genera variantes morfológicas (singular/plural, quita sufijos) y expande con `DOMAIN_SYNONYMS`. La query expandida (`normalize_and_expand_query`) con stems/lemas/nominalizaciones solo alimenta el similarity search de Chroma. No mezclar ambos flujos: extraer keywords del query expandido infla el umbral de `is_strong` y descarta chunks relevantes.
- **Códigos compuestos con guión**: `_extract_keywords` detecta patrones alfanuméricos con guiones (ej. `PNM-P01`, `BNR-S01`) y los preserva como keywords completas. Sin esto, `\w+` rompe el código en partes de < 4 chars que se descartan. `_keyword_forms` añade las partes individuales como formas para el matching por source tokens. Además, `_augment_with_compound_code_search` busca estos códigos directamente en Chroma con `$contains` cuando la búsqueda vectorial no los encuentra (el modelo de embeddings no da suficiente peso a tokens alfanuméricos cortos).
- **Priorización de keywords**: cuando hay más candidatas que `MAX_EXTRACT_KEYWORDS`, se ordenan por `len(token) + 3` si el token está en `DOMAIN_SYNONYMS` (bonus dominio). Esto evita que palabras largas genéricas (ej. "insuficiente") desplacen a términos técnicos cortos pero específicos (ej. "borrar", "disco", "memoria").
- **Stopwords modales**: verbos como `debo`, `puedo`, `quiero`, `saber`, `necesito`, `explicar`, `indicar`, `ayudar` están en STOPWORDS para evitar que inflen keywords sin aportar relevancia al matching con documentos técnicos.
- **Caché de descripciones de imágenes**: `image_descriptions.py` mantiene caché en memoria por SHA1 del base64. Imágenes idénticas (logos, cabeceras) se describen una sola vez por sesión de ingesta. Usar `get_image_cache_stats()` para ver hits/misses.
- **`consulta.py` vs `endpoint.py`**: el CLI es una herramienta básica de diagnóstico con diferencias significativas respecto al endpoint (no usa system_prompt, extrae keywords del query expandido, no capea `is_strong`, no tiene detección de saludos ni conversación). Para producción, usar siempre el endpoint.
- **`SMIRAG_MAX_CONVERSATION_CHARS`** (no `TURNS`): controla caracteres de historial. Los `.env` actuales definen `SMIRAG_MAX_CONVERSATION_TURNS` que no tiene efecto; corregir a `_CHARS`.
- Chroma usa rutas normalizadas; mover manuales sin reindexar deja entradas huérfanas hasta la próxima sincronización.
- Gemini genera costos por descripción de imágenes; si no es necesario, omite `GEMINI_API_KEY` o cambia `IA_SERVICE` a `ollama`. La caché por hash reduce drásticamente llamadas para logos repetidos.
- `endpoint.py` mantiene objetos globales; tras sincronización externa puede ser necesario reiniciar el proceso o usar `/ingesta/sync`.
- ⚠️ ~~`_refresh_vectorstore()` en `endpoint.py` recrea el retriever con `k=3` en vez de `RETRIEVAL_K` (20). Bug menor: solo afecta tras `/ingesta/sync`.~~ Corregido: ya no usa retriever, usa `db.similarity_search()` con `k=RETRIEVAL_K` y filtro nativo.
- Procesamiento HTML descarga imágenes remotas (con caché ligera en `.ingesta_image_cache/`) y respeta un límite de 5 MB por imagen.
- Sin la librería `google-generativeai` instalada, las descripciones por Gemini fallan silenciosamente; verifica logs.
- **CORS**: solo se permiten orígenes específicos definidos en `ALLOWED_ORIGINS` dentro de `endpoint.py`.

## Próximos Pasos Sugeridos para Nuevos Agentes
1. Validar `.env` y disponibilidad de Gemini/Ollama antes de tocar la ingesta.
2. Ejecutar `python listado.py` para confirmar contenido actual del vectorstore.
3. Realizar una sincronización de prueba con un manual pequeño y ver registros en consola.
4. Añadir pruebas o monitoreo para `/consulta` según necesidades del equipo.
5. Corregir `SMIRAG_MAX_CONVERSATION_TURNS` → `SMIRAG_MAX_CONVERSATION_CHARS` en los archivos `.env`.
6. Corregir `consulta.py`: pasar `system_prompt` al template y capear `is_strong`.
7. ~~Corregir `_refresh_vectorstore()`: usar `RETRIEVAL_K` en vez de `k=3`.~~ Corregido.
