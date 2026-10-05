"""
Configuration for the DeployX backend.

Everything is read from the environment (a local ``backend/.env`` file is
loaded automatically for development). Placeholders only — real secrets
never belong in ``.env.example``.
"""

from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = BASE_DIR.parent

# Single source of truth: the project-root .env.
#
# The path is derived from this file's location, so it resolves to the same
# absolute file no matter which directory the process was started from.
# A backend-local .env is still honoured as a fallback for anyone who keeps
# one, but we never create it.
_LOAD_CANDIDATES = (
    PROJECT_ROOT / ".env",
    BASE_DIR / ".env",
)

_ENV_LOADED_FROM = None
for _candidate in _LOAD_CANDIDATES:
    if _candidate.is_file():
        # override=True: the .env file is the source of truth for backend
        # secrets. A stale value inherited from an earlier shell session must
        # never shadow the file, otherwise authentication silently fails (or
        # provider tokens are decrypted with the wrong key).
        load_dotenv(_candidate, override=True)
        _ENV_LOADED_FROM = _candidate
        break

DATA_DIR = BASE_DIR / "data"
DATA_DIR.mkdir(parents=True, exist_ok=True)

DEFAULT_DATABASE_URL = f"sqlite:///{(DATA_DIR / 'deployx_backend.db').as_posix()}"

DATABASE_URL = (os.getenv("DEPLOYX_DATABASE_URL") or DEFAULT_DATABASE_URL).strip()

# Address the CLI should use to reach this backend.
BACKEND_URL = (
    os.getenv("DEPLOYX_BACKEND_URL") or "http://127.0.0.1:8000"
).strip().rstrip("/")

# Fernet key used to encrypt provider access tokens at rest.
ENCRYPTION_KEY = (os.getenv("DEPLOYX_ENCRYPTION_KEY") or "").strip()

# Shared secret required from n8n on every /v1/n8n/* request.
N8N_SERVICE_KEY = (os.getenv("DEPLOYX_N8N_SERVICE_KEY") or "").strip()

# Ticket / session lifetimes (short lived on purpose).
TICKET_TTL_SECONDS = int(os.getenv("DEPLOYX_TICKET_TTL_SECONDS", "600"))
SESSION_TTL_SECONDS = int(os.getenv("DEPLOYX_SESSION_TTL_SECONDS", "86400"))

# Outbound provider HTTP timeout (seconds).
HTTP_TIMEOUT = int(os.getenv("DEPLOYX_HTTP_TIMEOUT", "20"))

# Backend bind settings.
BIND_HOST = os.getenv("DEPLOYX_BACKEND_HOST", "127.0.0.1")
BIND_PORT = int(os.getenv("DEPLOYX_BACKEND_PORT", "8000"))


class ConfigError(RuntimeError):
    """Raised when required configuration is missing."""


def require_encryption_key() -> str:
    if not ENCRYPTION_KEY:
        raise ConfigError(
            "DEPLOYX_ENCRYPTION_KEY is not configured. "
            "Generate one with: python -m backend.crypto"
        )
    return ENCRYPTION_KEY


def require_service_key() -> str:
    if not N8N_SERVICE_KEY:
        raise ConfigError(
            "DEPLOYX_N8N_SERVICE_KEY is not configured. "
            "Set it in backend/.env before allowing n8n access."
        )
    return N8N_SERVICE_KEY


def is_configured() -> bool:
    """True when the backend can encrypt and authenticate n8n."""
    return bool(ENCRYPTION_KEY and N8N_SERVICE_KEY)


def describe() -> dict:
    """
    Non-sensitive description of the loaded configuration.

    Never returns key material - only where config came from and whether
    each required value is present.
    """
    return {
        "env_file": str(_ENV_LOADED_FROM) if _ENV_LOADED_FROM else None,
        "backend_url": BACKEND_URL,
        "database_url_configured": bool(DATABASE_URL),
        "encryption_key_present": bool(ENCRYPTION_KEY),
        "service_key_present": bool(N8N_SERVICE_KEY),
        "ticket_ttl_seconds": TICKET_TTL_SECONDS,
        "configured": is_configured(),
    }
