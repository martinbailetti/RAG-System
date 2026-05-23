"""Funciones relacionadas con la descripción de imágenes para enriquecer los fragmentos."""

from __future__ import annotations

import hashlib
import os
import json
import re
import time
import traceback
from typing import Optional

import requests

from .config import GEMINI_MODEL_IMAGE, IA_SERVICE, OLLAMA_MODEL, OLLAMA_URL

# ---------------------------------------------------------------------------
# Caché de descripciones por hash de imagen (evita re-describir logos repetidos)
# ---------------------------------------------------------------------------
_image_desc_cache: dict[str, str] = {}
_cache_hits = 0
_cache_misses = 0


def _b64_hash(image_b64: str) -> str:
    """SHA1 del contenido base64 de la imagen."""
    return hashlib.sha1(image_b64.encode("utf-8")).hexdigest()


def get_image_cache_stats() -> dict[str, int]:
    """Retorna estadísticas de la caché de descripciones de imágenes."""
    return {"hits": _cache_hits, "misses": _cache_misses, "cached": len(_image_desc_cache)}

try:
    import google.generativeai as genai
except Exception:  # pragma: no cover - solo se usa si la librería está disponible
    genai = None  # type: ignore


def init_gemini_client() -> Optional[object]:
    """Inicializa el cliente de Gemini usando la API key disponible."""
    try:
        api_key = os.environ.get("GEMINI_API_KEY")
    except Exception:  # pragma: no cover - entorno restringido
        api_key = None

    if not api_key:
        print("[Gemini] GEMINI_API_KEY no está definida; se omitirán descripciones de imágenes.")
        return None
    if genai is None:
        print(
            "[Gemini] Paquete google.generativeai no disponible; instala la librería para habilitar descripciones."
        )
        return None
    try:
        genai.configure(api_key=api_key)
        print("[Gemini] Cliente inicializado correctamente.")
        return genai.GenerativeModel(GEMINI_MODEL_IMAGE)
    except Exception as exc:  # pragma: no cover - errores de API
        print(f"[Gemini] No se pudo inicializar el cliente: {exc}")
        return None


def describe_image_with_gemini(image_b64: str, gemini_client: Optional[object], mime_type: str = "image/png") -> str:
    """Solicita a Gemini una descripción breve de la imagen. Retorna cadena vacía si falla."""
    global _cache_hits, _cache_misses
    if not gemini_client:
        return ""

    # Caché por hash: si ya se describió esta imagen, reutilizar
    img_hash = _b64_hash(image_b64)
    if img_hash in _image_desc_cache:
        _cache_hits += 1
        cached = _image_desc_cache[img_hash]
        if cached:
            print(f"[Gemini] Caché hit (imagen ya descrita): {cached[:60]}...")
        return cached

    _cache_misses += 1
    try:
        prompt = "Describe brevemente la imagen en español para enriquecer el contexto de un manual técnico."
        parts = [
            {"text": prompt},
            {"inline_data": {"mime_type": mime_type, "data": image_b64}},
        ]
        response = gemini_client.generate_content(parts)  # type: ignore[attr-defined]
        text = (getattr(response, "text", "") or "").strip()
        if text:
            _image_desc_cache[img_hash] = text
            return text
        candidate = None
        try:
            candidate = response.candidates[0].content.parts[0].text  # type: ignore[attr-defined]
        except Exception:
            candidate = None
        if candidate:
            _image_desc_cache[img_hash] = candidate.strip()
            return candidate.strip()
        feedback = getattr(response, "prompt_feedback", None)
        print(f"[Gemini] Respuesta vacía. Feedback: {feedback}")
        _image_desc_cache[img_hash] = ""
        return ""
    except Exception as exc:
        print(f"[Gemini] Error al describir imagen ({mime_type}): {exc}")
        return ""


