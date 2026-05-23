"""Extracción de keywords, stopwords y sinónimos de dominio para el pipeline RAG."""

import re
import unicodedata
from typing import Optional, List, Dict, Any


def _strip_accents(text: str) -> str:
    """Elimina acentos/diacríticos de un texto (NFD + quitar combining chars)."""
    normalized = unicodedata.normalize("NFKD", text)
    return "".join(ch for ch in normalized if not unicodedata.combining(ch))


MAX_EXTRACT_KEYWORDS = 7


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
    # ── Palabras cortas (3 chars) genéricas que no son términos técnicos ──
    # Necesarias tras bajar el filtro de longitud de < 4 a < 3.
    # Sin ellas, queries como "hay que ver" generarían keywords basura.
    "hay",
    "ver",
    "fue",
    "han",
    "van",
    "dar",
    "dos",
    "fin",
    "vez",
    "tan",
    "dio",
    "hoy",
    "iba",
    "ido",
    "oir",
    "pie",
    "sal",
    "sol",
    "sur",
    "voz",
    "luz",
    "ley",
    "rio",
    "oro",
    "ave",
    "ahi",
    "ahí",
    "asi",
    "así",
    "aun",
    "aún",
    "mal",
    "sea",
    "era",
    "via",
    "vía",
    "ves",
    "ven",
    "son",
    "mas",
    "bien",  # 4 chars pero ya estaba implícito por < 4
}


# Sinónimos de dominio: expanden keyword forms para mejorar el matching
# entre el vocabulario del usuario y el de los documentos técnicos.
# Términos técnicos muy específicos que, cuando aparecen en el query,
# deben priorizar fuertemente documentos cuya ruta/nombre los contenga.
# Útil para términos cortos (3 chars) o acrónimos que el modelo de embeddings
# infrapondera pero que indican claramente el documento destino.
PRIORITY_KEYWORDS: set[str] = {
    "sas",
    "soja",
    "ticketserver",
    "tito",
    "aft",
    "smc",
    "gbg",
    "cas",
    "ready2b",
    "ukbar",
    "mdc",
    "ccm",
    "gistra",
    "wigos",
    "hitachi",
    "ukbar",
    "r2b",
    "atm",
    "orenes",
    "aristocrat",
    "ainsworth",
    "puloon",
    "bill2bill",
    "b2b",
    "contactless",
    "blackjack",
    "crane",
    "jackpot",
    "big7",
    "twin",
    "comatel",
    "poker",
    "raspa",
    "nv200",
    "dlx",
    "bito",
    "bingball",
    "egasa",
    "sign",
    "panasonic",
    "telegram",
    "extrafeatures",
    "pinpad",
    "paytef",
    "gli",
    "epic",
    "edge",
    "custom",
    "forwardsystems",
    "sportium",
    "stackar",
    "wigos",
    "sashub",
    "wipe",
    "resident",
    "ipro",
    "bnr"
    
}


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
    "maquina": {"ccm", "mdc", "cambio"},
    "cambio": {"ccm", "mdc", "maquina"},
    "ccm": {"maquina", "mdc", "cambio"},
    "mdc": {"maquina", "ccm", "cambio"},
    "contrasena": {"clave", "password"},
    "clave": {"contrasena", "password"},
    "password": {"contrasena", "clave"},
    "magicred": {"magic"},
    "magic": {"magicred"},
    "entrar": {"acceder", "acceso", "entrada"},
    "acceder": {"entrar", "acceso", "entrada"},
    "acceso": {"entrar", "acceder", "entrada"},
    "entrada": {"entrar", "acceder", "acceso"},
}


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


# Patrón para detectar códigos técnicos compuestos con guiones (ej. PNM-P01,
# BNR-S01, ABC-123).  El guion forma parte del identificador y no debe
# separarse al tokenizar con \w+.  Se requiere al menos un segmento de ≥2
# caracteres a cada lado del guión.
_RE_COMPOUND_CODE = re.compile(
    r"\b[a-z0-9]{2,}(?:-[a-z0-9]{1,})+\b",
    re.IGNORECASE,
)


def _extract_keywords(text: str, *, prioritize_length: bool = True) -> list[str]:
    keywords: list[str] = []
    seen: set[str] = set()

    # ── Paso previo: extraer códigos compuestos con guiones ──────────────
    # Estos identificadores (ej. PNM-P01, BNR-S01) se perderían al
    # tokenizar con \w+ porque el guión actúa como separador y las partes
    # individuales suelen ser < 4 caracteres.
    for code in _RE_COMPOUND_CODE.findall(text):
        code_lower = code.lower()
        if code_lower not in seen and code_lower not in STOPWORDS:
            keywords.append(code_lower)
            seen.add(code_lower)
            # Marcar las partes individuales como vistas para evitar que
            # se añadan por separado (su semántica ya está capturada en
            # el código compuesto).
            for part in code_lower.split("-"):
                seen.add(part)

    for token in re.findall(r"\w+", text.lower()):
        if len(token) < 3 or token in seen or token in STOPWORDS:
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


