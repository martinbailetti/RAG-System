"""Utilidades compartidas para la consulta de la base vectorial y el modelo generativo."""

from __future__ import annotations

import os
import re
import unicodedata
from pathlib import Path
from typing import Iterable
import google.generativeai as genai
import numpy as np

if not hasattr(np, "float_"):
    np.float_ = np.float64  # type: ignore[attr-defined]
if not hasattr(np, "int_"):
    np.int_ = np.int64  # type: ignore[attr-defined]
if not hasattr(np, "uint"):
    np.uint = np.uint64  # type: ignore[attr-defined]
if not hasattr(np, "NaN"):
    np.NaN = np.nan  # type: ignore[attr-defined]

from langchain_chroma import Chroma
from langchain_community.embeddings import HuggingFaceEmbeddings

from env_loader import load_host_env

BASE_DIR = Path(__file__).resolve().parent.parent

load_host_env(BASE_DIR)

try:  # pragma: no cover - dependencia opcional
    import spacy  # type: ignore
except Exception:  # pragma: no cover - spaCy no instalado
    spacy = None

try:  # pragma: no cover - dependencia opcional en tiempo de ejecución
    from snowballstemmer import stemmer as _snowball_stemmer  # type: ignore
except Exception:  # pragma: no cover - si la librería no está disponible
    _snowball_stemmer = None

# Las variables de entorno ya se cargaron al importar el módulo.

_DISABLE_SPACY = os.environ.get("RAG_DISABLE_SPACY", "").strip().lower() in {"1", "true", "yes", "on"}

MODELO_CONSULTA = os.environ.get("GEMINI_MODEL_QUERY", "gemini-2.5-flash")

PROMPT_TEMPLATE = (
    "{system_prompt}\n\n"
    "Contexto:\n{context}\n\n"
    "Pregunta: {question}\n"
    "Respuesta:"
)

DEFAULT_SYSTEM_PROMPT = (
    "Eres un asistente técnico que responde basándose exclusivamente en el contexto proporcionado.\n"
    "Si la respuesta no está en el contexto, indica que no tienes la información."
)


def load_system_prompt(file_path: str | None = None) -> str:
    """Carga el system prompt desde un archivo o devuelve el valor por defecto."""
    if file_path is None:
        file_path = os.environ.get("SYSTEM_PROMPT_FILE", "system_prompt.txt")
    
    resolved = _resolve_path(file_path)
    if resolved.exists():
        try:
            return resolved.read_text(encoding="utf-8").strip()
        except Exception:
            pass
    return DEFAULT_SYSTEM_PROMPT


def _resolve_path(value: str) -> Path:
    """Convierte un valor a ruta absoluta tomando como base la raíz del proyecto."""
    path = Path(value)
    if path.is_absolute():
        return path
    return (BASE_DIR / path).resolve()


def get_db_path() -> str:
    """Obtiene la ruta al directorio de Chroma o arroja un error si falta."""
    db_path = os.environ.get("DB_PATH")
    if not db_path:
        raise RuntimeError("DB_PATH no está definido. Configura la variable de entorno o el archivo .env.")
    return str(_resolve_path(db_path))


def get_documents_folder(default: str | None = None) -> str:
    """Obtiene la carpeta de documentos original, admitiendo un valor por defecto opcional."""
    raw = os.environ.get("DOCS_PATH")
    if not raw:
        if default is None:
            raise RuntimeError("DOCS_PATH no está definido. Ajusta la variable de entorno o .env.")
        raw = default
    return str(_resolve_path(raw))


def create_embedding_model() -> HuggingFaceEmbeddings:
    """Crea el modelo de embeddings coherente con la ingesta."""
    return HuggingFaceEmbeddings(
        model_name="paraphrase-multilingual-mpnet-base-v2",
        model_kwargs={"device": "cpu"},
    )


def open_vectorstore(db_path: str, embedding_model: HuggingFaceEmbeddings) -> Chroma:
    """Inicializa el vectorstore persistido en disco."""
    return Chroma(persist_directory=db_path, embedding_function=embedding_model)


def ensure_gemini_model(model_name: str = MODELO_CONSULTA) -> genai.GenerativeModel:
    """Configura Gemini verificando la API key y devuelve el modelo solicitado."""
    api_key = os.environ.get("GEMINI_API_KEY")
    if not api_key:
        raise RuntimeError(
            "GEMINI_API_KEY no está definida. Configura la variable de entorno o el archivo .env."
        )
    try:
        genai.configure(api_key=api_key)
    except Exception as exc:  # pragma: no cover - dependencias externas
        raise RuntimeError(f"No se pudo configurar la API de Gemini: {exc}") from exc
    return genai.GenerativeModel(model_name)