def describe_image_with_ollama(image_b64: str, mime_type: str = "image/png") -> str:
    """Solicita a Ollama una descripción de la imagen y devuelve el texto obtenido."""
    global _cache_hits, _cache_misses

    # Caché por hash: si ya se describió esta imagen, reutilizar
    img_hash = _b64_hash(image_b64)
    if img_hash in _image_desc_cache:
        _cache_hits += 1
        cached = _image_desc_cache[img_hash]
        if cached:
            print(f"[Ollama] Caché hit (imagen ya descrita): {cached[:60]}...")
        return cached

    _cache_misses += 1
    try:
        data_uri = f"data:{mime_type};base64,{image_b64}"
        prompt = (
            "Describe brevemente la siguiente imagen en español para enriquecer el contexto de un manual técnico:\n"
            f"{data_uri}"
        )

        urls = [f"{OLLAMA_URL.rstrip('/')}/api/chat", f"{OLLAMA_URL.rstrip('/')}/api/generate"]
        payloads = [
            {"model": OLLAMA_MODEL, "messages": [{"role": "user", "content": prompt}], "max_tokens": 200},
            {"model": OLLAMA_MODEL, "prompt": prompt, "max_tokens": 200},
        ]

        session = getattr(describe_image_with_ollama, "_session", None)
        if session is None:
            session = requests.Session()
            describe_image_with_ollama._session = session

        max_attempts = 2
        timeout = 120
        for attempt in range(1, max_attempts + 1):
            for url, payload in zip(urls, payloads):
                backoff = 2 ** (attempt - 1)
                try:
                    print(
                        f"[Ollama] Attempt {attempt} POST {url} (timeout={timeout}) payload keys: {list(payload.keys())}"
                    )
                    start = time.time()
                    response = session.post(url, json=payload, timeout=timeout, stream=True)
                    elapsed = time.time() - start
                    print(f"[Ollama] HTTP {response.status_code} (elapsed {elapsed:.1f}s) headers: {dict(response.headers)}")

                    content_type = response.headers.get("Content-Type", "")
                    if response.status_code == 200 and (
                        "ndjson" in content_type or response.headers.get("Transfer-Encoding") == "chunked"
                    ):
                        text_acc = []
                        try:
                            for raw in response.iter_lines(decode_unicode=False):
                                if not raw:
                                    continue
                                if isinstance(raw, bytes):
                                    try:
                                        line = raw.decode("utf-8").strip()
                                    except Exception:
                                        line = raw.decode("utf-8", errors="replace").strip()
                                else:
                                    line = str(raw).strip()
                                if not line:
                                    continue
                                if line.startswith("data:"):
                                    line = line[len("data:") :].strip()
                                if not (line.startswith("{") or line.startswith("[")):
                                    idx = line.find("{")
                                    if idx != -1:
                                        line = line[idx:]
                                    else:
                                        continue
                                try:
                                    payload_json = json.loads(line)
                                except Exception:
                                    continue

                                content = None
                                if isinstance(payload_json, dict):
                                    message = payload_json.get("message")
                                    if isinstance(message, dict):
                                        content = message.get("content")
                                        if isinstance(content, dict):
                                            content = content.get("text")
                                    else:
                                        choices = payload_json.get("choices")
                                        if isinstance(choices, list) and choices:
                                            choice = choices[0]
                                            if isinstance(choice, dict):
                                                content = choice.get("text") or choice.get("response")
                                if content:
                                    text_acc.append(content)
                                if payload_json.get("done") is True:
                                    break
                        except Exception as exc:  # pragma: no cover - manejo defensivo del stream
                            print(f"[Ollama] Error leyendo stream: {exc}")
                        result = "".join(text_acc).strip()
                        if result:
                            _image_desc_cache[img_hash] = result
                            return result
                        continue

                    try:
                        json_body = response.json()
                    except Exception as exc_json:
                        text_body = response.text[:2000]
                        print(f"[Ollama] No se pudo parsear JSON: {exc_json}. Body (trunc): {text_body}")
                        continue

                    if response.status_code != 200:
                        print(f"[Ollama] Non-200 response body (trunc): {str(json_body)[:2000]}")
                        continue

                    text = ""
                    if isinstance(json_body, dict):
                        text = json_body.get("response") or json_body.get("text") or json_body.get("result") or ""
                        if not text:
                            for value in json_body.values():
                                if isinstance(value, str) and value.strip():
                                    text = value
                                    break
                    elif isinstance(json_body, list) and json_body:
                        first = json_body[0]
                        if isinstance(first, dict):
                            text = first.get("text") or first.get("response") or ""
                    if text:
                        _image_desc_cache[img_hash] = text.strip()
                        return text.strip()

                except requests.exceptions.RequestException as exc_request:
                    print(f"[Ollama] RequestException on attempt {attempt} to {url}: {exc_request}")
                    continue
                except Exception as exc:  # pragma: no cover - robustez ante errores inesperados
                    print(f"[Ollama] Error inesperado: {exc}\n{traceback.format_exc()}")
                    continue

            print(f"[Ollama] Reintentando todo el conjunto en {backoff}s...")
            time.sleep(backoff)

        _image_desc_cache[img_hash] = ""
        return ""
    except Exception as exc:
        print(f"[Ollama] Error al describir imagen: {exc}")
        return ""


