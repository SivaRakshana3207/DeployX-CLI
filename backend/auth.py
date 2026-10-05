"""
Authentication for the DeployX backend.

Two separate trust boundaries:

1. **n8n -> backend** (``/v1/n8n/*``)
   Requires ``Authorization: Bearer <DEPLOYX_N8N_SERVICE_KEY>`` compared in
   constant time. Missing/incorrect keys are rejected.

2. **CLI -> backend** (``/v1/session``, ``/v1/deployment-ticket``)
   The CLI exchanges its own GitHub/Vercel tokens once. The backend verifies
   them against the real provider APIs, stores them encrypted, and returns a
   short-lived *session token* that carries no provider secret.

n8n never receives provider tokens, and the CLI never receives them back.
"""

from __future__ import annotations

import functools
import logging
from datetime import timedelta
from typing import Callable, Optional

from flask import g, jsonify, request
from sqlalchemy import select
from sqlalchemy.orm import Session

from backend import config, crypto
from backend.models import ProviderConnection, User, utcnow

log = logging.getLogger("deployx.backend.auth")


class ProviderNotConnectedError(RuntimeError):
    """The user has no usable credential for this provider."""

    def __init__(self, provider: str):
        super().__init__(f"No stored credential for provider '{provider}'.")
        self.provider = provider


# ------------------------------------------------------------------
# Error helper
# ------------------------------------------------------------------

def error_response(code: str, message: str, status: int = 400, **extra):
    """Uniform, token-free error envelope."""
    payload = {"ok": False, "error": code, "message": message}
    payload.update(extra)
    return jsonify(payload), status


# ------------------------------------------------------------------
# n8n service key
# ------------------------------------------------------------------

SERVICE_KEY_HEADER = "X-DeployX-Service-Key"


def _bearer_token() -> Optional[str]:
    header = request.headers.get("Authorization", "")
    if not header.lower().startswith("bearer "):
        return None
    return header[7:].strip()


def _service_key_token() -> Optional[str]:
    """
    Read the n8n service key.

    Accepts either the explicit ``X-DeployX-Service-Key`` header (used by the
    DeployX-02 workflow) or ``Authorization: Bearer <key>``.
    """
    direct = (request.headers.get(SERVICE_KEY_HEADER) or "").strip()
    if direct:
        return direct
    return _bearer_token()


def require_service_key(view: Callable) -> Callable:
    """Reject any /v1/n8n/* call without the shared service key."""

    @functools.wraps(view)
    def wrapper(*args, **kwargs):
        provided = _service_key_token()

        if provided is None:
            log.warning("n8n request rejected: missing service key.")
            return error_response("unauthorized", "Missing service key.", 401)

        try:
            expected = config.require_service_key()
        except config.ConfigError:
            log.error(
                "n8n request rejected: DEPLOYX_N8N_SERVICE_KEY is not configured."
            )
            return error_response(
                "service_key_not_configured",
                "The backend is not configured to accept n8n requests.",
                503,
            )

        if not crypto.constant_time_equals(provided, expected):
            log.warning("n8n request rejected: incorrect service key.")
            return error_response("unauthorized", "Invalid service key.", 401)

        g.n8n_authorized = True
        return view(*args, **kwargs)

    return wrapper


# ------------------------------------------------------------------
# Provider connections (encrypted token storage)
# ------------------------------------------------------------------

def set_provider_connection(
    session: Session,
    user: User,
    provider: str,
    access_token: str,
    provider_user_id: Optional[str] = None,
    provider_username: Optional[str] = None,
    expires_at=None,
) -> ProviderConnection:
    """Encrypt and store (or replace) a provider token."""
    encrypted = crypto.encrypt_secret(access_token)

    connection = session.scalar(
        select(ProviderConnection).where(
            ProviderConnection.user_id == user.id,
            ProviderConnection.provider == provider,
        )
    )

    if connection is None:
        connection = ProviderConnection(
            user_id=user.id,
            provider=provider,
        )
        session.add(connection)

    connection.encrypted_access_token = encrypted
    connection.provider_user_id = (
        str(provider_user_id) if provider_user_id is not None else None
    )
    connection.provider_username = provider_username
    connection.expires_at = expires_at
    session.flush()

    log.info("Stored %s connection for user %s.", provider, user.id)
    return connection


