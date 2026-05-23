import importlib
import os
import re
from fastapi import FastAPI, HTTPException, BackgroundTasks
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import PlainTextResponse
from pydantic import BaseModel, validator
import langchain
from typing import Optional, List, Dict, Any, Tuple

from app_logging import logger
from env_loader import load_host_env
from ingesta.query_core import (
    MODELO_CONSULTA,
    PROMPT_TEMPLATE,
    build_context,
    create_embedding_model,
    ensure_gemini_model,
    get_db_path,
    get_documents_folder,
    load_system_prompt,
    normalize_and_expand_query,
    open_vectorstore,
    SPACY_AVAILABLE,
    SPACY_DISABLED,
    _strip_accents,
)
from ingesta.utils import (
    build_source_token_index,
    get_chroma_collection,
    normalize_source_reference,
)

load_host_env()

# Cargar system prompt una vez al iniciar (cache)
SYSTEM_PROMPT = load_system_prompt()
logger.info("System prompt cargado (%d caracteres)", len(SYSTEM_PROMPT))


def _is_debug_responses() -> bool:
    """Devuelve True si SMIRAG_DEBUG_RESPONSES está activo.
 
    Cuando está activo:
    - found siempre es True → el frontend muestra la respuesta real del LLM
      en vez del mensaje fijo "No encontramos una respuesta..."
    - Se incluye debug_info con keywords, formas, fuentes y scoring.
    Activar con SMIRAG_DEBUG_RESPONSES=1 en .env o entorno.
    """
    return os.environ.get("SMIRAG_DEBUG_RESPONSES", "0").strip().lower() in ("1", "true", "yes", "on")

# Patrones para detectar saludos, agradecimientos y despedidas.
# Usan anclas ^...$ para que solo coincidan con mensajes que sean EXCLUSIVAMENTE
# un saludo ("hola", "buenas tardes"). Prompts mixtos como
# "Hola, qué color tiene el cielo?" NO coinciden y pasan al pipeline RAG.
# NOTA: los patrones usan formas SIN acento porque _detectar_saludo
# normaliza el texto con _strip_accents antes de evaluar.
_GREETING_PATTERNS = [
    (r"^\s*(hola|hey|buenas?|buenos?\s*(dias?|tardes?|noches?)|buenas?\s*(dias?|tardes?|noches?)|hola[,;]?\s*(buenas?\s*(tardes?|noches?|dias?)?)?|que\s*tal|saludos?|hi|hello|buenas?\s*tardes?|buenas?\s*noches?|buenas?\s*dias?)\s*[,;]?\s*(baldric)?\s*[.!?]*\s*$", "saludo"),
    (r"^\s*(gracias|muchas\s*gracias|te\s*agradezco|thanks|thank\s*you)\s*[,;]?\s*(baldric)?\s*[.!?]*\s*$", "gracias"),
    (r"^\s*(adios|chao|chau|hasta\s*luego|nos\s*vemos|bye|hasta\s*pronto)\s*[,;]?\s*(baldric)?\s*[.!?]*\s*$", "despedida"),
]

# Longitud máxima para considerar detección de saludo (caracteres).
# Mensajes más largos se asumen como prompts reales y pasan al RAG.
_GREETING_MAX_LENGTH = 60

_GREETING_RESPONSES = {
    "saludo": "¡Hola! 👋 ¿En qué te puedo ayudar?",
    "gracias": "¡De nada! Si tienes más consultas, aquí estoy para ayudarte.",
    "despedida": "¡Hasta luego! Fue un gusto ayudarte.",
}


def _detectar_saludo(texto: str) -> str | None:
    """Detecta si el texto es un saludo, agradecimiento o despedida.

    Solo se evalúan mensajes cortos (< _GREETING_MAX_LENGTH caracteres) para
    evitar falsos positivos con prompts reales que empiecen con un saludo.
    Se normalizan acentos para que "buenos dias" y "buenos días" matcheen igual.
    """
    texto_lower = _strip_accents(texto.lower().strip())
    if len(texto_lower) > _GREETING_MAX_LENGTH:
        return None
    for pattern, tipo in _GREETING_PATTERNS:
        if re.match(pattern, texto_lower, re.IGNORECASE):
            return tipo
    return None


# Compatibilidad con versiones antiguas de LangChain
if not hasattr(langchain, "debug"):
    langchain.debug = False  # type: ignore[attr-defined]

try:
    DB_PATH = get_db_path()
except RuntimeError as exc:
    raise RuntimeError(f"No se pudo inicializar DB_PATH: {exc}") from exc

try:
    DOCUMENTS_FOLDER = get_documents_folder("manuales")
except RuntimeError as exc:
    raise RuntimeError(f"No se pudo resolver la carpeta de documentos: {exc}") from exc

# Importar la sincronización incremental desde ingesta.py de forma dinámica para evitar
# dependencias duras cuando se ejecute este archivo como script independiente.
sincronizar_pdf_con_chroma = None  # type: ignore[assignment]

try:
    ingesta_mod = importlib.import_module("ingesta")
    sincronizar_pdf_con_chroma = getattr(ingesta_mod, "sincronizar_pdf_con_chroma", None)
    DOCUMENTS_FOLDER = getattr(ingesta_mod, "DOCUMENTS_FOLDER", DOCUMENTS_FOLDER)
except Exception:
    sincronizar_pdf_con_chroma = None  # type: ignore[assignment]

class ConsultaRequest(BaseModel):
    pregunta: str
    rutas: Optional[List[str]] = None  # lista de rutas a filtrar (opcionales)
    root_folder: Optional[str] = None  # restringir por carpeta raíz (opcional)
    query_paths: Optional[str] = None  # prefijos de ruta separados por coma (opcional)

    # Contexto conversacional opcional (para coherencia entre turnos)
    conversation_id: Optional[int] = None
    conversation: Optional[List[Dict[str, Any]]] = None

    class Config:
        extra = "ignore"


class ChatMessage(BaseModel):
    role: str
    content: str

    @validator("role", pre=True)
    def normalize_role(cls, v):
        role = str(v or "").strip().lower()
        if role in {"assistant", "ai", "bot"}:
            return "assistant"
        if role == "system":
            return "system"
        return "user"

    @validator("content", pre=True)
    def normalize_content(cls, v):
        return str(v or "").strip()