def _strip_image_description_blocks(text: str) -> str:
    """Elimina bloques completos de descripción de imagen del texto.

    Los marcadores de imagen (generados en la ingesta de HTML y PDF) describen
    visualmente capturas de pantalla pero **no contienen información
    procedimental**.  Enviarlos al LLM introduce ruido significativo (hasta 80%
    del chunk puede ser descripción visual) y confunde al modelo al hacer que
    interprete detalles de UI como instrucciones.

    Se eliminan tres tipos de bloques:
    - ``[DESCRIPCIÓN DE IMAGEN ...]: texto descriptivo largo``
    - ``[IMAGEN ...]: sin descripción disponible.``
    - Cuerpos huérfanos: párrafos que terminan con ``.]`` y son continuaciones
      de descripciones cuyo marcador ``[DESCRIPCIÓN ...]`` quedó en un chunk
      anterior por el text-splitter.  El patrón ``.]`` es exclusivo de los
      textos generados por el modelo de descripción de imágenes.
    """
    text = _RE_IMG_DESC_BLOCK.sub("", text)
    text = _RE_IMG_NO_DESC.sub("", text)
    # Cuerpos huérfanos de descripción de imagen (sin marcador por chunk-split).
    text = _RE_ORPHAN_IMG_BODY.sub("", text)
    # Colapsar líneas vacías consecutivas resultantes
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


# Bloque completo: cabecera entre corchetes + descripción hasta el cierre ``.]``.
_RE_IMG_DESC_BLOCK = re.compile(
    r"\[DESCRIPCI[ÓO]N DE IMAGEN[^\]]*\]:?\s*.*?\.\]",
    re.DOTALL | re.IGNORECASE,
)
# Marcador sin descripción disponible.
_RE_IMG_NO_DESC = re.compile(
    r"\[IMAGEN[^\]]*\]:?\s*sin descripción disponible\.",
    re.IGNORECASE,
)
# Cuerpo huérfano de descripción de imagen: párrafo (≥50 chars) que termina con
# ``.]`` — patrón exclusivo de los textos generados por IA al describir capturas
# de pantalla.  Aparece cuando el text-splitter parte un bloque y el chunk
# siguiente recibe solo el cuerpo sin la cabecera ``[DESCRIPCIÓN ...]``.
_RE_ORPHAN_IMG_BODY = re.compile(
    r"(?:(?<=\n\n)|(?<=\A))[^\n](?:(?!\n\n).){49,}?\.\](?=\s*\n|\s*\Z)",
    re.DOTALL,
)


def build_context(docs: Iterable) -> str:
    """Une los fragmentos recuperados en un bloque de contexto.

    Antes de unirlos, elimina los bloques de descripción de imagen generados
    durante la ingesta.  Estos bloques describen visualmente capturas de pantalla
    pero no aportan información técnica procedimental y su presencia (a menudo
    40-80% del chunk) confunde al LLM.
    """
    fragments = []
    for doc in docs:
        raw = getattr(doc, "page_content", "")
        clean = _strip_image_description_blocks(raw)
        if clean:
            fragments.append(clean)
    return "\n\n".join(fragments)


if _snowball_stemmer is not None:
    _STEMMER = _snowball_stemmer("spanish")
else:  # pragma: no cover - fallback sin stemming
    _STEMMER = None

_SPACY_NLP = None
if spacy is not None and not _DISABLE_SPACY:  # pragma: no cover - carga perezosa de spaCy
    _SPACY_MODEL_NAME = os.environ.get("RAG_SPACY_MODEL", "es_core_news_sm")
    try:
        _SPACY_NLP = spacy.load(_SPACY_MODEL_NAME, exclude=["parser", "ner", "textcat"])
        if "lemmatizer" not in _SPACY_NLP.pipe_names:  # garantía de lematización
            import logging as _logging
            _logging.getLogger("rag").warning(
                "spaCy: modelo '%s' cargado pero sin componente 'lemmatizer' (pipes: %s). "
                "spaCy desactivado.",
                _SPACY_MODEL_NAME,
                _SPACY_NLP.pipe_names,
            )
            _SPACY_NLP = None
    except Exception as _spacy_exc:
        import logging as _logging
        _logging.getLogger("rag").warning(
            "spaCy: no se pudo cargar el modelo '%s': %s. "
            "Instala el modelo con: pip install https://github.com/explosion/spacy-models/releases/download/%s-3.7.0/%s-3.7.0-py3-none-any.whl",
            _SPACY_MODEL_NAME,
            _spacy_exc,
            _SPACY_MODEL_NAME,
            _SPACY_MODEL_NAME,
        )
        _SPACY_NLP = None

SPACY_AVAILABLE = _SPACY_NLP is not None
SPACY_DISABLED = _DISABLE_SPACY


def _strip_accents(text: str) -> str:
    normalized = unicodedata.normalize("NFKD", text)
    return "".join(ch for ch in normalized if not unicodedata.combining(ch))