def _keyword_core_forms(keyword: str) -> set[str]:
    """Variantes morfológicas del keyword SIN sinónimos de dominio.

    Se usa para detectar matches 'directos' (la palabra real del usuario)
    vs matches solo por sinónimo.
    """
    forms = {keyword}
    if len(keyword) >= 5:
        forms.add(keyword[:-1])
    if keyword.endswith("es") and len(keyword) >= 6:
        forms.add(keyword[:-2])
    if keyword.endswith("os") or keyword.endswith("as"):
        stem = keyword[:-2]
        if len(stem) >= 3:
            forms.add(stem)
    return {form for form in forms if len(form) >= 3}


def _keyword_forms(keyword: str) -> set[str]:
    forms = _keyword_core_forms(keyword)
    # Expandir con sinónimos de dominio para mejorar matching semántico.
    # Se busca la entrada tanto para la palabra original como para todas sus
    # formas core (singular, stem...), de modo que "claves" → "clave" →
    # sinónimos {"contrasena", "password"} funcionen correctamente.
    for lookup in {keyword, *forms}:
        synonyms = DOMAIN_SYNONYMS.get(lookup)
        if synonyms:
            forms.update(synonyms)
    # Para códigos compuestos con guión (ej. "pnm-p01"), añadir las partes
    # individuales como formas.  Estas partes permiten al source-token
    # matching localizar documentos cuyo path contiene las partes del
    # código (el índice de tokens divide por guiones).
    if "-" in keyword:
        for part in keyword.split("-"):
            if len(part) >= 2:
                forms.add(part)
    return {form for form in forms if len(form) >= 2}


def _strip_image_markers(text: str) -> str:
    """Elimina bloques completos de descripción de imagen del texto.

    Los marcadores ``[DESCRIPCIÓN DE IMAGEN ...]`` y sus párrafos descriptivos
    generados durante la ingesta contienen vocabulario (nombres de fichero,
    descripciones de UI) que infla artificialmente el keyword matching.
    Esta función elimina el marcador **y** la descripción que le sigue,
    así como cuerpos huérfanos (sin cabecera) que terminan con ``.]``,
    dejando solo el texto técnico real del chunk.
    """
    text = _RE_IMG_BLOCK_KW.sub("", text)
    text = _RE_IMG_NO_DESC_KW.sub("", text)
    text = _RE_ORPHAN_IMG_BODY_KW.sub("", text)
    return text


# Bloque completo: cabecera entre corchetes + descripción hasta cierre ``.]``.
# Antes usaba ``.*?(?=\n\n|...)`` con DOTALL, lo que consumía texto técnico
# real cuando no había doble salto de línea tras la descripción.
_RE_IMG_BLOCK_KW = re.compile(
    r"\[DESCRIPCI[ÓO]N DE IMAGEN[^\]]*\]:?\s*.*?\.\]",
    re.DOTALL | re.IGNORECASE,
)
# Marcador sin descripción.
_RE_IMG_NO_DESC_KW = re.compile(
    r"\[IMAGEN[^\]]*\]:?\s*sin descripción disponible\.",
    re.IGNORECASE,
)
# Cuerpo huérfano: párrafo (≥50 chars) que termina con ``.]``.
_RE_ORPHAN_IMG_BODY_KW = re.compile(
    r"(?:(?<=\n\n)|(?<=\A))[^\n](?:(?!\n\n).){49,}?\.\](?=\s*\n|\s*\Z)",
    re.DOTALL,
)


def _keyword_match_count(doc, keyword_forms: dict[str, set[str]]) -> int:
    if not keyword_forms:
        return 0
    text = _strip_accents(_strip_image_markers((doc.page_content or "").lower()))
    return sum(
        1
        for forms in keyword_forms.values()
        if any(form in text for form in forms)
    )


def _keyword_match_quality(doc, keyword_forms: dict[str, set[str]]) -> float:
    """Scoring ponderado: match directo (palabra real del usuario) pesa más que match por sinónimo.

    - Match por forma core (morfológica) del keyword: 1.0
    - Match solo por sinónimo de dominio: 0.5
    - Sin match: 0.0

    Esto permite diferenciar un documento que habla literalmente de
    'máquina de cambio' (match directo) de uno que solo menciona 'CCM'
    (match por sinónimo).
    """
    if not keyword_forms:
        return 0.0
    text = _strip_accents(_strip_image_markers((doc.page_content or "").lower()))
    score = 0.0
    for keyword, all_forms in keyword_forms.items():
        core = _keyword_core_forms(keyword)
        if any(form in text for form in core):
            score += 1.0  # Match directo de la palabra del usuario
        elif any(form in text for form in all_forms):
            score += 0.5  # Match solo por sinónimo
    return score


def _source_keyword_match_count(doc, keyword_forms: dict[str, set[str]]) -> int:
    if not keyword_forms:
        return 0
    source = _strip_accents(str(doc.metadata.get("source", "")).lower())
    return sum(
        1
        for forms in keyword_forms.values()
        if any(form in source for form in forms)
    )


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
