"""Lock file inter-proceso para evitar ingestas concurrentes.

Uso:
    from ingesta_lock import acquire_lock, release_lock, is_locked

    if not acquire_lock():
        sys.exit(1)          # otro proceso tiene el lock
    try:
        ...ingesta...
    finally:
        release_lock()

El lock file (.ingesta.lock) se crea de forma atómica (O_CREAT|O_EXCL) para
que dos procesos que arrancan simultáneamente no puedan adquirirlo ambos.
Si el proceso dueño muere abruptamente y deja el archivo, se detecta por PID
y se borra automáticamente (lock "stale").
"""
import os
import sys

from app_logging import logger  # noqa: E402 – app_logging vive en la raíz del repo

LOCK_FILE = os.path.join(os.path.dirname(os.path.dirname(__file__)), ".ingesta.lock")


def _read_lock_pid() -> int | None:
    """Lee el PID almacenado en el lock file. Devuelve None si no existe o está corrupto."""
    try:
        with open(LOCK_FILE, "r", encoding="utf-8") as fh:
            return int(fh.read().strip())
    except (OSError, ValueError):
        return None


def _pid_alive(pid: int) -> bool:
    """Devuelve True si el proceso con ese PID existe y sigue en ejecución.

    En Windows NO se usa os.kill(pid, 0) porque la implementación de CPython
    para Windows no define señal 0 como "no-op de comprobación" (como sí hace
    POSIX): puede enviar señales reales o comportarse de forma inesperada.
    En su lugar se llama directamente a la Win32 API via ctypes:
      - OpenProcess                 → obtiene handle con permisos mínimos
      - GetExitCodeProcess          → 259 (STILL_ACTIVE) = proceso vivo
    En Unix se mantiene el mecanismo estándar POSIX (kill -0).
    """
    if sys.platform == "win32":
        import ctypes
        PROCESS_QUERY_INFORMATION = 0x0400
        STILL_ACTIVE = 259
        handle = ctypes.windll.kernel32.OpenProcess(PROCESS_QUERY_INFORMATION, False, pid)
        if not handle:
            return False
        exit_code = ctypes.c_ulong()
        ctypes.windll.kernel32.GetExitCodeProcess(handle, ctypes.byref(exit_code))
        ctypes.windll.kernel32.CloseHandle(handle)
        return exit_code.value == STILL_ACTIVE
    else:
        try:
            os.kill(pid, 0)
            return True
        except PermissionError:
            return True
        except OSError:
            return False


def is_locked() -> bool:
    """Devuelve True si hay una ingesta activa (lock file presente y PID vivo)."""
    if not os.path.exists(LOCK_FILE):
        return False
    pid = _read_lock_pid()
    if pid is None:
        return False
    return _pid_alive(pid)


def acquire_lock() -> bool:
    """Intenta adquirir el lock file.

    Devuelve True si se adquirió. Devuelve False si otro proceso lo posee.
    Si el lock file existe pero el proceso propietario ha muerto (stale),
    lo elimina y lo readquiere.
    """
    pid = os.getpid()
    try:
        # O_CREAT | O_EXCL es atómico: falla si el archivo ya existe.
        fd = os.open(LOCK_FILE, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        os.write(fd, str(pid).encode())
        os.close(fd)
        logger.info("[lock] Lock de ingesta adquirido (PID %d).", pid)
        return True
    except FileExistsError:
        pass

    # El archivo existe — comprobar si el dueño sigue vivo.
    owner_pid = _read_lock_pid()
    if owner_pid is not None and _pid_alive(owner_pid):
        logger.warning(
            "[lock] Ingesta ya en curso en el proceso PID %d. Abortando.", owner_pid
        )
        print(
            f"\n❌ Ya hay una ingesta en curso (PID {owner_pid}). "
            "Espera a que termine antes de lanzar otra.\n",
            file=sys.stderr,
        )
        return False

    # Lock stale: el dueño anterior ha muerto — limpiar y readquirir.
    logger.warning(
        "[lock] Lock stale detectado (PID %s ya no existe). Eliminando y readquiriendo.",
        owner_pid,
    )
    try:
        os.remove(LOCK_FILE)
    except OSError:
        pass
    return acquire_lock()  # recursión single-level; safe


def release_lock() -> None:
    """Libera el lock file si somos sus propietarios."""
    owner_pid = _read_lock_pid()
    if owner_pid != os.getpid():
        # No somos los dueños (o ya fue eliminado); no hacer nada.
        return
    try:
        os.remove(LOCK_FILE)
        logger.info("[lock] Lock de ingesta liberado (PID %d).", os.getpid())
    except OSError:
        pass
