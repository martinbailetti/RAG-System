# SMIRAG — Sistema RAG para Manuales Técnicos

Pipeline RAG económico y multimodal para manuales técnicos (PDF/HTML) con embeddings locales (HuggingFace), vectorstore Chroma y generación con Gemini Flash u Ollama.

## Características

- **Ingesta incremental**: sincronización inteligente por hash SHA1. Solo procesa documentos nuevos o modificados.
- **Persistencia por batch**: cada lote (~150 chunks) se persiste a disco inmediatamente, minimizando pérdida si se interrumpe el proceso.
- **Caché de descripciones de imágenes**: imágenes idénticas (logos, cabeceras repetidas en cada página) se describen solo una vez por sesión de ingesta (caché por SHA1 del contenido).
- **Enriquecimiento multimodal**: descripción automática de imágenes y tablas antes de indexar (vía Gemini u Ollama).
- **Expansión de queries**: stemming (Snowball), lematización (spaCy) y heurísticas de infinitivos/nominalizaciones para mejorar el recall en la búsqueda vectorial.
- **Filtrado nativo Chroma por scope**: `root_folder` y `query_paths` se resuelven a filtros `$in` nativos antes de la búsqueda, garantizando que solo se accede a documentos autorizados.
- **Filtrado post-retrieval inteligente**: keywords extraídos del query original (sin acentos, sin verbos modales) con umbral `is_strong` capeado a 3 para evitar descartar chunks relevantes.
- **API REST** con FastAPI: consulta con contexto conversacional, estructura de documentos, sincronización en background.
- **CLI interactivo** para consultas directas al vectorstore.
- **System prompt configurable** para personalizar respuestas del asistente.
- **Marcadores `.hidden`** para excluir documentos temporalmente sin borrarlos.
- **Detección de saludos/despedidas**: el endpoint responde automáticamente a mensajes cortos de cortesía sin activar el pipeline RAG.
- **Desambiguación automática**: si la pregunta es ambigua (ej. "¿cómo instalo el plugin?") y el contexto contiene múltiples opciones, el LLM pide aclaración al usuario en vez de adivinar. El campo `needs_clarification` en la respuesta permite al frontend renderizar estas preguntas de forma diferente.

---

## Requisitos Previos