class ConsultaResponse(BaseModel):
    respuesta: str
    fuentes: list[str]
    usage: dict[str, int] | None = None
    found: bool = True
    greeting: bool = False
    debug_info: dict[str, Any] | None = None


class IngestaRequest(BaseModel):
    carpeta: Optional[str] = None


class TreeNode(BaseModel):
    name: str
    path: str
    type: str  # siempre "directory" en este contexto
    children: Optional[List["TreeNode"]] = None

    class Config:
        arbitrary_types_allowed = True


TreeNode.update_forward_refs()

embedding_model = create_embedding_model()

try:
    db = open_vectorstore(DB_PATH, embedding_model)
except Exception as exc:
    raise RuntimeError(
        "La base de datos Chroma no está disponible. Ejecuta 'ingesta_masiva.py' primero."
    ) from exc

_COLLECTION = get_chroma_collection(db)
SOURCE_TOKEN_INDEX = build_source_token_index(_COLLECTION)
_SOURCE_CHUNK_CACHE: Dict[str, List[Tuple[str, Dict[str, Any]]]] = {}
SOURCE_FALLBACK_TOKEN_LIMIT = 12
SOURCE_FALLBACK_DOCS_PER_SOURCE = 2
SOURCE_FALLBACK_MAX_DOCS = 12


class _InlineDoc:
    """Contenedor ligero para documentos añadidos por heurísticas."""

    def __init__(self, content: str, metadata: Dict[str, Any]):
        self.page_content = content
        self.metadata = metadata

llm = ensure_gemini_model(MODELO_CONSULTA)

RETRIEVAL_K = 20
FALLBACK_K = 150
TARGET_KEYWORD_DOCS = 3
MAX_CONTEXT_DOCS = 5
MAX_EXTRACT_KEYWORDS = 7
MAX_FALLBACK_APPEND = 20

retriever = db.as_retriever(search_kwargs={"k": RETRIEVAL_K})

if SPACY_AVAILABLE:
    logger.info("spaCy activo: se usan lemas para normalizar las preguntas.")
elif SPACY_DISABLED:
    logger.info("spaCy deshabilitado por SMIRAG_DISABLE_SPACY; heurísticas básicas en uso.")
else:
    logger.info("spaCy no disponible; se mantienen heurísticas básicas de lematización.")


STOPWORDS = {
    "el",
    "la",
    "los",
    "las",
    "un",
    "una",
    "unos",
    "unas",
    "lo",
    "al",
    "del",
    "de",
    "que",
    "y",
    "o",
    "u",
    "a",
    "en",
    "con",
    "por",
    "para",
    "como",
    "se",
    "es",
    "son",
    "ser",
    "soy",
    "eres",
    "somos",
    "estoy",
    "estas",
    "esta",
    "está",
    "están",
    "esta",
    "estamos",
    "estan",
    "hay",
    "habia",
    "había",
    "tenia",
    "tenía",
    "tengo",
    "tienes",
    "tienen",
    "hacer",
    "hace",
    "hacen",
    "si",
    "no",
    "mas",
    "más",
    "menos",
    "pero",
    "sin",
    "sobre",
    "entre",
    "cuanto",
    "cuánto",
    "cual",
    "cuál",
    "cuales",
    "cuáles",
    "donde",
    "dónde",
    "cuando",
    "cuándo",
    "quien",
    "quién",
    "quienes",
    "quiénes",
    "que",
    "qué",
    "este",
    "esta",
    "estos",
    "estas",
    "ese",
    "esa",
    "eso",
    "esos",
    "esas",
    "aquel",
    "aquella",
    "aquellos",
    "aquellas",
    "mi",
    "mis",
    "tu",
    "tus",
    "tú",
    "su",
    "sus",
    "nuestro",
    "nuestra",
    "nuestros",
    "nuestras",
    "vuestro",
    "vuestra",
    "vuestros",
    "vuestras",
    "yo",
    "usted",
    "ustedes",
    "nosotros",
    "nosotras",
    "vosotros",
    "vosotras",
    "ellos",
    "ellas",
    "me",
    "te",
    "se",
    "nos",
    "les",
    "le",
    "etc",
    "etcetera",
    "etcétera",
    # Verbos modales/interrogativos típicos de preguntas (no aparecen en docs técnicos)
    "debo",
    "deben",
    "debemos",
    "debe",
    "saber",
    "sabes",
    "sabe",
    "sabemos",
    "saben",
    "puedo",
    "puede",
    "pueden",
    "podemos",
    "quiero",
    "quiere",
    "quieren",
    "queremos",
    "necesito",
    "necesita",
    "necesitan",
    "necesitamos",
    "tengo",
    "tener",
    "podria",
    "podría",
    "seria",
    "sería",
    "deberia",
    "debería",
    "quisiera",
    "favor",
    "ayuda",
    "ayudar",
    "explicar",
    "explica",
    "decir",
    "dime",
    "indica",
    "indicar",
    # Indefinidos y cuantificadores (no aportan al matching técnico)
    "alguna",
    "alguno",
    "algunos",
    "algunas",
    "algun",
    "ninguna",
    "ninguno",
    "ningun",
    "otra",
    "otro",
    "otros",
    "otras",
    "todo",
    "toda",
    "todos",
    "todas",
    "mucho",
    "mucha",
    "muchos",
    "muchas",
    "poco",
    "poca",
    "algo",
    "nada",
    "cada",
    "mismo",
    "misma",
    "tambien",
    "también",
    "siempre",
    "nunca",
    # Conjugaciones faltantes de verbos ya incluidos
    "tenemos",
    "hacemos",
    "hemos",
    "sido",
    "siendo",
    "eran",
    "fueron",
    # Saludos temporales (ruido conversacional, no técnico)
    "buenos",
    "buenas",
    "dias",
    "tardes",
    "noches",
    "manana",
    "mañana",
    # Preposiciones/adverbios genéricos faltantes
    "tras",
    "desde",
    "hasta",
    "antes",
    "despues",
    "después",
    "durante",
    "mientras",
    "dentro",
    "fuera",
    "aqui",
    "aquí",
    "alla",
    "allí",
    # Verbos/sustantivos genéricos que no aportan al matching técnico
    "realizar",
    "realiza",
    "realizan",
    "llegar",
    "llega",
    "llegan",
    "dando",
    "momento",
    "tipo",
    "forma",
    "manera",
    "parte",
    "partes",
    "cosa",
    "cosas",
    "veces",
    "lado",
}


