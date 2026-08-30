"""Extracción de keywords, stopwords y sinónimos de dominio para el pipeline RAG."""

import json
import os
import re
import unicodedata
from pathlib import Path
from typing import Optional, List, Dict, Any

from app_logging import logger

_KEYWORD_CONFIG_DIR = Path(__file__).resolve().parent / "keyword_config"
_KEYWORD_CONFIG_LOADED = False


def _strip_accents(text: str) -> str:
    """Elimina acentos/diacríticos de un texto (NFD + quitar combining chars)."""
    normalized = unicodedata.normalize("NFKD", text)
    return "".join(ch for ch in normalized if not unicodedata.combining(ch))


MAX_EXTRACT_KEYWORDS = 7

STOPWORDS: set[str] = set()
PRIORITY_KEYWORDS: set[str] = set()
DOMAIN_SYNONYMS: dict[str, set[str]] = {}


def _resolve_keyword_config_dir(config_dir: Path | str | None = None) -> Path:
    if config_dir is not None:
        path = Path(config_dir)
        return path if path.is_absolute() else (_KEYWORD_CONFIG_DIR.parent / path).resolve()
    env_path = os.environ.get("RAG_KEYWORD_CONFIG_DIR")
    if env_path:
        path = Path(env_path)
        return path if path.is_absolute() else (_KEYWORD_CONFIG_DIR.parent / path).resolve()
    return _KEYWORD_CONFIG_DIR


def _load_json_list(path: Path) -> list[str]:
    with path.open(encoding="utf-8") as f:
        data = json.load(f)
    if not isinstance(data, list) or not all(isinstance(item, str) for item in data):
        raise ValueError(f"{path.name} debe ser un array JSON de strings")
    return data


def _load_json_domain_synonyms(path: Path) -> dict[str, set[str]]:
    with path.open(encoding="utf-8") as f:
        data = json.load(f)
    if not isinstance(data, dict):
        raise ValueError(f"{path.name} debe ser un objeto JSON")
    result: dict[str, set[str]] = {}
    for key, values in data.items():
        if not isinstance(key, str) or not isinstance(values, list) or not all(
            isinstance(item, str) for item in values
        ):
            raise ValueError(
                f"{path.name} debe mapear strings a arrays de strings"
            )
        result[key] = set(values)
    return result


def load_keyword_config(config_dir: Path | str | None = None) -> None:
    """Carga stopwords, priority keywords y sinónimos de dominio desde JSON."""
    global STOPWORDS, PRIORITY_KEYWORDS, DOMAIN_SYNONYMS, _KEYWORD_CONFIG_LOADED

    if _KEYWORD_CONFIG_LOADED and config_dir is None:
        return

    base = _resolve_keyword_config_dir(config_dir)
    stopwords_path = base / "stopwords.json"
    priority_path = base / "priority_keywords.json"
    synonyms_path = base / "domain_synonyms.json"

    for path in (stopwords_path, priority_path, synonyms_path):
        if not path.is_file():
            raise FileNotFoundError(f"No se encontró el archivo de keywords: {path}")

    STOPWORDS = set(_load_json_list(stopwords_path))
    PRIORITY_KEYWORDS = set(_load_json_list(priority_path))
    DOMAIN_SYNONYMS = _load_json_domain_synonyms(synonyms_path)
    _KEYWORD_CONFIG_LOADED = True

    logger.info(
        "Keyword config cargada desde %s (%d stopwords, %d priority, %d domain synonyms)",
        base,
        len(STOPWORDS),
        len(PRIORITY_KEYWORDS),
        len(DOMAIN_SYNONYMS),
    )


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


load_keyword_config()