- **Python 3.9+** (Windows 11)
- **Gemini API Key** (para generación y/o descripción de imágenes) — [Google AI Studio](https://aistudio.google.com/)
- Opcionalmente: **Ollama** local como alternativa para describir imágenes
- Opcionalmente: **spaCy** con modelo `es_core_news_sm` para mejorar lematización

---

## Instalación Rápida

```powershell
# Clonar/descargar el proyecto
cd C:\Projects\smirag

# Crear y activar entorno virtual
python -m venv venv
.\venv\Scripts\activate

# Instalar dependencias (incluye spaCy y el modelo es_core_news_sm)
pip install -r requirements.txt
```

> **Si el servidor no tiene acceso a internet**, el modelo `es_core_news_sm` no se descargará automáticamente desde GitHub. En ese caso, descarga el `.whl` en una máquina con acceso y cópialo al servidor:
>
> ```powershell
> # En una máquina con internet:
> pip download https://github.com/explosion/spacy-models/releases/download/es_core_news_sm-3.7.0/es_core_news_sm-3.7.0-py3-none-any.whl -d .
>
> # En el servidor (tras copiar el archivo):
> .\venv\Scripts\activate       # ← Activar el virtualenv primero
> pip install es_core_news_sm-3.7.0-py3-none-any.whl
> ```
>
> **Importante**: asegúrate de activar el virtualenv (`.\venv\Scripts\activate`) antes de ejecutar `pip install`. Si no, el modelo se instalará en el Python global del sistema y el servidor no lo encontrará.
>
> Para verificar que el modelo está disponible:
> ```powershell
> python -c "import es_core_news_sm; print('OK')"
> ```
> Si falla, el servidor arrancará igualmente pero usará heurísticas básicas de lematización (el log indicará `spaCy no disponible`).

---

## Configuración (.env)

Crear un archivo `.env` en la raíz del proyecto:

```env
GEMINI_API_KEY=tu_clave_aqui
DB_PATH=smidocs_db                          # Ruta donde persiste Chroma
DOCS_PATH=smidocs                           # Carpeta base con PDFs/HTML a indexar
ROOT_FOLDER=                                # Subcarpeta opcional para restringir ingesta
IA_SERVICE=gemini                           # gemini | ollama
GEMINI_MODEL_QUERY=gemini-2.5-flash         # Modelo Gemini para responder consultas
GEMINI_MODEL_IMAGE=gemini-2.0-flash         # Modelo Gemini para describir imágenes
OLLAMA_URL=http://localhost:11434           # Solo si IA_SERVICE=ollama
OLLAMA_MODEL=llama2                         # Modelo Ollama para describir imágenes
SYSTEM_PROMPT_FILE=system_prompt.txt        # Instrucciones del asistente (opcional)
SMIRAG_DISABLE_SPACY=0                     # 1/true/yes/on para desactivar spaCy
SMIRAG_MAX_CONVERSATION_CHARS=4000         # Máximo de caracteres de contexto conversacional
```

Las rutas relativas se resuelven desde la raíz del repositorio.

> **Nota sobre defaults**: sin `.env`, los valores por defecto son `DB_PATH=chroma_db_manuales`, `DOCS_PATH=manuales`, `GEMINI_MODEL_QUERY=gemini-2.5-flash`, `GEMINI_MODEL_IMAGE=gemini-2.0-flash`.

### Overrides por equipo

Cada estación puede crear `.env.<COMPUTERNAME>` (ej: `.env.ONLINE1`, `.env.PCFitxatge`). El loader (`env_loader.py`) aplica primero `.env` y luego el override específico (ignorando mayúsculas del nombre de host).

---

## Uso

### 1. Ingesta de documentos

Deposita PDFs/HTML en la carpeta `DOCS_PATH` y ejecuta:

```powershell
python sync_ingesta.py
```

- Solo procesa archivos nuevos o modificados (detección por SHA1 + mtime + size).
- Describe imágenes automáticamente según `IA_SERVICE`. Imágenes repetidas (logos) se describen solo una vez por sesión gracias a la caché por hash.
- **Persistencia incremental**: cada batch (~150 chunks) se guarda a disco. Si se interrumpe, solo se pierde el último lote y al reiniciar se reanudan los archivos pendientes.
- Usa marcadores `archivo.pdf.hidden` para excluir documentos sin borrarlos.
- Dry-run disponible con `python pre_ingesta.py` (detecta cambios sin insertar).

### 2. Consulta interactiva (CLI)

```powershell
python consulta.py
```

Escribe preguntas en lenguaje natural. Comandos de salida: `salir`, `exit`, `quit`.

> **Nota**: el CLI es una herramienta básica de diagnóstico. Para uso en producción, usar la API REST que tiene filtrado avanzado, contexto conversacional y detección de saludos.

### 3. API REST (FastAPI)

SMIRAG expone un servidor de aplicación ASGI (Asynchronous Server Gateway Interface): una aplicación **FastAPI** servida por **Uvicorn**.

- **FastAPI** es el framework web (define rutas, validación, serialización).
- **Uvicorn** es el servidor ASGI que escucha en el puerto y despacha las peticiones HTTP.

```powershell
uvicorn endpoint:app --host 0.0.0.0 --port 8888 --reload
```

O con los scripts de producción: `start.bat` / `stop.bat` (configurados para `D:\smidocs`).

#### Endpoints

| Método | Ruta | Descripción |
|--------|------|-------------|
| POST | `/consulta` | Consulta RAG con filtros opcionales y contexto conversacional |
| GET | `/estructura` | Árbol de carpetas navegable (param `base` opcional) |
| POST | `/ingesta/sync` | Sincronización en background con refresco del vectorstore |
| GET | `/ingesta/pendientes` | Dry-run: lista los documentos que se procesarían en la próxima ingesta sin insertar nada |
| GET | `/documentos` | Listado detallado de documentos indexados: metadatos, chunks, filtro por carpeta (`?carpeta=sub&limit=50`) |
| GET | `/documentos/detalle` | Texto completo concatenado de un documento por ruta exacta (`?ruta=smidocs/manual.pdf`) |

#### POST `/consulta` — Parámetros

```json
{
  "pregunta": "¿Cómo exportar tarjetas de una Forticash?",
  "rutas": ["ruta/exacta/del/pdf.pdf"],
  "root_folder": "subcarpeta",
  "query_paths": "smi_tec_044,smi_tec_045",
  "conversation_id": 1,
  "conversation": [
    {"role": "user", "content": "pregunta anterior"},
    {"role": "assistant", "content": "respuesta anterior"}
  ]
}
```

| Campo | Tipo | Descripción |
|-------|------|-------------|
| `pregunta` | string (obligatorio) | La consulta del usuario |
| `rutas` | list[string] | Filtrar por rutas exactas de documentos |
| `root_folder` | string | Restringir a una subcarpeta |
| `query_paths` | string | Prefijos de ruta separados por coma |
| `conversation` | list[object] | Historial conversacional (para coherencia, no para búsqueda) |

#### POST `/consulta` — Respuesta

```json
{
  "respuesta": "Para exportar tarjetas...",
  "fuentes": ["smi_tec_044_01.pdf (fragmento: ...)"],
  "usage": {"prompt_token_count": 1234, "candidates_token_count": 567},
  "found": true,
  "greeting": false,
  "needs_clarification": false
}
```

| Campo | Tipo | Descripción |
|-------|------|-------------|
| `respuesta` | string | Texto de respuesta del asistente |
| `fuentes` | list[string] | Fragmentos de documentos usados como contexto |
| `usage` | object \| null | Tokens consumidos (prompt, candidatos, total) |
| `found` | bool | `true` si se encontró información relevante |
| `greeting` | bool | `true` si el mensaje era un saludo/despedida (sin RAG) |
| `needs_clarification` | bool | `true` si la pregunta es ambigua y el LLM pide aclaración al usuario |

#### GET `/documentos` — Respuesta

```json
{
  "documentos": [
    {
      "ruta": "smidocs/tecnico/manual.pdf",
      "nombre": "manual.pdf",
      "carpeta": "smidocs/tecnico",
      "chunks": 45,
      "hash": "a3f1c2...",
      "mtime": 1708000000.0,
      "size": 204800
    }
  ],
  "total": 1,
  "total_chunks": 45
}
```

| Parámetro | Tipo | Descripción |
|-----------|------|-------------|
| `limit` | int (opcional) | Número máximo de documentos a retornar |
| `carpeta` | string (opcional) | Filtra documentos cuya carpeta contenga esta subcadena |

#### GET `/ingesta/pendientes` — Dry-run de ingesta

Replica exactamente la detección de `sincronizar_pdf_con_chroma` **sin modificar Chroma**. Útil para saber qué cambiaría antes de ejecutar `/ingesta/sync`.

| Parámetro | Tipo | Descripción |
|-----------|------|-------------|
| `carpeta` | string (opcional) | Carpeta a analizar (por defecto `DOCUMENTS_FOLDER`) |

Respuesta:

```json
{
  "carpeta": "/ruta/a/smidocs",
  "nuevos":      [{"ruta": "sub/manual.pdf", "nombre": "manual.pdf", "carpeta": "sub", "estado": "nuevo",      "hash": "a3f1c2...", "size": 204800, "mtime": 1708000000.0}],
  "modificados": [{"ruta": "sub/otro.pdf",   "nombre": "otro.pdf",   "carpeta": "sub", "estado": "modificado", "hash": "b4e2d3...", "size": 102400, "mtime": 1709000000.0}],
  "eliminados":  [{"ruta": "sub/viejo.pdf",  "nombre": "viejo.pdf",  "carpeta": "sub", "estado": "eliminado", "hash": null,       "size": null,   "mtime": null}],
  "ocultos":     [{"ruta": "sub/temp.pdf",   "nombre": "temp.pdf",   "carpeta": "sub", "estado": "oculto",    "hash": null,       "size": null,   "mtime": null}],
  "total": 4
}
```

| Campo | Descripción |
|-------|-------------|
| `nuevos` | Archivos en disco no indexados todavía |
| `modificados` | Indexados pero con hash distinto (se reingestarían) |
| `eliminados` | Indexados pero ya no existen en disco (se purgarían) |
| `ocultos` | Con marcador `.hidden`; se eliminarían del índice si existieran |
| `total` | Suma de todas las categorías |

---

## Arquitectura

```
├── endpoint.py              # API REST (FastAPI) — servicio principal
├── consulta.py              # CLI interactivo (diagnóstico)
├── sync_ingesta.py          # Script de sincronización/ingesta
├── env_loader.py            # Cargador .env con soporte multi-host
├── system_prompt.txt        # Instrucciones del asistente
├── start.bat / stop.bat     # Scripts de producción (puerto 8888)
├── ingesta/
│   ├── config.py            # Configuración centralizada (env vars + defaults)
│   ├── chroma_sync.py       # Sincronización incremental con Chroma
│   ├── query_core.py        # Embeddings, expansión de queries, prompts
│   ├── pdf_processing.py    # Extracción de texto/tablas/imágenes de PDFs
│   ├── html_processing.py   # Extracción de texto/tablas/imágenes de HTML
│   ├── image_descriptions.py# Descripción de imágenes (Gemini/Ollama) + caché
│   ├── image_cache.py       # Caché de imágenes remotas (HTML)
│   └── utils.py             # Fingerprints, normalización, índices, tablas→markdown
```

### Pipeline de ingesta

1. Detecta PDFs/HTML nuevos o modificados (SHA1 + mtime + size)
2. Excluye archivos con marcador `.hidden`
3. Extrae texto página a página (PyMuPDF para PDF / BeautifulSoup para HTML)
4. Procesa tablas (→ Markdown) y describe imágenes (Gemini/Ollama con caché por hash)
5. Divide con `RecursiveCharacterTextSplitter` (chunk_size=1500, overlap=300)
6. Genera embeddings locales con HuggingFace (`paraphrase-multilingual-mpnet-base-v2`)
7. Persiste en Chroma con metadatos (`source`, `source_hash`, `source_mtime`, `source_size`)
8. Cada batch de ~150 chunks se persiste a disco inmediatamente

### Pipeline de consulta (endpoint.py) — Flujo detallado

El pipeline de consulta transforma la pregunta del usuario en una respuesta contextualizada en 15 fases:

```
PREGUNTA DEL USUARIO
  ↓
[1]  Detección de saludos/despedidas
  ↓
[2]  Normalización y expansión de query
  ↓
[3]  Extracción de keywords (del query original)
  ↓
[4]  Generación de formas de keywords (sinónimos + morfología)
  ↓
[5]  Resolución de filtros de ruta (root_folder, query_paths → filtro nativo Chroma)
  ↓
[6]  Búsqueda vectorial inicial (k=20) con filtro nativo
  ↓
[7]  Fallback ampliado (k=150) si cobertura insuficiente
  ↓
[8]  Augmentación fase 1: chunks de mismas fuentes (con filtro de scope)
  ↓
[9]  Augmentación fase 2: códigos compuestos (con filtro de scope)
  ↓
[10] Augmentación fase 3: keywords prioritarias (con filtro de scope)
  ↓
[11] Verificación de cobertura → retry pre-LLM (opcional)
  ↓
[12] Priorización y deduplicación de documentos (top 5)
  ↓
[13] Construcción de contexto (limpieza de descripciones de imagen)
  ↓
[14] Invocación al LLM (Gemini) con system prompt + contexto + historial
  ↓
[15] Análisis de respuesta → retry post-LLM (opcional)
  ↓
RESPUESTA AL USUARIO
```

---

#### Fase 1 — Detección de saludos y despedidas

Antes de ejecutar el pipeline RAG, se comprueba si el mensaje es un saludo, agradecimiento o despedida.

- **Longitud máxima**: ≤60 caracteres (mensajes más largos se tratan como consultas reales).
- **Normalización**: se eliminan acentos (`_strip_accents`) y se convierte a minúsculas.
- **Patrones**: 3 expresiones regulares con anclas `^...$`:
  - **Saludo**: `hola`, `hey`, `buenas`, `buenos días`, `qué tal`, `saludos`…
  - **Agradecimiento**: `gracias`, `muchas gracias`, `te agradezco`, `thanks`…
  - **Despedida**: `adiós`, `chao`, `hasta luego`, `nos vemos`, `bye`…
- **Resultado**: si coincide, devuelve una respuesta enlatada con `greeting: true` y `found: true` sin RAG.

---

#### Fase 2 — Normalización y expansión de query

**Función**: `normalize_and_expand_query()` (en `ingesta/query_core.py`)

Genera una versión expandida de la pregunta que se usa exclusivamente para la búsqueda vectorial (similarity search). **No** se usa para extraer keywords.

1. **Normalización base**: minúsculas + eliminación de acentos (NFD).
2. **Tokenización**: `\b\w+\b` extrae tokens individuales.
3. **Stemming** (Snowball, español): reduce palabras a raíces (`configurar` → `configur`). Mínimo 3 caracteres.
4. **Lematización** (spaCy, opcional): modelo `es_core_news_sm`. Extrae lemas (`configurando` → `configurar`). Desactivable con `SMIRAG_DISABLE_SPACY=1`.
5. **Infinitivos**: detecta conjugaciones (`-amos`, `-endo`, `-an`, `-es`…) y genera candidatos infinitivos (`-ar`, `-er`, `-ir`). Mínimo 4 caracteres.
6. **Nominalizaciones**: de infinitivos genera sustantivos (`-ar` → `-ación`, `-amiento`, `-ador`; `-er` → `-ección`, `-imiento`; `-ir` → `-ición`, `-imiento`…). Mínimo 5 caracteres.
7. **Ensamblaje**: concatena `[original] + [base] + [stems] + [lemas] + [infinitivos] + [nominalizaciones]`.

**Ejemplo**: "¿Cómo configuro el reinicio?" → `"como configuro el reinicio como config reinicio reinicios configurar configuracion configuramiento..."`.

---

#### Fase 3 — Extracción de keywords

**Función**: `_extract_keywords()` (en `endpoint_keywords.py`)

Extrae hasta **7 keywords** (`MAX_EXTRACT_KEYWORDS`) del query **original** (no del expandido), para evitar que la expansión infle el umbral de `is_strong` y descarte chunks relevantes.

**Pasos**:

1. **Códigos compuestos**: detecta patrones alfanuméricos con guiones (ej. `PNM-P01`, `BNR-S01`) con regex `\b[a-z0-9]{2,}(?:-[a-z0-9]{1,})+\b` y los preserva enteros como keyword. Las partes individuales se marcan como "vistas" para no añadirlas por separado.
2. **Tokenización**: `\w+` en minúsculas. Se descartan:
   - Tokens con menos de 3 caracteres.
   - Tokens ya vistos.
   - Tokens en el set de **STOPWORDS** (~200 palabras).
   - Tokens a distancia Levenshtein 1 de algún stopword (posibles erratas).
3. **Deduplicación por substrings**: elimina tokens que son subcadenas de otros más largos (`instala` se descarta si existe `instalacion`).
4. **Ordenación y límite** (si hay más de 7 candidatos):
   - Con `prioritize_length=True` (por defecto): ordena por `len(token) + 3` si el token está en `DOMAIN_SYNONYMS`. El bonus +3 evita que palabras genéricas largas desplacen a términos técnicos cortos.
   - Con `prioritize_length=False` (en retries): se mantiene orden de aparición en la pregunta del usuario.

**STOPWORDS** — Categorías principales:
- Artículos/pronombres: `el`, `la`, `los`, `un`, `se`, `yo`, `usted`…
- Verbos comunes: `es`, `son`, `ser`, `estar`, `hay`, `hacer`…
- Preposiciones: `de`, `a`, `en`, `con`, `por`, `para`, `sin`…
- Conjunciones: `y`, `o`, `pero`, `si`, `no`…
- **Verbos modales/interrogativos**: `debo`, `puedo`, `quiero`, `necesito`, `saber`, `explicar`, `indicar`, `ayudar` — no aportan al matching con documentos técnicos.
- Cuantificadores: `alguno`, `todo`, `mucho`, `algo`, `nada`…

---

#### Fase 4 — Generación de formas de keywords (sinónimos + morfología)

**Funciones**: `_keyword_forms()` y `_keyword_core_forms()` (en `endpoint_keywords.py`)

Para cada keyword se genera un set de **formas variantes** usadas en el matching:

**Formas core** (morfológicas, sin sinónimos):
- La keyword original.
- Si ≥ 5 caracteres: sin último carácter (`claves` → `clave`).
- Si termina en `-es` y ≥ 6 caracteres: sin `-es`.
- Si termina en `-os`/`-as`: sin sufijo (raíz).

**Formas completas** (core + sinónimos):
- Todo lo anterior.
- Si la keyword o alguna forma core está en `DOMAIN_SYNONYMS`: se añaden todos los sinónimos del grupo.
- Si contiene guión (código compuesto): se añaden las partes individuales ≥ 2 chars.

**DOMAIN_SYNONYMS** — Algunos grupos:
| Keyword | Sinónimos |
|---------|-----------|
| `borrar` | limpiar, eliminar, wipe, limpieza, borrado, liberacion |
| `disco` | unidad, drive, espacio, capacidad, almacenamiento |
| `instalar` | instalacion, instalador, setup |
| `error` | fallo, problema, incidencia, averia |
| `ccm` | maquina, mdc, cambio |
| `entrar` | acceder, acceso, entrada |
| `reiniciar` | reinicio, restart, reboot, rearrancar |
| `contrasena` | password, clave, pin, codigo |

Total: ~40+ grupos de sinónimos de dominio técnico.

**PRIORITY_KEYWORDS** — Términos específicos de dominio que los embeddings infravaloran:
`sas`, `soja`, `ticketserver`, `tito`, `aft`, `smc`, `gbg`, `cas`, `ready2b`, `ukbar`, `mdc`, `ccm`, `gistra`, `wigos`, `hitachi`

---

#### Fase 5 — Resolución de filtros de ruta (filtro nativo Chroma)

**Funciones**: `_resolve_allowed_sources()` y `_build_chroma_filter()` (en `endpoint.py`)

Antes de cualquier búsqueda vectorial, los filtros de ruta (`root_folder`, `query_paths`) se resuelven a un **filtro nativo Chroma `$in`** usando el set `ALL_SOURCES` (construido al startup con todas las rutas únicas del vectorstore).

**Flujo**:

1. **Resolución de scope** — `_resolve_allowed_sources(root_norm, prefix_filters)`:
   - Si no hay `root_folder` ni `query_paths`: devuelve `None` (sin restricción).
   - Si hay filtros: itera `ALL_SOURCES` y selecciona las fuentes cuya ruta normalizada empiece por los prefijos indicados.
   - Devuelve un `set[str]` con las fuentes exactas permitidas.

2. **Construcción del filtro Chroma** — `_build_chroma_filter(rutas_norm, allowed_sources)`:
   - Si `rutas` (filtro exacto por documento): `{"source": {"$in": rutas_norm}}`. Se intersecta con `allowed_sources` si existe.
   - Si solo hay scope (`allowed_sources`): `{"source": {"$in": list(allowed_sources)}}`.
   - Si `allowed_sources` cubre todas las fuentes: devuelve `None` (optimización, filtro innecesario).
   - Si ningún source coincide: devuelve un sentinel que no matcheará nada.

**Ventajas frente al filtrado post-retrieval anterior**:
- **Seguridad**: Chroma solo busca en documentos autorizados, no en toda la base.
- **Eficiencia**: k=20 siempre devuelve 20 documentos relevantes dentro del scope (antes podían descartarse la mayoría por estar fuera del prefijo).
- **Sin doble filtrado**: las augmentaciones también reciben `allowed_sources` y solo añaden chunks de fuentes permitidas.

**`ALL_SOURCES`**: set de ~N rutas únicas indexadas, construido al startup y tras cada `/ingesta/sync`. Se deriva de los valores del `SOURCE_TOKEN_INDEX` (O(1) por fuente).

---

#### Fase 6 — Búsqueda vectorial inicial

Realiza una búsqueda por similitud en Chroma con la query expandida (fase 2) y el filtro nativo (fase 5):

- **k = 20** (`RETRIEVAL_K`): recupera los 20 chunks más similares **dentro del scope autorizado**.
- El filtro `source_filter` (construido en fase 5) se pasa directamente a `db.similarity_search()`.
- Si no hay resultados con la query expandida: reintenta con la pregunta original.

---

#### Fase 7 — Fallback ampliado

Si la búsqueda inicial no recupera suficientes documentos "strong":

- **Criterio**: `qualified < TARGET_KEYWORD_DOCS` (3 fuentes strong mínimas).
- **Acción**: nueva búsqueda con `k = 150` (`FALLBACK_K`).
- **Filtro de candidatos**: se añaden hasta `MAX_FALLBACK_APPEND` (20) documentos que cumplan el umbral de keyword matches y no estén ya en la lista.

**is_strong()** — Un documento se considera "strong" si:
- Si no hay keywords: siempre strong.
- Si `content_matches + path_matches ≥ required_matches` (1 si ≤1 keyword, sino 2).
- Si tiene al menos 1 path match: automáticamente strong.
- Sino: `content_matches ≥ min(len(keyword_forms), 3)`. El cap a **3** evita que queries con muchos términos descarten chunks relevantes.

---

#### Fases 8-10 — Augmentaciones

Tres mecanismos complementarios buscan chunks adicionales que la búsqueda vectorial puede haber perdido. Todos reciben `allowed_sources` para **garantizar que solo añaden chunks de fuentes dentro del scope autorizado** (sin necesidad de re-filtrado posterior):

**Fase 8 — Augmentación por fuentes** (`_augment_with_source_candidates`):
- Consulta el `SOURCE_TOKEN_INDEX` (índice invertido token→fuentes) con las formas de keywords.
- Filtra las fuentes candidatas contra `allowed_sources` antes de puntuar.
- Puntúa cada fuente por cantidad de tokens coincidentes.
- Para **fuentes nuevas** (no en resultados): añade hasta 2 chunks por fuente.
- Para **fuentes existentes**: añade hasta 1 chunk adicional.
- Límite total: `SOURCE_FALLBACK_MAX_DOCS` (12).

**Fase 9 — Códigos compuestos** (`_augment_with_compound_code_search`):
- Detecta códigos alfanuméricos con guión en la pregunta original (ej. `PNM-P01`).
- Los busca directamente en Chroma con `$contains` (case-sensitive) combinado con filtro `where: {"source": {"$in": allowed_sources}}` si hay scope activo.
- Prueba 3 variantes: original, UPPER, lower. Se detiene en la primera que devuelve resultados.
- Máximo `_COMPOUND_CODE_LIMIT` (12) chunks por código.

**Fase 10 — Keywords prioritarias** (`_augment_with_priority_keyword_search`):
- Si alguna keyword es un `PRIORITY_KEYWORD` y aparece en el `SOURCE_TOKEN_INDEX`:
- Filtra las fuentes del índice contra `allowed_sources`.
- Carga los chunks de las fuentes correspondientes.
- Añade hasta 2 chunks con coincidencia de keywords por fuente.

---

#### Fase 11 — Verificación de cobertura y retry pre-LLM

**Condición**: si hay ≥ 4 keywords y < 50% de ellas tienen al menos una coincidencia en algún documento recuperado:

1. Re-extrae keywords con `prioritize_length=False` (orden de aparición en vez de por longitud).
2. Si el set de keywords cambió: repite todo el pipeline (fases 5-10) con las nuevas keywords.
3. Si el retry no produce documentos pero los había antes: restaura el estado pre-retry.

**Propósito**: el usuario tiende a mencionar el tema principal al principio de la pregunta. Reordenar las keywords por aparición puede relevar términos clave que la priorización por longitud desplazó.

Adicionalmente, se eliminan todos los documentos con 0 keyword matches (tanto en contenido como en ruta) para no enviar contexto irrelevante al LLM.

---

#### Fase 12 — Priorización y deduplicación

**Función**: `_prioritize_docs()` (en `endpoint_retrieval.py`)

Selecciona los mejores **5 fragmentos** (`MAX_CONTEXT_DOCS`) para enviar al LLM:

1. **Clasificación**: separa documentos en "strong" y "weak" según `is_strong()`.
2. **Scoring** — Cada doc "strong" recibe un puntaje:

```
score = (path_matches × 5000)
      + (content_matches × 1000)
      + (useful_content_ratio × 500)      ← densidad de texto útil
      + (match_quality × 1500)             ← 1.0 si coincidencia directa, 0.5 si solo sinónimo
      + (priority_keywords_en_ruta × 6000) ← bonus por PRIORITY_KEYWORDS en la ruta
      − retrieval_rank                     ← penaliza posiciones más bajas
      − 2000 (si no hay path match ni priority bonus)
```

| Componente | Multiplicador | Propósito |
|---|---|---|
| Path matches | ×5000 | Señal más fuerte: el documento habla del tema |
| Content matches | ×1000 | Señal media: el tema se menciona en el texto |
| Densidad (útil/total) | ×500 | Preferir texto puro sobre chunks con imágenes |
| Calidad (directa/sinónimo) | ×1500 | Distinguir coincidencias literales de expansiones |
| Priority keyword en ruta | ×6000 | Forzar documentos especializados (SAS, CCM…) |
| Retrieval rank | −rank | Desempatar: los primeros resultados son mejores |

3. **Deduplicación por similitud Jaccard**: si dos documentos tienen > 60% de overlap en tokens, se elimina el duplicado y se reemplaza por el siguiente candidato.
4. **Relleno**: si los slots no se llenan con docs "strong", se añaden docs "weak" hasta alcanzar el límite.

---

#### Fase 13 — Construcción de contexto

**Función**: `build_context()` (en `ingesta/query_core.py`)

Prepara el texto que se enviará al LLM:

1. Para cada documento priorizado:
   - Extrae `page_content`.
   - **Elimina descripciones de imágenes** con 3 patrones regex:
     - `[DESCRIPCIÓN DE IMAGEN ...]: texto descriptivo.]` — bloques completos.
     - `[IMAGEN ...]: sin descripción disponible.` — marcadores sin descripción.
     - Párrafos huérfanos ≥50 caracteres que terminan en `.]` (fragmentos de descripción cortados por el text-splitter).
   - Colapsa 3+ saltos de línea consecutivos a 2.
2. Concatena todos los fragmentos con `\n\n`.

---

#### Fase 14 — Invocación al LLM (Gemini)

**Modelo**: configurable con `GEMINI_MODEL_QUERY` (default: `gemini-2.5-flash`).

**System prompt**: se carga al iniciar desde `system_prompt.txt` (o del archivo indicado por `SYSTEM_PROMPT_FILE`). Contiene instrucciones de idioma, terminología, formato de respuesta y la regla de desambiguación (`[ACLARACIÓN]`).

**Historial conversacional** (opcional):
- Si `req.conversation` contiene mensajes previos, se formatean como `"ROLE: contenido"` y se prepend a la pregunta.
- Máximo `SMIRAG_MAX_CONVERSATION_CHARS` (default 4000, cap 20000) caracteres.
- **Solo para coherencia y desambiguación**, NO como fuente de verdad. No se usa para la búsqueda vectorial.

**Prompt final**:
```
{system_prompt}

Contexto:
{contexto}

Pregunta: {pregunta_con_historial_opcional}
Respuesta:
```

**Manejo de errores**: si la llamada a Gemini falla, devuelve HTTP 502 con el detalle del error.

**Métricas de uso**: se extraen `prompt_token_count`, `candidates_token_count` y `total_token_count` de la respuesta (campo `usage` en la response).

---

#### Fase 15 — Análisis de respuesta y retries post-LLM

**a) Detección de desambiguación** (`check_clarification`):
- Si el texto de la respuesta empieza con `[ACLARACIÓN]` (regex: `^\s*\[ACLARACI[OÓ]N\]\s*`):
- El LLM indica que la pregunta es ambigua y existen múltiples opciones en el contexto.
- Se elimina el prefijo, se retorna con `found: true` y `needs_clarification: true`.
- **No** se ejecutan retries post-LLM.