# Sinónimos de dominio: expanden keyword forms para mejorar el matching
# entre el vocabulario del usuario y el de los documentos técnicos.
DOMAIN_SYNONYMS: dict[str, set[str]] = {
    "borrar": {"limpiar", "eliminar", "wipe", "limpieza", "borrado", "eliminacion", "liberar"},
    "limpiar": {"borrar", "eliminar", "wipe", "borrado", "limpieza", "liberar"},
    "eliminar": {"borrar", "limpiar", "wipe", "borrado", "eliminacion", "liberar"},
    "wipe": {"borrar", "limpiar", "eliminar", "limpieza", "borrado", "liberar"},
    "limpieza": {"limpiar", "borrar", "wipe", "eliminar", "liberar"},
    "borrado": {"borrar", "limpiar", "wipe", "eliminar", "liberar"},
    "liberar": {"borrar", "limpiar", "wipe", "eliminar", "limpieza", "liberacion"},
    "liberacion": {"liberar", "limpiar", "borrar", "wipe", "eliminar"},
    "disco": {"unidad", "drive", "espacio"},
    "unidad": {"disco", "drive", "espacio"},
    "drive": {"disco", "unidad", "espacio"},
    "espacio": {"disco", "unidad", "drive", "capacidad", "almacenamiento"},
    "instalar": {"instalacion", "instalador", "setup"},
    "instalacion": {"instalar", "instalador", "setup"},
    "instalador": {"instalar", "instalacion", "setup"},
    "actualizar": {"actualizacion", "update", "upgrade"},
    "actualizacion": {"actualizar", "update", "upgrade"},
    "configurar": {"configuracion", "config", "ajustar"},
    "configuracion": {"configurar", "config"},
    "error": {"fallo", "problema", "incidencia", "averia"},
    "fallo": {"error", "problema", "incidencia"},
    "problema": {"error", "fallo", "incidencia"},
    "memoria": {"ram", "memory", "insuficiente"},
    "memoria": {"ram", "espacio"},
    "descargar": {"descarga", "download"},
    "descarga": {"descargar", "download"},
    "reiniciar": {"reinicio", "reboot", "restart"},
    "reinicio": {"reiniciar", "reboot", "restart"},
    "arrancar": {"arranque", "inicio", "boot"},
    "arranque": {"arrancar", "inicio", "boot"},
    "carpeta": {"directorio", "folder"},
    "directorio": {"carpeta", "folder"},
    "pantalla": {"monitor", "display", "screen"},
    "monitor": {"pantalla", "display", "screen"},
    "red": {"network", "conexion", "ethernet", "wifi"},
    "conexion": {"red", "network", "ethernet", "wifi"},
    "impresora": {"printer", "imprimir", "impresion"},
    "imprimir": {"impresora", "printer", "impresion"},
}


def _safe_int(value: str, fallback: int) -> int:
    try:
        return int(value)
    except Exception:
        return fallback


def _format_conversation_for_prompt(raw_conversation: Optional[List[Dict[str, Any]]], pregunta: str) -> str:
    if not raw_conversation:
        return ""

    max_chars = _safe_int(os.environ.get("SMIRAG_MAX_CONVERSATION_CHARS", "4000"), 4000)
    max_chars = max(0, min(max_chars, 20000))
    if max_chars == 0:
        return ""

    messages: list[ChatMessage] = []
    for item in raw_conversation:
        try:
            msg = ChatMessage(**(item or {}))
        except Exception:
            continue
        if not msg.content:
            continue
        messages.append(msg)

    # Evitar duplicar la pregunta actual si viene incluida como último turno.
    if messages and messages[-1].role == "user" and messages[-1].content == (pregunta or "").strip():
        messages = messages[:-1]

    if not messages:
        return ""

    lines: list[str] = []
    for msg in messages:
        lines.append(f"{msg.role.upper()}: {msg.content}")

    text = "\n".join(lines).strip()
    if len(text) <= max_chars:
        return text
    return text[-max_chars:]


def _extract_keywords_from_conversation(conversation: Optional[List[Dict[str, Any]]], limit: int = 3) -> list[str]:
    """Extrae keywords de los últimos mensajes de la conversación para mejorar la búsqueda vectorial."""
    if not conversation:
        return []

    # Tomar los últimos N mensajes (priorizando los más recientes)
    recent = conversation[-limit:] if len(conversation) > limit else conversation

    combined_text = ""
    for msg in recent:
        content = msg.get("content", "")
        if isinstance(content, str):
            combined_text += " " + content

    return _extract_keywords(combined_text)


def _is_likely_stopword_typo(word: str) -> bool:
    """Detecta si una palabra es un posible typo de un stopword (distancia de edición 1)."""
    if len(word) < 4:
        return False
    letters = "abcdefghijklmnñopqrstuvwxyzáéíóú"
    splits = [(word[:i], word[i:]) for i in range(len(word) + 1)]
    deletes = [L + R[1:] for L, R in splits if R]
    transposes = [L + R[1] + R[0] + R[2:] for L, R in splits if len(R) > 1]
    replaces = [L + c + R[1:] for L, R in splits if R for c in letters]
    inserts = [L + c + R for L, R in splits for c in letters]
    candidates = set(deletes + transposes + replaces + inserts)
    return bool(candidates & STOPWORDS)


def _extract_keywords(text: str, *, prioritize_length: bool = True) -> list[str]:
    keywords: list[str] = []
    seen: set[str] = set()
    for token in re.findall(r"\w+", text.lower()):
        if len(token) < 4 or token in seen or token in STOPWORDS:
            continue
        # Descartar posibles typos de stopwords (distancia de edición 1)
        if _is_likely_stopword_typo(token):
            continue
        keywords.append(token)
        seen.add(token)
    filtered: list[str] = []
    for token in keywords:
        if any(len(other) > len(token) and token in other for other in keywords):
            continue
        filtered.append(token)
    if len(filtered) > MAX_EXTRACT_KEYWORDS:
        if prioritize_length:
            # Priorizar keywords de dominio y más largas (más específicas).
            # Términos presentes en DOMAIN_SYNONYMS reciben un bonus de +3
            # para que no sean desplazados por palabras largas genéricas.
            filtered.sort(
                key=lambda t: len(t) + (3 if t in DOMAIN_SYNONYMS else 0),
                reverse=True,
            )
        # Si prioritize_length=False, mantiene orden de aparición:
        # el usuario tiende a mencionar el tema principal primero.
        filtered = filtered[:MAX_EXTRACT_KEYWORDS]
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
    # Expandir con sinónimos de dominio para mejorar matching semántico
    synonyms = DOMAIN_SYNONYMS.get(keyword)
    if synonyms:
        forms.update(synonyms)
    return {form for form in forms if len(form) >= 3}


