"""
Authenticated encryption and safe-token helpers for the DeployX backend.

Provider access tokens are encrypted with Fernet (AES-128-CBC + HMAC-SHA256,
i.e. authenticated encryption) before they touch the database, so a stolen
database file does not reveal usable provider credentials.

Nothing in this module ever prints or returns a secret.
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import secrets

from cryptography.fernet import Fernet, InvalidToken

from backend import config

log = logging.getLogger("deployx.backend.crypto")


class DecryptionError(RuntimeError):
    """Raised when a stored credential cannot be decrypted."""


def _fernet() -> Fernet:
    key = config.require_encryption_key()
    return Fernet(key.encode("utf-8") if isinstance(key, str) else key)


def generate_encryption_key() -> str:
    """Return a brand new Fernet key (used by setup + tests)."""
    return Fernet.generate_key().decode("ascii")


def encrypt_secret(plaintext: str) -> str:
    """Encrypt a provider token for storage. Never logs the value."""
    if plaintext is None:
        raise ValueError("Nothing to encrypt.")
    return _fernet().encrypt(str(plaintext).encode("utf-8")).decode("ascii")


def decrypt_secret(ciphertext: str) -> str:
    """Decrypt a stored provider token. Never logs the value."""
    if not ciphertext:
        raise DecryptionError("Stored credential is empty.")
    try:
        return _fernet().decrypt(ciphertext.encode("ascii")).decode("utf-8")
    except InvalidToken as exc:
        # Deliberately vague: never reveal whether the key or data is wrong.
        log.warning("A stored provider credential could not be decrypted.")
        raise DecryptionError("Stored credential could not be decrypted.") from exc


def generate_ticket(nbytes: int = 32) -> str:
    """Cryptographically random, URL-safe ticket value."""
    return secrets.token_urlsafe(nbytes)


def hash_token(token: str) -> str:
    """One-way hash used to store tickets / session tokens."""
    return hashlib.sha256(str(token).encode("utf-8")).hexdigest()


def constant_time_equals(left, right) -> bool:
    """Timing-safe comparison for service keys / session tokens."""
    return hmac.compare_digest(
        str(left or "").encode("utf-8"),
        str(right or "").encode("utf-8"),
    )


def redact(value) -> str:
    """Safe placeholder for any secret-ish value."""
    return "***redacted***" if value else None


class RedactingFilter(logging.Filter):
    """
    Defence in depth: scrub known secret values from log records.

    Provider tokens are never logged in the first place; this filter makes
    sure an accidental ``logger.info(token)`` cannot leak one either.
    """

    def __init__(self, secrets_to_scrub=None):
        super().__init__()
        self._secrets = set(s or "" for s in (secrets_to_scrub or []) if s)

    def add_secret(self, value) -> None:
        if value:
            self._secrets.add(str(value))

    def filter(self, record: logging.LogRecord) -> bool:
        if not self._secrets:
            return True
        try:
            message = record.getMessage()
        except Exception:
            return True
        scrubbed = message
        for secret in self._secrets:
            if secret and secret in scrubbed:
                scrubbed = scrubbed.replace(secret, "***redacted***")
        if scrubbed != message:
            record.msg = scrubbed
            record.args = ()
        return True


redacting_filter = RedactingFilter()


def main() -> None:  # pragma: no cover - convenience helper
    """Print a fresh encryption key for backend/.env."""
    print("# Add this to backend/.env (keep it secret):")
    print(f"DEPLOYX_ENCRYPTION_KEY={generate_encryption_key()}")


if __name__ == "__main__":  # pragma: no cover
    main()