**b) Detección de "no información"** (`check_no_info`):
- 13 patrones regex detectan frases como "no se encuentra información", "no tengo datos", "no dispongo de información"…
- Si se detecta un patrón "no info" **pero** el LLM ofrece alternativas (detectadas por 6 patrones adicionales como "sin embargo…", "lo que sí tengo…"), se considera `found: true`.
- Si realmente no hay información: `found: false`.

**c) Retry post-LLM** (si `found: false` y no se ejecutó el retry pre-LLM):
1. Re-extrae keywords con `prioritize_length=False`.
2. Repite el pipeline completo (fases 5-13).
3. Invoca al LLM de nuevo con el nuevo contexto.
4. Si produce `found: true`: reemplaza la respuesta original.
5. Si tampoco encuentra: mantiene la respuesta original.

**d) Modo debug** (`SMIRAG_DEBUG_RESPONSES=1`):
- Fuerza `found: true` para mostrar la respuesta real del LLM en vez de "No se encontró información".
- Incluye `debug_info` en la respuesta con: keywords, keyword_forms, docs_retrieved, detalle por chunk (source, matches, rank, snippet).

---

### Constantes clave (endpoint.py)

| Constante | Valor | Descripción |
|-----------|-------|-------------|
| `RETRIEVAL_K` | 20 | Docs recuperados en búsqueda inicial |
| `FALLBACK_K` | 150 | Docs en búsqueda ampliada |
| `MAX_CONTEXT_DOCS` | 5 | Fragmentos enviados al LLM |
| `MAX_FALLBACK_APPEND` | 20 | Máx. docs añadidos por fallback |
| `TARGET_KEYWORD_DOCS` | 3 | Mínimo docs "strong" deseados |
| `MAX_EXTRACT_KEYWORDS` | 7 | Máx. keywords extraídas por query |
| `SOURCE_FALLBACK_TOKEN_LIMIT` | 12 | Tokens candidatos para augmentación por fuente |
| `SOURCE_FALLBACK_DOCS_PER_SOURCE` | 2 | Chunks por fuente nueva en augmentación |
| `SOURCE_FALLBACK_MAX_DOCS` | 12 | Total máx. docs añadidos por augmentación |
| `_COMPOUND_CODE_LIMIT` | 12 | Chunks por código compuesto |
| `_GREETING_MAX_LENGTH` | 60 | Máx. chars para detección de saludos |
| `JACCARD_DEDUP_THRESHOLD` | 0.6 | Umbral de similitud para deduplicar |
| `SMIRAG_MAX_CONVERSATION_CHARS` | 4000 | Máx. chars de historial conversacional |
| `is_strong` cap | 3 | Máximo keyword matches requeridos |