def _spanish_infinitive_candidates(token: str) -> list[str]:
    """Genera candidatos de infinitivo sencillos para formas verbales comunes."""

    def _add_forms(base: str, suffixes: tuple[str, ...], bucket: set[str]) -> None:
        if len(base) < 2:
            return
        for suffix in suffixes:
            candidate = f"{base}{suffix}"
            if len(candidate) >= 4:
                bucket.add(candidate)

    lower = token.lower()
    infinitives: set[str] = set()

    if len(lower) < 5:
        return []

    if lower.endswith(("ar", "er", "ir")):
        return []

    # Formas personales y gerundios frecuentes en español.
    if lower.endswith("amos"):
        _add_forms(lower[:-4], ("ar",), infinitives)
    elif lower.endswith("emos"):
        _add_forms(lower[:-4], ("er",), infinitives)
    elif lower.endswith("imos"):
        _add_forms(lower[:-4], ("ir",), infinitives)
    elif lower.endswith("ando"):
        _add_forms(lower[:-4], ("ar",), infinitives)
    elif lower.endswith("iendo") or lower.endswith("yendo"):
        _add_forms(lower[:-5], ("er", "ir"), infinitives)
    elif lower.endswith("an"):
        _add_forms(lower[:-2], ("ar", "er", "ir"), infinitives)
    elif lower.endswith("en"):
        _add_forms(lower[:-2], ("er", "ir"), infinitives)
    elif lower.endswith("o"):
        _add_forms(lower[:-1], ("ar", "er", "ir"), infinitives)
    elif lower.endswith("as"):
        _add_forms(lower[:-2], ("ar",), infinitives)
    elif lower.endswith("es"):
        _add_forms(lower[:-2], ("er", "ir"), infinitives)
    elif lower.endswith("a"):
        _add_forms(lower[:-1], ("ar",), infinitives)
    elif lower.endswith("e"):
        _add_forms(lower[:-1], ("er", "ir"), infinitives)

    return [form for form in infinitives if form and form != lower]


def _spanish_nominalization_candidates(token: str) -> list[str]:
    """Deriva formas nominales frecuentes a partir de infinitivos (acion, amiento...)."""

    lower = token.lower()
    variants: set[str] = set()
    if len(lower) < 5:
        return []

    endings = (
        ("ar", ("acion", "aciones", "amiento", "amientos", "ador", "adora", "adores", "adoras")),
        ("er", ("eccion", "ecciones", "imiento", "imientos", "edor", "edora", "edores", "edoras")),
        ("ir", ("icion", "iciones", "imiento", "imientos", "idor", "idora", "idores", "idoras")),
    )

    for ending, suffixes in endings:
        if lower.endswith(ending) and len(lower) > len(ending) + 1:
            base = lower[: -len(ending)]
            for suffix in suffixes:
                candidate = f"{base}{suffix}"
                if len(candidate) >= 5:
                    variants.add(candidate)

    return sorted(variant for variant in variants if variant and variant != lower)


def normalize_and_expand_query(text: str) -> str:
    """Normaliza la consulta a minúsculas, elimina acentos y añade variantes con stemming."""
    if not text:
        return ""

    lowered = text.lower().strip()
    base = _strip_accents(lowered)

    tokens = re.findall(r"\b\w+\b", base, flags=re.UNICODE)
    stems: list[str] = []
    if _STEMMER is not None and tokens:
        try:
            stems = [
                stem
                for stem in _STEMMER.stemWords(tokens)
                if stem and len(stem) > 2
            ]
        except Exception:
            stems = []

    unique_stems: list[str] = []
    seen = set()
    for stem in stems:
        if stem not in seen:
            seen.add(stem)
            unique_stems.append(stem)

    lemmas: list[str] = []
    lemma_fallback_tokens: set[str] = set()
    if _SPACY_NLP is not None and lowered:
        try:
            doc = _SPACY_NLP(lowered)
            ordered: list[str] = []
            for token in doc:
                lemma = token.lemma_
                if not lemma or lemma == "-PRON-":
                    continue
                lemma_lower = lemma.lower()
                lemma_clean = _strip_accents(lemma_lower)
                if len(lemma_clean) < 3:
                    continue
                token_clean = _strip_accents(token.text.lower())
                added = False
                if lemma_clean not in ordered:
                    ordered.append(lemma_clean)
                    added = True
                if (not added or lemma_clean == token_clean) and len(token_clean) >= 4:
                    lemma_fallback_tokens.add(token_clean)
            lemmas = ordered
        except Exception:
            lemmas = []
            lemma_fallback_tokens.clear()

    infinitive_variants: set[str] = set()
    candidates_source = tokens if not lemmas else sorted(lemma_fallback_tokens)
    if candidates_source:
        for token in candidates_source:
            for variant in _spanish_infinitive_candidates(token):
                if variant not in tokens:
                    infinitive_variants.add(variant)

    nominal_variants: set[str] = set()
    for token in candidates_source:
        for variant in _spanish_nominalization_candidates(token):
            if variant not in tokens:
                nominal_variants.add(variant)

    segments = [lowered]
    if base != lowered:
        segments.append(base)
    if unique_stems:
        segments.append(" ".join(unique_stems))
    if lemmas:
        segments.append(" ".join(lemmas))
    if infinitive_variants:
        segments.append(" ".join(sorted(infinitive_variants)))
    if nominal_variants:
        segments.append(" ".join(sorted(nominal_variants)))

    augmented = " ".join(segment for segment in segments if segment)
    return augmented.strip()