def _keyword_match_count(doc, keyword_forms: dict[str, set[str]]) -> int:
    if not keyword_forms:
        return 0
    text = (doc.page_content or "").lower()
    return sum(
        1
        for forms in keyword_forms.values()
        if any(form in text for form in forms)
    )


def _source_keyword_match_count(doc, keyword_forms: dict[str, set[str]]) -> int:
    if not keyword_forms:
        return 0
    source = str(doc.metadata.get("source", "")).lower()
    return sum(
        1
        for forms in keyword_forms.values()
        if any(form in source for form in forms)
    )


def _norm_path(path: str | None) -> str:
    if not path:
        return ""
    return os.path.normcase(os.path.abspath(path))


def _normalize_source_input(value: str | None) -> str:
    if not value:
        return ""
    normalized = normalize_source_reference(value.strip())
    return normalized.strip()


def _prioritize_docs(
    docs: list,
    keyword_forms: dict[str, set[str]],
    required_matches: int,
    limit: int,
) -> list:
    if not docs:
        return []
    if not keyword_forms:
        return sorted(docs, key=lambda d: d.metadata.get("_retrieval_rank", 0))[:limit]

    def is_strong(doc) -> bool:
        matches = doc.metadata.get("_keyword_matches", 0)
        path_matches = doc.metadata.get("_path_keyword_matches", 0)
        if matches < required_matches:
            return False
        if path_matches > 0:
            return True
        needed = min(max(required_matches + 1, len(keyword_forms)), 3)
        return matches >= needed

    def doc_score(doc) -> float:
        matches = doc.metadata.get("_keyword_matches", 0)
        path_matches = doc.metadata.get("_path_keyword_matches", 0)
        rank = doc.metadata.get("_retrieval_rank", 0)
        score = (path_matches * 5000) + (matches * 1000) - rank
        if path_matches == 0:
            score -= 2000
        return score

    strong_docs = [doc for doc in docs if is_strong(doc)]
    weak_docs = [doc for doc in docs if doc not in strong_docs]

    ordered: list = []
    ordered.extend(sorted(strong_docs, key=doc_score, reverse=True))
    if len(ordered) < limit:
        ordered.extend(sorted(weak_docs, key=doc_score, reverse=True))

    return ordered[:limit]


def _load_source_records(source: str) -> List[Tuple[str, Dict[str, Any]]]:
    if source in _SOURCE_CHUNK_CACHE:
        return _SOURCE_CHUNK_CACHE[source]
    if _COLLECTION is None:
        return []

    records = _COLLECTION.get(where={"source": {"$eq": source}}, include=["documents", "metadatas"])
    documents = records.get("documents") or []
    metadatas = records.get("metadatas") or []

    prepared: List[Tuple[str, Dict[str, Any]]] = []
    for text, metadata in zip(documents, metadatas):
        meta_copy: Dict[str, Any] = dict(metadata or {})
        meta_copy["source"] = meta_copy.get("source") or source
        prepared.append((text or "", meta_copy))

    _SOURCE_CHUNK_CACHE[source] = prepared
    return prepared


def _augment_with_source_candidates(
    docs: List[Any],
    keyword_forms: dict[str, set[str]],
    required_matches: int,
) -> int:
    if not keyword_forms or not SOURCE_TOKEN_INDEX:
        return 0

    candidate_tokens: List[str] = []
    seen_tokens: set[str] = set()
    for keyword, forms in keyword_forms.items():
        for token in {keyword, *forms}:
            token_lower = token.lower()
            if token_lower in seen_tokens:
                continue
            if token_lower in SOURCE_TOKEN_INDEX:
                candidate_tokens.append(token_lower)
                seen_tokens.add(token_lower)
            elif token_lower.endswith("s") and token_lower[:-1] in SOURCE_TOKEN_INDEX:
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
        for source in SOURCE_TOKEN_INDEX.get(token, ()):  # type: ignore[arg-type]
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
        _normalize_source_input(doc.metadata.get("source"))
        for doc in docs
        if doc.metadata.get("source")
    }

    appended = 0
    for source in candidate_sources:
        if source in existing_sources:
            continue

        records = _load_source_records(source)
        if not records:
            continue

        candidates: List[Tuple[_InlineDoc, int, int]] = []
        for text, metadata in records:
            document = _InlineDoc(content=text, metadata=dict(metadata))
            matches = _keyword_match_count(document, keyword_forms)
            path_matches = _source_keyword_match_count(document, keyword_forms)
            if matches == 0 and path_matches == 0:
                continue
            if matches < max(1, required_matches - 1) and path_matches == 0:
                continue
            candidates.append((document, matches, path_matches))

        if not candidates:
            continue

        candidates.sort(
            key=lambda entry: (
                entry[2],  # path matches first
                entry[1],  # total keyword matches
                len(entry[0].page_content or ""),
            ),
            reverse=True,
        )

        added_for_source = 0
        for document, matches, path_matches in candidates[:SOURCE_FALLBACK_DOCS_PER_SOURCE]:
            document.metadata["_retrieval_rank"] = len(docs)
            document.metadata["_keyword_matches"] = matches
            document.metadata["_path_keyword_matches"] = path_matches
            docs.append(document)
            appended += 1
            added_for_source += 1
            if appended >= SOURCE_FALLBACK_MAX_DOCS:
                break

        if added_for_source:
            existing_sources.add(source)

        if appended >= SOURCE_FALLBACK_MAX_DOCS:
            break

    return appended


app = FastAPI(title="Consulta Técnica RAG", version="1.0.0")

ALLOWED_ORIGINS = [
    "http://127.0.0.1:8000",
    "http://192.168.13.162:8000",
    "http://81.33.21.33:6767",
]

