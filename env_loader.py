"""Host-aware .env loader used across CLI scripts and services."""
from __future__ import annotations

import os
from pathlib import Path
from typing import Optional

from dotenv import load_dotenv

# Cache repo root so scripts can override base directory if needed.
REPO_ROOT = Path(__file__).resolve().parent


def _candidate_hostnames(raw_name: str) -> list[str]:
    """Return distinct hostname variants to try when resolving per-host .env files."""
    lowered = raw_name.lower()
    uppered = raw_name.upper()
    return list(dict.fromkeys([raw_name, lowered, uppered]))


def load_host_env(base_path: Optional[Path] = None) -> Optional[Path]:
    """Load .env plus .env.<COMPUTERNAME> (if present) with override semantics."""
    base_dir = Path(base_path) if base_path else REPO_ROOT
    default_env = base_dir / ".env"

    # First pass: load the shared .env if it exists.
    load_dotenv(default_env)

    hostname = (os.environ.get("COMPUTERNAME") or os.environ.get("HOSTNAME") or "").strip()
    if not hostname:
        return default_env if default_env.exists() else None

    for candidate in _candidate_hostnames(hostname):
        host_env = base_dir / f".env.{candidate}"
        if host_env.exists():
            load_dotenv(host_env, override=True)
            return host_env

    return default_env if default_env.exists() else None