def get_provider_connection(
    session: Session,
    user: User,
    provider: str,
) -> Optional[ProviderConnection]:
    return session.scalar(
        select(ProviderConnection).where(
            ProviderConnection.user_id == user.id,
            ProviderConnection.provider == provider,
        )
    )


def get_provider_token(session: Session, user: User, provider: str) -> str:
    """
    Decrypt a provider token for outbound use.

    The value is returned to internal provider helpers only - never to a
    response body, a URL or a log record.
    """
    connection = get_provider_connection(session, user, provider)

    if connection is None:
        raise ProviderNotConnectedError(provider)

    try:
        return crypto.decrypt_secret(connection.encrypted_access_token)
    except crypto.DecryptionError as exc:
        raise ProviderNotConnectedError(provider) from exc


# ------------------------------------------------------------------
# Users and CLI sessions
# ------------------------------------------------------------------

def find_or_create_user(
    session: Session,
    github_user: Optional[dict] = None,
    vercel_user: Optional[dict] = None,
) -> User:
    """
    Resolve the DeployX user from *verified* provider identities.

    Only provider user IDs returned by GitHub/Vercel are used, never a
    client-supplied username.
    """
    user = None

    github_id = (
        str(github_user["id"])
        if github_user and github_user.get("id") is not None
        else None
    )

    # Vercel identity may arrive as "id" or as the OIDC "sub" claim.
    vercel_id = None
    if vercel_user:
        vercel_id = vercel_user.get("id") or vercel_user.get("sub")
        vercel_id = str(vercel_id) if vercel_id is not None else None

    if github_id is not None:
        user = session.scalar(
            select(User).where(User.github_user_id == github_id)
        )

    if user is None and vercel_id is not None:
        user = session.scalar(
            select(User).where(User.vercel_user_id == vercel_id)
        )

    if user is None:
        user = User()
        session.add(user)

    if github_user:
        # GitHub's API returns "login"; normalised callers may use "username".
        user.github_username = (
            github_user.get("username") or github_user.get("login")
        )
        user.github_user_id = github_id

    if vercel_user:
        user.vercel_username = vercel_user.get("username")
        user.vercel_user_id = vercel_id

    session.flush()
    return user


def issue_session_token(
    session: Session,
    user: User,
) -> tuple:
    """Issue a new CLI session token (only the hash is stored)."""
    raw = crypto.generate_ticket(32)
    expires_at = utcnow() + timedelta(seconds=config.SESSION_TTL_SECONDS)

    user.session_token_hash = crypto.hash_token(raw)
    user.session_expires_at = expires_at
    session.flush()

    log.info("Issued a DeployX session for user %s.", user.id)
    return raw, expires_at


def resolve_session_token(session: Session, raw: Optional[str]) -> Optional[User]:
    if not raw:
        return None

    user = session.scalar(
        select(User).where(
            User.session_token_hash == crypto.hash_token(raw)
        )
    )

    if user is None:
        return None

    if user.session_expires_at and user.session_expires_at < utcnow():
        return None

    return user


def require_session(view: Callable) -> Callable:
    """Require a valid DeployX CLI session token."""

    @functools.wraps(view)
    def wrapper(*args, **kwargs):
        from backend.db import new_session

        raw = _bearer_token() or request.headers.get("X-DeployX-Session")

        session = new_session()
        try:
            user = resolve_session_token(session, raw)
            if user is None:
                return error_response(
                    "unauthorized",
                    "A valid DeployX session is required.",
                    401,
                )
            g.deployx_user = user
            return view(*args, **kwargs)
        finally:
            session.close()

    return wrapper


def user_context(user: User) -> dict:
    """Non-sensitive context that is safe to return to the CLI or n8n."""
    return {
        "user_id": user.id,
        "github_username": user.github_username,
        "vercel_username": user.vercel_username,
    }