app.add_middleware(
    CORSMiddleware,
    allow_origins=ALLOWED_ORIGINS,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.post("/consulta", response_model=ConsultaResponse)
def consultar(req: ConsultaRequest):
    if not req.pregunta.strip():
        raise HTTPException(status_code=400, detail="La pregunta no puede estar vacía.")

    # Detectar saludos/agradecimientos/despedidas antes del RAG
    tipo_saludo = _detectar_saludo(req.pregunta)
    if tipo_saludo:
        return ConsultaResponse(
            respuesta=_GREETING_RESPONSES[tipo_saludo],
            fuentes=[],
            found=True,
            greeting=True,
        )

    normalized_query = normalize_and_expand_query(req.pregunta)
    keywords = _extract_keywords(_strip_accents(req.pregunta))
    logger.info("Consulta: %s", req.pregunta[:200])
    logger.info("Keywords: %s", keywords)

    # Nota: La conversación se envía en el prompt a Gemini para coherencia,
    # pero NO se usa para la búsqueda vectorial (evita contaminación entre temas)

    keyword_forms = {keyword: _keyword_forms(keyword) for keyword in keywords}
    required_matches = 1 if len(keywords) <= 1 else 2
    root_norm = _normalize_source_input(req.root_folder)
    prefix_filters: list[str] = []
    if req.query_paths:
        raw_parts = [part.strip() for part in req.query_paths.split(",")]
        for part in raw_parts:
            normalized = _normalize_source_input(part)
            if normalized:
                prefix_filters.append(normalized)
        if prefix_filters:
            logger.debug("Filtro query_paths normalizado: %s", prefix_filters)

    # Si se especifican rutas, filtrar por metadato 'source' en Chroma
    documentos = None
    rutas = (req.rutas or [])
    rutas_norm: list[str] = []
    if rutas:
        rutas_norm = [_normalize_source_input(p) for p in rutas if p]
        rutas_norm = [ruta for ruta in rutas_norm if ruta]
        if not rutas_norm:
            raise HTTPException(
                status_code=400,
                detail="Las rutas proporcionadas no coinciden con ROOT_FOLDER. Verifica tu selección.",
            )
        try:
            documentos = db.similarity_search(
                normalized_query,
                k=RETRIEVAL_K,
                filter={"source": {"$in": rutas_norm}},
            )
            if not documentos:
                documentos = db.similarity_search(
                    req.pregunta,
                    k=RETRIEVAL_K,
                    filter={"source": {"$in": rutas_norm}},
                )
        except Exception as exc:
            raise HTTPException(status_code=400, detail=f"Filtro inválido o error en la búsqueda: {exc}")
    else:
        documentos = retriever.invoke(normalized_query)
        if not documentos:
            documentos = retriever.invoke(req.pregunta)

    documentos = documentos or []
    for idx, doc in enumerate(documentos):
        doc.metadata["_retrieval_rank"] = idx
        doc.metadata["_keyword_matches"] = _keyword_match_count(doc, keyword_forms) if keywords else 0
        doc.metadata["_path_keyword_matches"] = _source_keyword_match_count(doc, keyword_forms) if keywords else 0

    def is_strong(doc) -> bool:
        if not keywords:
            return True
        matches = doc.metadata.get("_keyword_matches", 0)
        path_matches = doc.metadata.get("_path_keyword_matches", 0)
        if matches < required_matches:
            return False
        if path_matches > 0:
            return True
        needed = min(max(required_matches + 1, len(keyword_forms)), 3)
        return matches >= needed

    appended = 0
    if keywords:
        target_keyword_docs = min(TARGET_KEYWORD_DOCS, len(keyword_forms) or 1)
        qualified = sum(1 for doc in documentos if is_strong(doc))
        if qualified < target_keyword_docs:
            extra_kwargs: dict[str, object] = {"k": FALLBACK_K}
            if rutas_norm:
                extra_kwargs["filter"] = {"source": {"$in": rutas_norm}}
            extra_docs = db.similarity_search(req.pregunta, **extra_kwargs)
            existing_signatures = {
                (
                    doc.metadata.get("source"),
                    doc.metadata.get("source_hash"),
                    doc.page_content,
                )
                for doc in documentos
            }
            for idx_extra, extra_doc in enumerate(extra_docs):
                signature = (
                    extra_doc.metadata.get("source"),
                    extra_doc.metadata.get("source_hash"),
                    extra_doc.page_content,
                )
                if signature in existing_signatures:
                    continue
                matches = _keyword_match_count(extra_doc, keyword_forms)
                if matches < required_matches:
                    continue
                path_matches = _source_keyword_match_count(extra_doc, keyword_forms)
                if path_matches == 0 and matches < min(max(required_matches + 1, len(keyword_forms)), 3):
                    continue
                extra_doc.metadata["_retrieval_rank"] = len(documentos) + idx_extra
                extra_doc.metadata["_keyword_matches"] = matches
                extra_doc.metadata["_path_keyword_matches"] = path_matches
                documentos.append(extra_doc)
                existing_signatures.add(signature)
                appended += 1
                if is_strong(extra_doc):
                    qualified += 1
                if appended >= MAX_FALLBACK_APPEND:
                    break
    if appended:
        logger.info("Añadidos %d fragmentos por coincidencia de palabras clave.", appended)

    source_appended = _augment_with_source_candidates(documentos, keyword_forms, required_matches)
    if source_appended:
        logger.info("Añadidos %d fragmentos por coincidencia de ruta.", source_appended)

    if root_norm:
        documentos = [
            doc
            for doc in documentos
            if _normalize_source_input(doc.metadata.get("source")).startswith(root_norm)
        ]
    if prefix_filters:
        documentos = [
            doc
            for doc in documentos
            if any(
                _normalize_source_input(doc.metadata.get("source")).startswith(prefix)
                for prefix in prefix_filters
            )
        ]
    # Guardar estado pre-retry para poder restaurar si el retry no mejora.
    _pre_retry_docs = list(documentos) if documentos else []
    _pre_retry_kw = list(keywords)
    _pre_retry_kf = dict(keyword_forms)
    _pre_retry_rm = required_matches

    # Limpiar docs sin ningún match de keywords (comportamiento existente).
    if keywords and documentos and not any(
        doc.metadata.get("_keyword_matches", 0) > 0 or doc.metadata.get("_path_keyword_matches", 0) > 0
        for doc in documentos
    ):
        documentos = []

    # Evaluar cobertura: ¿cuántas keywords distintas matchean en algún doc?
    # Si pocas keywords están representadas, los docs probablemente sean off-topic
    # (ej. matchean "actualizar" pero no "borrar"/"disco"/"memoria").
    _tried_order_keywords = False
    _needs_retry = not documentos
    if not _needs_retry and keywords and len(keyword_forms) >= 4 and documentos:
        _covered = sum(
            1 for forms in keyword_forms.values()
            if any(
                any(
                    f in (d.page_content or "").lower() or f in str(d.metadata.get("source", "")).lower()
                    for f in forms
                )
                for d in documentos
            )
        )
        if _covered / len(keyword_forms) < 0.5:
            logger.info(
                "[retry] Cobertura keywords baja (%d/%d=%.0f%%); evaluando retry con orden de aparición.",
                _covered, len(keyword_forms), _covered / len(keyword_forms) * 100,
            )
            _needs_retry = True

    # --- Retry pre-LLM con keywords por orden de aparición ---
    if _needs_retry and keywords:
        keywords_by_order = _extract_keywords(_strip_accents(req.pregunta), prioritize_length=False)
        if set(keywords_by_order) != set(keywords):
            _tried_order_keywords = True
            logger.info("[pre-retry] Keywords originales: %s", keywords)
            logger.info("[pre-retry] Reintentando con keywords por orden de aparición: %s", keywords_by_order)
            keywords = keywords_by_order
            keyword_forms = {kw: _keyword_forms(kw) for kw in keywords}
            required_matches = 1 if len(keywords) <= 1 else 2

            documentos = retriever.invoke(normalized_query)
            if not documentos:
                documentos = retriever.invoke(req.pregunta)
            documentos = documentos or []

            for idx, doc in enumerate(documentos):
                doc.metadata["_retrieval_rank"] = idx
                doc.metadata["_keyword_matches"] = _keyword_match_count(doc, keyword_forms)
                doc.metadata["_path_keyword_matches"] = _source_keyword_match_count(doc, keyword_forms)

            # Fallback ampliado
            target_kw_docs = min(TARGET_KEYWORD_DOCS, len(keyword_forms) or 1)
            qualified_r = sum(1 for doc in documentos if doc.metadata.get("_keyword_matches", 0) >= required_matches)
            if qualified_r < target_kw_docs:
                extra_docs = db.similarity_search(req.pregunta, k=FALLBACK_K)
                existing_sigs = {
                    (d.metadata.get("source"), d.metadata.get("source_hash"), d.page_content)
                    for d in documentos
                }
                retry_appended = 0
                for idx_e, extra_doc in enumerate(extra_docs):
                    sig = (extra_doc.metadata.get("source"), extra_doc.metadata.get("source_hash"), extra_doc.page_content)
                    if sig in existing_sigs:
                        continue
                    matches = _keyword_match_count(extra_doc, keyword_forms)
                    if matches < required_matches:
                        continue
                    path_matches = _source_keyword_match_count(extra_doc, keyword_forms)
                    if path_matches == 0 and matches < min(max(required_matches + 1, len(keyword_forms)), 3):
                        continue
                    extra_doc.metadata["_retrieval_rank"] = len(documentos) + idx_e
                    extra_doc.metadata["_keyword_matches"] = matches
                    extra_doc.metadata["_path_keyword_matches"] = path_matches
                    documentos.append(extra_doc)
                    existing_sigs.add(sig)
                    retry_appended += 1
                    if retry_appended >= MAX_FALLBACK_APPEND:
                        break
                if retry_appended:
                    logger.info("[retry] Añadidos %d fragmentos en segunda pasada.", retry_appended)

            source_appended_r = _augment_with_source_candidates(documentos, keyword_forms, required_matches)
            if source_appended_r:
                logger.info("[retry] Añadidos %d fragmentos por ruta en segunda pasada.", source_appended_r)

            if keywords and not any(
                doc.metadata.get("_keyword_matches", 0) > 0 or doc.metadata.get("_path_keyword_matches", 0) > 0
                for doc in documentos
            ):
                documentos = []

            # Si el retry no mejoró y teníamos docs originales, restaurar.
            if not documentos and _pre_retry_docs:
                logger.info("[retry] Reintento sin resultados; restaurando documentos originales.")
                documentos = _pre_retry_docs
                keywords = _pre_retry_kw
                keyword_forms = _pre_retry_kf
                required_matches = _pre_retry_rm

    documentos = _prioritize_docs(documentos, keyword_forms, required_matches, MAX_CONTEXT_DOCS)
    contexto = build_context(documentos)

    if not contexto:
        debug_mode = _is_debug_responses()
        _debug_info = None
        if debug_mode:
            _debug_info = {
                "keywords": keywords,
                "keyword_forms": {k: sorted(v) for k, v in keyword_forms.items()},
                "docs_retrieved": 0,
                "reason": "Sin contexto: no se encontraron documentos relevantes tras filtrado.",
            }
        return ConsultaResponse(
            respuesta="No encuentro información relevante en la base de conocimiento.",
            fuentes=[],
            found=True if debug_mode else False,
            debug_info=_debug_info,
        )

    question_text = req.pregunta
    conversation_block = _format_conversation_for_prompt(req.conversation, req.pregunta)
    if conversation_block:
        question_text = (
            "Historial de conversación (solo para coherencia y desambiguación, no como fuente de verdad):\n"
            f"{conversation_block}\n\n"
            "Pregunta actual:\n"
            f"{req.pregunta}"
        )

    prompt_text = PROMPT_TEMPLATE.format(
        system_prompt=SYSTEM_PROMPT,
        context=contexto,
        question=question_text
    )
    try:
        respuesta = llm.generate_content(prompt_text)
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"Error al invocar Gemini: {exc}")

    fuentes = [
        f"{doc.metadata.get('source')} (fragmento: {doc.page_content[:150].replace(chr(10), ' ')}...)"
        for doc in documentos
    ]

    usage_data: dict[str, int] | None = None
    usage_obj = getattr(respuesta, "usage_metadata", None)
    if usage_obj:
        if isinstance(usage_obj, dict):
            usage_data = {
                key: int(value)
                for key, value in usage_obj.items()
                if isinstance(value, (int, float))
            }
        else:
            prompt_tokens = getattr(usage_obj, "prompt_token_count", None)
            candidates_tokens = getattr(usage_obj, "candidates_token_count", None)
            total_tokens = getattr(usage_obj, "total_token_count", None)
            usage_data = {
                key: value
                for key, value in {
                    "prompt_token_count": prompt_tokens,
                    "candidates_token_count": candidates_tokens,
                    "total_token_count": total_tokens,
                }.items()
                if isinstance(value, (int, float))
            }
    if usage_data == {}:
        usage_data = None

    contenido = getattr(respuesta, "text", None) or str(respuesta)

    # Detectar si el LLM indica que no tiene información.
    # Se usan regex para cubrir variaciones de conjugación y fraseo.
    # IMPORTANTE: el LLM a veces usa formas indirectas como
    # "no dispongo de una nota técnica específica" en vez de
    # "no dispongo de información", así que los regex deben ser
    # lo suficientemente flexibles con .* entre el verbo y el objeto.
    _NO_INFO_REGEXES = [
        r"no\s+(se\s+)?(encuentra|encontr[ée]|encuentro|hall[aoó])\s+informaci[oó]n",
        r"no\s+(tengo|tenemos|tiene|tienen)\s+(la\s+)?informaci[oó]n",
        r"no\s+(dispongo|disponemos|dispone)\s+de\s+informaci[oó]n",
        r"no\s+(dispongo|disponemos|dispone)\b.{0,80}\b(nota|documento|manual|informaci[oó]n|datos|contexto)",
        r"no\s+(hay|existe)\s+informaci[oó]n",
        r"no\s+(tengo|tenemos|tiene)\s+(los\s+)?datos",
        r"no\s+(cuento|contamos|cuenta)\s+con\s+informaci[oó]n",
        r"no\s+tengo\s+ningun[ao]",
        r"no\s+(se\s+)?(encuentra|encontr[ée]|encuentro)\b.*\binformaci[oó]n",
        r"no\s+(poseo|poseemos)\s+informaci[oó]n",
        r"no\s+figura\b.*\binformaci[oó]n",
        r"la\s+informaci[oó]n\s+(no\s+)?(est[aá]|se\s+encuentra)\s+disponible",
        r"no\s+(incluye|incluyen|contiene|contienen)\b.{0,60}\b(nota|documento|manual|informaci[oó]n)",
    ]
    contenido_lower = contenido.lower()
    found = not any(re.search(pat, contenido_lower) for pat in _NO_INFO_REGEXES)

    # --- Post-LLM retry: si found=False, reintentar con keywords por orden de aparición ---
    # Solo si no se intentó ya en el retry pre-LLM.
    if not found and not _tried_order_keywords and keywords:
        _plr_kw = _extract_keywords(_strip_accents(req.pregunta), prioritize_length=False)
        if set(_plr_kw) != set(keywords):
            logger.info("[post-llm-retry] found=False con keywords %s; reintentando con orden: %s", keywords, _plr_kw)
            _plr_kf = {kw: _keyword_forms(kw) for kw in _plr_kw}
            _plr_rm = 1 if len(_plr_kw) <= 1 else 2

            _plr_docs = retriever.invoke(normalized_query)
            if not _plr_docs:
                _plr_docs = retriever.invoke(req.pregunta)
            _plr_docs = _plr_docs or []

            for idx, doc in enumerate(_plr_docs):
                doc.metadata["_retrieval_rank"] = idx
                doc.metadata["_keyword_matches"] = _keyword_match_count(doc, _plr_kf)
                doc.metadata["_path_keyword_matches"] = _source_keyword_match_count(doc, _plr_kf)

            # Fallback ampliado
            _plr_target = min(TARGET_KEYWORD_DOCS, len(_plr_kf) or 1)
            _plr_qual = sum(1 for d in _plr_docs if d.metadata.get("_keyword_matches", 0) >= _plr_rm)
            if _plr_qual < _plr_target:
                _plr_extra = db.similarity_search(req.pregunta, k=FALLBACK_K)
                _plr_sigs = {
                    (d.metadata.get("source"), d.metadata.get("source_hash"), d.page_content)
                    for d in _plr_docs
                }
                _plr_app = 0
                for idx_e, ed in enumerate(_plr_extra):
                    sig = (ed.metadata.get("source"), ed.metadata.get("source_hash"), ed.page_content)
                    if sig in _plr_sigs:
                        continue
                    m = _keyword_match_count(ed, _plr_kf)
                    if m < _plr_rm:
                        continue
                    pm = _source_keyword_match_count(ed, _plr_kf)
                    if pm == 0 and m < min(max(_plr_rm + 1, len(_plr_kf)), 3):
                        continue
                    ed.metadata["_retrieval_rank"] = len(_plr_docs) + idx_e
                    ed.metadata["_keyword_matches"] = m
                    ed.metadata["_path_keyword_matches"] = pm
                    _plr_docs.append(ed)
                    _plr_sigs.add(sig)
                    _plr_app += 1
                    if _plr_app >= MAX_FALLBACK_APPEND:
                        break
                if _plr_app:
                    logger.info("[post-llm-retry] Añadidos %d fragmentos fallback.", _plr_app)

            _augment_with_source_candidates(_plr_docs, _plr_kf, _plr_rm)
            _plr_docs = _prioritize_docs(_plr_docs, _plr_kf, _plr_rm, MAX_CONTEXT_DOCS)
            _plr_ctx = build_context(_plr_docs)

            if _plr_ctx:
                _plr_prompt = PROMPT_TEMPLATE.format(
                    system_prompt=SYSTEM_PROMPT,
                    context=_plr_ctx,
                    question=question_text,
                )
                try:
                    _plr_resp = llm.generate_content(_plr_prompt)
                    _plr_content = getattr(_plr_resp, "text", None) or str(_plr_resp)
                    _plr_found = not any(re.search(pat, _plr_content.lower()) for pat in _NO_INFO_REGEXES)
                    if _plr_found:
                        logger.info("[post-llm-retry] Retry exitoso con keywords por orden de aparición.")
                        contenido = _plr_content
                        found = True
                        documentos = _plr_docs
                        keywords = _plr_kw
                        keyword_forms = _plr_kf
                        fuentes = [
                            f"{doc.metadata.get('source')} (fragmento: {doc.page_content[:150].replace(chr(10), ' ')}...)"
                            for doc in documentos
                        ]
                        # Actualizar usage con los datos del retry
                        _plr_usage = getattr(_plr_resp, "usage_metadata", None)
                        if _plr_usage:
                            if isinstance(_plr_usage, dict):
                                usage_data = {k: int(v) for k, v in _plr_usage.items() if isinstance(v, (int, float))}
                            else:
                                _ru = {
                                    "prompt_token_count": getattr(_plr_usage, "prompt_token_count", None),
                                    "candidates_token_count": getattr(_plr_usage, "candidates_token_count", None),
                                    "total_token_count": getattr(_plr_usage, "total_token_count", None),
                                }
                                usage_data = {k: v for k, v in _ru.items() if isinstance(v, (int, float))} or None
                    else:
                        logger.info("[post-llm-retry] Retry también resultó en found=False.")
                except Exception:
                    logger.warning("[post-llm-retry] Error al invocar Gemini en retry.", exc_info=True)

    debug_mode = _is_debug_responses()
    _debug_info = None
    if debug_mode:
        _debug_info = {
            "keywords": keywords,
            "keyword_forms": {k: sorted(v) for k, v in keyword_forms.items()},
            "docs_retrieved": len(documentos),
            "docs_detail": [
                {
                    "source": doc.metadata.get("source", ""),
                    "keyword_matches": doc.metadata.get("_keyword_matches", 0),
                    "path_matches": doc.metadata.get("_path_keyword_matches", 0),
                    "rank": doc.metadata.get("_retrieval_rank", 0),
                    "snippet": (doc.page_content or "")[:200],
                }
                for doc in documentos
            ],
            "found_original": found,
        }
        found = True  # Forzar para que el frontend muestre la respuesta real

    # Log de resultado
    sources_short = list({doc.metadata.get("source", "") for doc in documentos})
    logger.info("Resultado: found=%s, docs=%d, fuentes=%s", found, len(documentos), sources_short)

    return ConsultaResponse(respuesta=contenido, fuentes=fuentes, usage=usage_data, found=found, debug_info=_debug_info)


