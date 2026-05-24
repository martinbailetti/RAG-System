"""Utilidades auxiliares: detección de saludos, formato de conversación, debug y regex no-info."""

import os
import re
from typing import Optional, List, Dict, Any, Set

from ingesta.query_core import _strip_accents
from endpoint_models import ChatMessage


# ── Patrones de saludos ─────────────────────────────────────────────────────
# Usan anclas ^...$ para que solo coincidan con mensajes que sean EXCLUSIVAMENTE
# un saludo ("hola", "buenas tardes"). Prompts mixtos como
# "Hola, qué color tiene el cielo?" NO coinciden y pasan al pipeline RAG.
# NOTA: los patrones usan formas SIN acento porque _detectar_saludo
# normaliza el texto con _strip_accents antes de evaluar.
_GREETING_PATTERNS = [
    (r"^\s*(hola|hey|buenas?|buenos?\s*(dias?|tardes?|noches?)|buenas?\s*(dias?|tardes?|noches?)|hola[,;]?\s*(buenas?\s*(tardes?|noches?|dias?)?)?|que\s*tal|saludos?|hi|hello|buenas?\s*tardes?|buenas?\s*noches?|buenas?\s*dias?)\s*[,;]?\s*(baldric)?\s*[.!?]*\s*$", "saludo"),
    (r"^\s*(gracias|muchas\s*gracias|te\s*agradezco|thanks|thank\s*you)[\s,;!.?]*(genial|perfecto|excelente|estupendo|fenomenal|muy\s*bien|ok|guay|chevere|de\s*acuerdo|entendido|fantástico|fantastico|super|buenisimo|buenísimo|maravilloso)?\s*(baldric)?\s*[!.?]*\s*$", "gracias"),
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


# ── Regex para detectar "no tengo información" del LLM ─────────────────────
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

# ── Regex para detectar que el LLM ofrece alternativas pese a no tener info directa ──
# Si el LLM dice "no tengo información" pero a continuación ofrece opciones
# relacionadas, la respuesta sigue siendo útil → found=True.
_OFFERS_ALTERNATIVES_REGEXES = [
    r"la\s+informaci[oó]n\s+(disponible|que\s+tengo|que\s+tenemos)\s+se\s+(centra|enfoca|refiere)",
    r"(sin\s+embargo|no\s+obstante|ahora\s+bien)[,;]?\s+.{5,}",
    r"lo\s+que\s+s[ií]\s+(tengo|tenemos|hay|encuentro|est[aá]\s+disponible)",
    r"(puedo|podemos)\s+(indicar|ofrecer|mencionar|sugerir)",
    r"s[ií]\s+(tengo|tenemos|hay|existe[n]?|encuentro)\s+.{3,60}\b(relacionad[oa]s?|disponibles?)",
    r"se\s+(centra|enfoca|refiere)\s+(en|a)\b",
]


# ── Funciones auxiliares ────────────────────────────────────────────────────

def _is_debug_responses() -> bool:
    """Devuelve True si RAG_DEBUG_RESPONSES está activo.

    Cuando está activo:
    - found siempre es True → el frontend muestra la respuesta real del LLM
      en vez del mensaje fijo "No encontramos una respuesta..."
    - Se incluye debug_info con keywords, formas, fuentes y scoring.
    Activar con RAG_DEBUG_RESPONSES=1 en .env o entorno.
    """
    return os.environ.get("RAG_DEBUG_RESPONSES", "0").strip().lower() in ("1", "true", "yes", "on")


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


def _safe_int(value: str, fallback: int) -> int:
    try:
        return int(value)
    except Exception:
        return fallback


# ── Detección de búsqueda de documentos ──────────────────────────────────────
# Detecta preguntas que buscan LISTADOS de documentos: ¿tenemos manuales para X?
# Las distingue de preguntas sobre un TEMA concreto: ¿cómo se instala X?

_BUSQUEDA_DOCS_PATTERNS = [
    r"\b(tenemos?|hay|existen?|disponemos\s+de|tienes?|tienen)\b.{0,50}\b(manual|manuales|documento|documentos|nota[s]?\s*t[eé]cnica[s]?|ficha[s]?|gu[íi]a[s]?|informacion|info|documentacion)\b",
    r"\b(qu[eé]|cu[aá]les?)\s+(manuales?|documentos?|notas?\s+t[eé]cnicas?|fichas?|gu[íi]as?|informacion|info|documentacion)\s+(tenemos|hay|existen?|disponemos)\b",
    r"\b(busca[r]?|lista[r]?|muestra[r]?|dame|d[aá]me)\s+(los?\s+|la[s]?\s+)?(manuales?|documentos?|notas?\s+t[eé]cnicas?|gu[íi]as?|informacion|info|documentacion)\b",
    r"^.{0,30}\b(manuales?|documentos?|notas?\s+t[eé]cnicas?|informacion|info|documentacion)\s+(para|de|sobre|del?)\b",
    r"\b(tenemos?|hay|existen?)\b.{0,40}\b(documentaci[oó]n|informacion|info)\b",
]


def _es_busqueda_documentos(texto: str) -> bool:
    """Devuelve True si la pregunta es una búsqueda de listado de documentos.

    Ejemplos True:  "¿Tenemos manuales para la ruleta?" / "¿Hay documentos de la balanza?"
    Ejemplos False: "¿Cómo se instala la ruleta?" / "¿Cuál es el procedimiento de apertura?"
    """
    texto_norm = _strip_accents(texto.lower().strip())
    for pattern in _BUSQUEDA_DOCS_PATTERNS:
        if re.search(pattern, texto_norm, re.IGNORECASE):
            return True
    return False


def _build_fuentes(documentos: list, keyword_forms: dict, all_sources: bool) -> list[str]:
    """Construye la lista de fuentes a devolver en la respuesta.

    Cuando all_sources=True (búsqueda de documentos):
      - Deduplica por ruta de archivo (sin fragmento repetido).
      - Filtra solo fuentes cuya ruta contenga al menos una keyword específica
        (excluye keywords genéricas como "manual", "documento", etc.).
      - Si ninguna fuente coincide por ruta, devuelve todas deduplicadas (fallback).
    Cuando all_sources=False:
      - Devuelve todas las fuentes con fragmento (el frontend ya recorta a 1).
    """
    if not documentos:
        return []

    if not all_sources:
        return [
            f"{doc.metadata.get('source')} (fragmento: {doc.page_content[:150].replace(chr(10), ' ')}...)"
            for doc in documentos
        ]

    # Deduplicar por ruta de archivo
    seen: Set[str] = set()
    unique_docs = []
    for doc in documentos:
        src = (doc.metadata.get("source") or "").strip()
        if src and src not in seen:
            seen.add(src)
            unique_docs.append(doc)

    # Keywords genéricas que aparecen en la mayoría de rutas y no discriminan
    _GENERIC_DOC_KEYWORDS = {
        "manual", "manuales", "documento", "documentos", "nota", "notas",
        "tecnica", "tecnicas", "tecnico", "tecnicos", "ficha", "fichas",
        "guia", "guias", "usuario", "usuarios", "informacion", "info",
        "documentacion",
    }

    # Filtrar keyword_forms excluyendo las claves genéricas
    specific_forms = {
        k: v for k, v in keyword_forms.items()
        if _strip_accents(k.lower()) not in _GENERIC_DOC_KEYWORDS
    }

    # Filtrar por keywords específicas en ruta
    if specific_forms:
        filtered = []
        for doc in unique_docs:
            src_lower = _strip_accents((doc.metadata.get("source") or "").lower())
            if any(
                form in src_lower
                for forms in specific_forms.values()
                for form in forms
            ):
                filtered.append(doc)
        # Fallback: si ninguna coincide por ruta, devolver todas las únicas
        if filtered:
            unique_docs = filtered

    return [
        f"{doc.metadata.get('source')} (fragmento: {doc.page_content[:150].replace(chr(10), ' ')}...)"
        for doc in unique_docs
    ]


def _format_conversation_for_prompt(raw_conversation: Optional[List[Dict[str, Any]]], pregunta: str) -> str:
    if not raw_conversation:
        return ""

    max_chars = _safe_int(os.environ.get("RAG_MAX_CONVERSATION_CHARS", "4000"), 4000)
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


def check_no_info(contenido: str) -> bool:
    """Comprueba si la respuesta del LLM indica que no tiene información.

    Returns:
        True si el LLM encontró información (found=True).
        False si el LLM indica que no tiene información (found=False).

    Si el LLM dice "no tengo información" pero ofrece alternativas
    ("la información disponible se centra en...", "sin embargo...", etc.),
    se considera found=True porque la respuesta sigue siendo útil.
    """
    contenido_lower = contenido.lower()
    has_no_info = any(re.search(pat, contenido_lower) for pat in _NO_INFO_REGEXES)
    if not has_no_info:
        return True  # El LLM no dice que falte info → found=True

    # El LLM dice que no tiene info, pero ¿ofrece alternativas?
    if any(re.search(pat, contenido_lower) for pat in _OFFERS_ALTERNATIVES_REGEXES):
        return True  # Ofrece alternativas → found=True

    return False  # Realmente no encontró nada → found=False


# ── Detección de petición de aclaración ─────────────────────────────────────
_CLARIFICATION_RE = re.compile(r"^\s*\[ACLARACI[OÓ]N\]\s*", re.IGNORECASE)


def check_clarification(contenido: str) -> tuple[bool, str]:
    """Detecta si la respuesta del LLM pide aclaración al usuario.

    El LLM prefija su respuesta con [ACLARACIÓN] cuando la pregunta es ambigua
    y hay múltiples opciones posibles en el contexto.

    Returns:
        (True, texto_sin_prefijo) si el LLM pide aclaración.
        (False, texto_original) si la respuesta es normal.
    """
    match = _CLARIFICATION_RE.match(contenido)
    if match:
        return True, contenido[match.end():].strip()
    return False, contenido