---

## Utilidades

| Script | Descripción |
|--------|-------------|
| `listado.py` | Lista rutas indexadas en Chroma (normalizadas) |
| `listado_raw.py` | Lista rutas EXACTAS sin normalizar |
| `pre_ingesta.py` | Dry-run: detecta nuevos/modificados sin insertar |
| `debug_query.py` | Diagnóstico de recuperación vectorial |
| `inspect_source_chunks.py` | Inspecciona chunks de una fuente |
| `ver_contenido.py` | Muestra contenido indexado de un archivo |
| `batch_prompt_eval.py` | Evaluación batch de prompts |
| `backup_and_recreate_chroma.py` | Respalda y recrea la base Chroma |
| `tools_test_ollama.py` | Smoke test de Ollama |

---

## Dependencias principales

| Paquete | Versión | Propósito |
|---------|---------|-----------|
| langchain | 0.2.17 | Framework RAG |
| langchain-chroma | 0.2.2 | Integración Chroma |
| chromadb | 0.4.24 | Vectorstore persistente (SQLite) |
| pymupdf | 1.23.26 | Extracción de PDFs |
| google-generativeai | ≥0.7.0 | API de Gemini |
| sentence-transformers | ≥2.7.0 | Embeddings locales |
| fastapi | ≥0.115.0 | API REST |
| pydantic | ≥2.7 | Validación de datos |
| beautifulsoup4 | 4.14.2 | Procesamiento HTML |
| snowballstemmer | 2.2.0 | Stemming español |
| python-dotenv | 1.0.1 | Carga de .env |
| requests | 2.31.0 | HTTP (Ollama, imágenes remotas) |
| spacy | ≥3.7.0 *(opcional)* | Lematización española |

