"""
Secure local credential storage for DeployX.

Access tokens (GitHub, Vercel) are stored behind a small abstraction so
the storage backend can be upgraded without touching the rest of the
code base.

Backends, best available first:
    1. keyring  -> OS credential store
                   (Windows Credential Manager / macOS Keychain / Secret Service)
    2. dpapi    -> Windows DPAPI per-user encrypted file (no dependency)
    3. file     -> plaintext JSON fallback (last resort)

The legacy plaintext file ``~/.deployx/credentials.json`` is migrated
into the best available secure backend on first read and then removed,
so tokens are no longer left in cleartext.

No token value is ever printed by this module.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

AUTH_DIR = Path.home() / ".deployx"
LEGACY_FILE = AUTH_DIR / "credentials.json"
ENCRYPTED_FILE = AUTH_DIR / "credentials.dat"

_KEYRING_SERVICE = "DeployX"
_KEYRING_ACCOUNT = "credentials"

_IS_WINDOWS = sys.platform.startswith("win")

try:  # keyring is optional; DPAPI / file fallbacks always work.
    import keyring as _keyring  # type: ignore
except Exception:  # pragma: no cover - environment dependent
    _keyring = None


def _ensure_dir() -> None:
    AUTH_DIR.mkdir(parents=True, exist_ok=True)


def _parse(raw: str | None) -> dict | None:
    if not raw:
        return None
    try:
        data = json.loads(raw)
    except (TypeError, ValueError):
        return None
    return data if isinstance(data, dict) else None


# ------------------------------------------------------------------
# keyring backend (Windows Credential Manager, etc.)
# ------------------------------------------------------------------

def _keyring_read() -> dict | None:
    if _keyring is None:
        return None
    try:
        return _parse(_keyring.get_password(_KEYRING_SERVICE, _KEYRING_ACCOUNT))
    except Exception:
        return None


def _keyring_write(payload: str) -> bool:
    if _keyring is None:
        return False
    try:
        _keyring.set_password(_KEYRING_SERVICE, _KEYRING_ACCOUNT, payload)
        return True
    except Exception:
        return False


def _keyring_clear() -> None:
    if _keyring is None:
        return
    try:
        _keyring.delete_password(_KEYRING_SERVICE, _KEYRING_ACCOUNT)
    except Exception:
        pass


# ------------------------------------------------------------------
# Windows DPAPI backend (CryptProtectData / CryptUnprotectData)
# ------------------------------------------------------------------

if _IS_WINDOWS:
    import ctypes
    from ctypes import wintypes

    class _DataBlob(ctypes.Structure):
        _fields_ = [
            ("cbData", wintypes.DWORD),
            ("pbData", ctypes.POINTER(ctypes.c_char)),
        ]

    def _dpapi_call(data: bytes, decrypt: bool) -> bytes | None:
        buffer = ctypes.create_string_buffer(bytes(data) or b"\x00")
        blob_in = _DataBlob(
            len(data),
            ctypes.cast(buffer, ctypes.POINTER(ctypes.c_char)),
        )
        blob_out = _DataBlob()

        crypt32 = ctypes.windll.crypt32
        kernel32 = ctypes.windll.kernel32

        function = (
            crypt32.CryptUnprotectData if decrypt else crypt32.CryptProtectData
        )
        function.argtypes = [
            ctypes.POINTER(_DataBlob),
            ctypes.c_void_p,
            ctypes.c_void_p,
            ctypes.c_void_p,
            ctypes.c_void_p,
            wintypes.DWORD,
            ctypes.POINTER(_DataBlob),
        ]
        function.restype = wintypes.BOOL

        if not function(
            ctypes.byref(blob_in), None, None, None, None, 0,
            ctypes.byref(blob_out),
        ):
            return None

        try:
            return ctypes.string_at(blob_out.pbData, blob_out.cbData)
        finally:
            kernel32.LocalFree.argtypes = [ctypes.c_void_p]
            kernel32.LocalFree.restype = ctypes.c_void_p
            kernel32.LocalFree(ctypes.cast(blob_out.pbData, ctypes.c_void_p))

    def _dpapi_encrypt(text: str) -> bytes | None:
        return _dpapi_call(text.encode("utf-8"), decrypt=False)

    def _dpapi_decrypt(data: bytes) -> str | None:
        raw = _dpapi_call(data, decrypt=True)
        return raw.decode("utf-8", "replace") if raw is not None else None

else:  # pragma: no cover - non-Windows platforms
    def _dpapi_encrypt(text: str) -> bytes | None:
        return None

    def _dpapi_decrypt(data: bytes) -> str | None:
        return None


# ------------------------------------------------------------------
# Read / write with migration
# ------------------------------------------------------------------

def _read_secure() -> dict | None:
    data = _keyring_read()
    if data is not None:
        return data

    if _IS_WINDOWS and ENCRYPTED_FILE.exists():
        try:
            raw = _dpapi_decrypt(ENCRYPTED_FILE.read_bytes())
        except OSError:
            raw = None
        return _parse(raw)

    return None


def _read_legacy() -> dict | None:
    if not LEGACY_FILE.exists():
        return None
    try:
        return _parse(LEGACY_FILE.read_text(encoding="utf-8"))
    except OSError:
        return None


def _write_best(data: dict[str, Any]) -> bool:
    """Write to the best available backend. Returns True if secure."""
    _ensure_dir()
    payload = json.dumps(data, indent=2)

    secure = _keyring_write(payload)

    if _IS_WINDOWS:
        encrypted = _dpapi_encrypt(payload)
        if encrypted is not None:
            try:
                ENCRYPTED_FILE.write_bytes(encrypted)
                secure = True
            except OSError:
                pass

    if not secure:
        LEGACY_FILE.write_text(payload, encoding="utf-8")

    return secure


def _remove_legacy() -> None:
    try:
        LEGACY_FILE.unlink()
    except OSError:
        pass


# ------------------------------------------------------------------
# Public API
# ------------------------------------------------------------------

def load_all() -> dict[str, Any]:
    data = _read_secure()
    if data is not None:
        return data

    legacy = _read_legacy()
    if legacy is not None:
        if _write_best(legacy):
            _remove_legacy()
        return legacy

    return {}


def save_all(data: dict[str, Any]) -> None:
    if _write_best(data):
        _remove_legacy()


def get_provider(provider: str) -> dict[str, Any] | None:
    value = load_all().get(provider)
    return value if isinstance(value, dict) else None


def set_provider(provider: str, credentials: dict[str, Any]) -> None:
    data = load_all()
    data[provider] = credentials
    save_all(data)


def delete_provider(provider: str) -> None:
    data = load_all()
    data.pop(provider, None)
    if data:
        save_all(data)
    else:
        delete_all()


def delete_all() -> None:
    _keyring_clear()
    for path in (ENCRYPTED_FILE, LEGACY_FILE):
        try:
            path.unlink()
        except OSError:
            pass


def backend_label() -> str:
    """Human-readable description of the active secure backend."""
    if _keyring is not None:
        if _IS_WINDOWS:
            return "Windows Credential Manager (keyring)"
        try:
            return f"keyring ({_keyring.get_keyring().__class__.__name__})"
        except Exception:
            return "OS keyring"
    if _IS_WINDOWS:
        return "Windows DPAPI (per-user encrypted)"
    return "plaintext file (no secure backend available)"