# --- Endpoint para explorar la estructura de PDFs/HTML ---
def _build_tree(root: str) -> TreeNode:
    root_path = os.path.abspath(root)
    if not os.path.exists(root_path):
        raise HTTPException(status_code=400, detail=f"La ruta no existe: {root_path}")

    def walk(current: str) -> TreeNode:
        children: list[TreeNode] = []
        try:
            entries = sorted(os.listdir(current))
        except PermissionError:
            entries = []
        for entry in entries:
            full_path = os.path.join(current, entry)
            if os.path.isdir(full_path):
                children.append(walk(full_path))
        return TreeNode(
            name=os.path.basename(current) or current,
            path=current,
            type="directory",
            children=children or None,
        )

    return walk(root_path)


@app.get("/estructura", response_model=TreeNode)
def obtener_estructura(base: Optional[str] = None):
    target = base or DOCUMENTS_FOLDER
    return _build_tree(target)


# --- Endpoint para ingesta/sincronización ---
def _refresh_vectorstore():
    """Reinicializa el vectorstore y retriever para ver nuevos documentos sin reiniciar el servidor."""
    global db, retriever
    try:
        db = open_vectorstore(DB_PATH, embedding_model)
        retriever = db.as_retriever(search_kwargs={"k": 3})
    except Exception as exc:
        # No interrumpir si hay un problema; quedará el estado anterior
        logger.error("No se pudo refrescar el vectorstore: %s", exc)