Ver [requirements.txt](requirements.txt) para la lista completa con versiones pinneadas.

> **Nota**: `uvicorn` no está en requirements.txt; debe instalarse aparte (`pip install uvicorn`).

---

## Consideraciones

- **Costos**: Gemini genera costos por descripción de imágenes. La caché por hash reduce drásticamente llamadas redundantes (logos repetidos). Para evitar costos, omitir `GEMINI_API_KEY` o usar `IA_SERVICE=ollama`.
- **Interrupción de ingesta**: Gracias a la persistencia por batch (~150 chunks), solo se pierde el último lote si se corta el proceso. Al reiniciar, se reanudan solo los archivos pendientes.
- **Keyword extraction vs query expansion**: Los keywords para filtrado se extraen del query original (sin acentos); la query expandida solo alimenta el similarity search. Mezclar ambos flujos infla el umbral y descarta chunks relevantes.
- **Stopwords modales**: verbos como "debo", "puedo", "quiero", "saber", "necesito" se filtran de los keywords para evitar que inflen el conteo sin aportar relevancia al matching.
- **Rutas normalizadas**: Mover manuales sin reindexar deja entradas huérfanas hasta la próxima sincronización.
- **Objetos globales**: `endpoint.py` mantiene vectorstore en memoria; tras sincronización externa, usar `/ingesta/sync` o reiniciar.
- **CORS**: solo se permiten orígenes específicos (configurados en `ALLOWED_ORIGINS` dentro de `endpoint.py`).