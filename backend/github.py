"""
GitHub operations executed with the *DeployX user's own* stored token.

The token is decrypted inside this module, used for outbound HTTPS calls and
discarded. It is never returned to the caller, never placed in a URL and
never logged.

Repository ownership is always derived from the authenticated GitHub
account, so one user's ticket can never address another user's repository.
"""

from __future__ import annotations

import logging

import requests
from sqlalchemy.orm import Session

from backend import config
from backend.auth import get_provider_token
from backend.models import User

log = logging.getLogger("deployx.backend.github")

GITHUB_API_URL = "https://api.github.com"

# Provider slug - request fields can never change this.
_PROVIDER = "github"


class ProviderError(RuntimeError):
    """A safe, token-free provider failure."""

    def __init__(self, code: str, message: str, status: int = 502):
        super().__init__(message)
        self.code = code
        self.message = message
        self.status = status


def _headers(token: str) -> dict:
    return {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": "DeployX-Backend",
    }


def _normalise(repo: dict) -> dict:
    """Safe subset of a GitHub repository payload."""
    full_name = repo.get("full_name")
    return {
        "name": repo.get("name"),
        "full_name": full_name,
        "owner": (repo.get("owner") or {}).get("login"),
        "private": bool(repo.get("private", False)),
        "html_url": repo.get("html_url"),
        "clone_url": repo.get("clone_url")
        or (f"https://github.com/{full_name}.git" if full_name else None),
        "default_branch": repo.get("default_branch") or "main",
        # Vercel's create-deployment API requires the numeric GitHub repo id.
        "repo_id": repo.get("id"),
    }


def get_authenticated_user(session: Session, user: User) -> dict:
    """Return the GitHub account that owns the stored token."""
    token = get_provider_token(session, user, _PROVIDER)

    try:
        response = requests.get(
            f"{GITHUB_API_URL}/user",
            headers=_headers(token),
            timeout=config.HTTP_TIMEOUT,
        )
    except requests.RequestException as exc:
        log.warning("GitHub request failed: %s", type(exc).__name__)
        raise ProviderError(
            "provider_unavailable", "GitHub could not be reached.", 502
        ) from exc

    if response.status_code != 200:
        log.warning("GitHub returned HTTP %s for /user.", response.status_code)
        raise ProviderError(
            "provider_error", "GitHub rejected the stored credential.", 502
        )

    data = response.json() or {}
    return {"id": data.get("id"), "username": data.get("login")}


def _authenticated_username(session: Session, user: User) -> str:
    """Owner is always derived from the authenticated GitHub account."""
    identity = get_authenticated_user(session, user)
    username = identity.get("username")
    if not username:
        raise ProviderError(
            "provider_error", "GitHub did not return a username.", 502
        )
    return username


def assert_owned_by_user(
    session: Session,
    user: User,
    repository: str,
) -> str:
    """
    Validate ``owner/name`` against the authenticated GitHub account.

    Raises ``identity_mismatch`` (without revealing any token) when the
    repository does not belong to the ticket holder.
    """
    username = _authenticated_username(session, user)

    if not repository or "/" not in str(repository):
        raise ProviderError(
            "invalid_repository",
            "Repository must be given as owner/name.",
            400,
        )

    owner = str(repository).split("/", 1)[0]

    if owner.lower() != username.lower():
        log.warning(
            "Rejected cross-account repository access: %s != %s",
            owner,
            username,
        )
        raise ProviderError(
            "identity_mismatch",
            "The repository does not belong to the authenticated user.",
            403,
        )

    return username


def _lookup_repository(token: str, username: str, name: str):
    """Fetch one repository, or ``None`` when it does not exist."""
    try:
        response = requests.get(
            f"{GITHUB_API_URL}/repos/{username}/{name}",
            headers=_headers(token),
            timeout=config.HTTP_TIMEOUT,
        )
    except requests.RequestException as exc:
        log.warning("GitHub get repo failed: %s", type(exc).__name__)
        raise ProviderError(
            "provider_unavailable", "GitHub could not be reached.", 502
        ) from exc

    if response.status_code == 404:
        return None

    if response.status_code != 200:
        log.warning("GitHub get repo returned HTTP %s.", response.status_code)
        raise ProviderError(
            "provider_error", "GitHub could not return the repository.", 502
        )

    return _normalise(response.json() or {})


def create_repository(
    session: Session,
    user: User,
    name: str,
    private: bool = False,
    description: str = "",
) -> dict:
    """Create (or reuse) a repository inside the user's own account."""
    username = _authenticated_username(session, user)
    token = get_provider_token(session, user, _PROVIDER)

    existing = _lookup_repository(token, username, name)
    if existing:
        existing["created"] = False
        return existing

    body = {"name": name, "private": bool(private), "auto_init": False}
    if description:
        body["description"] = description

    try:
        response = requests.post(
            f"{GITHUB_API_URL}/user/repos",
            headers=_headers(token),
            json=body,
            timeout=config.HTTP_TIMEOUT,
        )
    except requests.RequestException as exc:
        log.warning("GitHub create repo failed: %s", type(exc).__name__)
        raise ProviderError(
            "provider_unavailable", "GitHub could not be reached.", 502
        ) from exc

    if response.status_code == 422:
        # Already exists - reuse it.
        existing = _lookup_repository(token, username, name)
        if existing:
            existing["created"] = False
            return existing

    if response.status_code not in (200, 201):
        log.warning("GitHub create repo returned HTTP %s.", response.status_code)
        raise ProviderError(
            "provider_error", "GitHub could not create the repository.", 502
        )

    result = _normalise(response.json() or {})
    result["created"] = True
    return result


def get_repository(
    session: Session,
    user: User,
    repository: str,
) -> dict:
    """Fetch a repository, enforcing that it belongs to the user."""
    username = assert_owned_by_user(session, user, repository)
    token = get_provider_token(session, user, _PROVIDER)

    result = _lookup_repository(token, username, repository.split("/", 1)[1])

    if result is None:
        raise ProviderError("not_found", "The repository was not found.", 404)

    result["created"] = False
    return result