def _sync_job(folder: str):
    if sincronizar_pdf_con_chroma is None:
        logger.warning("La función de sincronización no está disponible. Verifica 'ingesta.py'.")
        return
    try:
        sincronizar_pdf_con_chroma(folder)
    finally:
        _refresh_vectorstore()


@app.post("/ingesta/sync")
def ingesta_sync(background_tasks: BackgroundTasks, req: IngestaRequest | None = None):
    if sincronizar_pdf_con_chroma is None:
        raise HTTPException(status_code=500, detail="Sincronización no disponible. Revisa 'ingesta.py'.")
    folder = (req.carpeta if req else None) or DOCUMENTS_FOLDER
    if not os.path.isabs(folder):
        folder = os.path.abspath(folder)
    if not os.path.exists(folder):
        raise HTTPException(status_code=400, detail=f"La carpeta no existe: {folder}")

    background_tasks.add_task(_sync_job, folder)
    return {"status": "enqueued", "folder": folder}


# --- Endpoint para listar documentos indexados ---
@app.get("/documentos")
def listar_documentos(limit: int | None = None):
    """Retorna la lista de rutas únicas de documentos indexados en Chroma."""
    collection = get_chroma_collection(db)
    if not collection:
        return {"documentos": [], "total": 0}

    seen: set[str] = set()
    sources: list[str] = []
    offset = 0
    batch_size = 500

    while True:
        try:
            res = collection.get(include=["metadatas"], limit=batch_size, offset=offset)
        except TypeError:
            res = collection.get(include=["metadatas"])
        metadatas = res.get("metadatas") or []
        if not metadatas:
            break
        for md in metadatas:
            src = (md or {}).get("source")
            if not src:
                continue
            norm = os.path.normcase(os.path.abspath(src))
            if norm not in seen:
                seen.add(norm)
                sources.append(src)
            if limit is not None and len(sources) >= limit:
                return {"documentos": sources, "total": len(sources)}
        if len(metadatas) < batch_size:
            break
        offset += batch_size

    return {"documentos": sources, "total": len(sources)}


# --- Endpoint para consultar el log ---
@app.get("/log", response_class=PlainTextResponse)
def obtener_log(tail: int | None = None):
    """Retorna el contenido del archivo de log.

    Parámetros:
        tail: si se indica, retorna solo las últimas N líneas.
    """
    from app_logging import _LOG_FILE

    if not os.path.isfile(_LOG_FILE):
        raise HTTPException(status_code=404, detail="El archivo de log no existe todavía.")

    try:
        with open(_LOG_FILE, "r", encoding="utf-8", errors="replace") as f:
            if tail is not None and tail > 0:
                lines = f.readlines()
                return "".join(lines[-tail:])
            return f.read()
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Error al leer el log: {exc}")
