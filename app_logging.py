"""Módulo centralizado de logging para smirag.

Uso desde cualquier módulo:
    from app_logging import logger

    logger.info("Mensaje informativo")
    logger.warning("Algo inesperado")
    logger.error("Error grave")
    logger.debug("Solo visible si SMIRAG_LOG_LEVEL=DEBUG")

El log se escribe a:
  - Consola (stdout) — siempre
  - Archivo  smirag.log  — siempre (rotación automática a 5 MB, máx. 3 backups)

Variables de entorno:
  SMIRAG_LOG_LEVEL   = DEBUG | INFO (default) | WARNING | ERROR
  SMIRAG_LOG_FILE    = ruta del archivo (default: smirag.log en raíz del proyecto)
"""

import logging
import os
from logging.handlers import RotatingFileHandler

_LOG_LEVEL = os.environ.get("SMIRAG_LOG_LEVEL", "INFO").upper()
_LOG_FILE = os.environ.get(
    "SMIRAG_LOG_FILE",
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "smirag.log"),
)
_MAX_BYTES = 5 * 1024 * 1024  # 5 MB por archivo
_BACKUP_COUNT = 3             # smirag.log.1, .2, .3

_FMT = "%(asctime)s [%(levelname)s] %(name)s: %(message)s"
_DATE_FMT = "%Y-%m-%d %H:%M:%S"

logger = logging.getLogger("smirag")
logger.setLevel(getattr(logging, _LOG_LEVEL, logging.INFO))

# Evitar duplicar handlers si el módulo se reimporta (reload)
if not logger.handlers:
    # Handler de consola
    _console = logging.StreamHandler()
    _console.setFormatter(logging.Formatter(_FMT, datefmt=_DATE_FMT))
    logger.addHandler(_console)

    # Handler de archivo con rotación
    try:
        _file_handler = RotatingFileHandler(
            _LOG_FILE,
            maxBytes=_MAX_BYTES,
            backupCount=_BACKUP_COUNT,
            encoding="utf-8",
        )
        _file_handler.setFormatter(logging.Formatter(_FMT, datefmt=_DATE_FMT))
        logger.addHandler(_file_handler)
    except Exception as exc:
        logger.warning("No se pudo crear el archivo de log '%s': %s", _LOG_FILE, exc)

    # No propagar al root logger para evitar duplicados
    logger.propagate = False