# Preámbulos que la IA añade antes de la descripción real.
# Se eliminan del inicio del texto (con posibles signos de puntuación y saltos).
_PREAMBLE_PATTERNS = re.compile(
    r"^[\s\"']*"
    r"(?:"
    r"claro[,\.]?\s*"
    r"|por\s+supuesto[,\.]?\s*"
    r"|aqu[ií]\s+tienes?\s+[^:]{0,60}:\s*"
    r"|a\s+continuaci[oó]n\s+[^:]{0,60}:\s*"
    r"|esta\s+es\s+[^:]{0,60}:\s*"
    r"|descripci[oó]n\s+(?:breve\s+)?(?:de\s+la\s+imagen\s*)?[:\-]\s*"
    r")*"
    r"[\s\"']*",
    re.IGNORECASE,
)


def _strip_preamble(text: str) -> str:
    """Elimina frases introductorias genéricas que la IA antepone a la descripción real."""
    return _PREAMBLE_PATTERNS.sub("", text, count=1).strip().lstrip(":").strip()


def describe_image(image_b64: str, mime_type: str, gemini_client: Optional[object]) -> str:
    """Selecciona el servicio configurado para describir la imagen."""
    if IA_SERVICE == "ollama":
        text = describe_image_with_ollama(image_b64, mime_type=mime_type)
    else:
        text = describe_image_with_gemini(image_b64, gemini_client, mime_type=mime_type)
    if text:
        text = _strip_preamble(text)
    if text and _is_uninformative_description(text):
        print(f"      -> Descripción descartada (no aporta información técnica útil): {text[:80]}...")
        return ""
    return text


# Patrones que indican que la IA no pudo interpretar la imagen y produjo
# una descripción genérica sin valor técnico.  Se evalúan sobre el texto
# en minúsculas y sin acentos para cubrir variantes ortográficas.
_UNINFORMATIVE_PATTERNS = [
    r"requiere?\s+m[aá]s\s+contexto",
    r"interpretaci[oó]n\s+precisa",
    r"no\s+(es\s+)?posible\s+determinar",
    r"no\s+se\s+puede\s+(determinar|identificar|interpretar)",
    r"no\s+puedo\s+(describir|determinar|identificar)",
    r"sin\s+contexto\s+adicional",
    r"no\s+tengo\s+suficiente",
    r"no\s+es\s+posible\s+proporcionar",
    r"imagen\s+decorativa",
    r"no\s+aporta\s+informaci[oó]n",
    r"dif[ií]cil\s+de\s+determinar",
    r"no\s+contiene\s+informaci[oó]n\s+t[eé]cnica",
    r"formas?\s+(irregulares?|abstractas?|simples?)",
    r"simplicidad\s+de\s+las?\s+formas",
    # Logos, logotipos, marcas — no aportan contenido técnico indexable.
    r"\blogo(tipo)?\b",
    r"\bemblem[ae]\b",
    r"\bmarca\s+(corporativa|comercial|registrada)",
    r"\biso(tipo|logotipo)?\b",
    r"imagen\s+(?:muestra\s+)?(?:un|el|una)\s+logo",
    r"(?:letras?|texto)\s+.{0,40}\s+(?:sobre\s+fondo|en\s+blanco|en\s+negro)",
    # Imágenes vacías, en negro, en blanco o sin contenido visible.
    r"(?:completamente|totalmente|enteramente)\s+(negro|blanca?|oscuro|vac[ií]o)",
    r"sin\s+detalles?\s+(ni\s+objetos?\s+visibles?|visibles?)",
    r"fondo\s+(completamente\s+)?(negro|blanco|oscuro|vac[ií]o)",
    r"recuadro\s+(completamente\s+)?(negro|blanco|vac[ií]o)",
    r"no\s+(hay|se\s+ven?)\s+(detalles?|objetos?|contenido|elementos?)\s+visibles?",
    r"imagen\s+(en\s+)?(blanco|negro)\s+(?:y\s+negro\s+)?sin\s+",
]
_RE_UNINFORMATIVE = re.compile(
    "|".join(_UNINFORMATIVE_PATTERNS),
    re.IGNORECASE,
)


def _is_uninformative_description(text: str) -> bool:
    """Devuelve True si la descripción generada por la IA es genérica/inútil.

    Se descarta si:
    - Contiene frases típicas de "no puedo interpretar esta imagen".
    - Es demasiado corta para contener información técnica real (< 40 chars).
    """
    if len(text.strip()) < 40:
        return True
    return bool(_RE_UNINFORMATIVE.search(text))
