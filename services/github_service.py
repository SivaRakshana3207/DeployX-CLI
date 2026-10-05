"""
GitHub REST helpers used by the DeployX CLI.

Repository creation is performed by the CLI with the *authenticated
user's own token*, so a repository is always created or reused inside
that user's GitHub account — never a shared developer account.

The n8n workflow is still called for the rest of the deployment
pipeline (see cli/deployx.py).
"""

from __future__ import annotations

import requests

GITHUB_API_URL = "https://api.github.com"


def _headers(token: str) -> dict:
    return {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": "DeployX",
    }


def get_authenticated_user(token: str) -> dict:
    """Return the authenticated user (``login``, ``id``, ...)."""
    response = requests.get(
        f"{GITHUB_API_URL}/user",
        headers=_headers(token),
        timeout=20,
    )
    response.raise_for_status()
    return response.json()


def validate_token(token: str) -> bool:
    if not token:
        return False
    try:
        response = requests.get(
            f"{GITHUB_API_URL}/user",
            headers=_headers(token),
            timeout=20,
        )
    except requests.RequestException:
        return False
    return response.status_code == 200


def get_repository(token: str, owner: str, name: str) -> dict | None:
    response = requests.get(
        f"{GITHUB_API_URL}/repos/{owner}/{name}",
        headers=_headers(token),
        timeout=20,
    )
    if response.status_code == 200:
        return response.json()
    return None


def _normalise(repo: dict) -> dict:
    full_name = repo.get("full_name")
    return {
        "name": repo.get("name"),
        "full_name": full_name,
        "owner": (repo.get("owner") or {}).get("login"),
        "html_url": repo.get("html_url"),
        "clone_url": repo.get("clone_url")
        or (f"https://github.com/{full_name}.git" if full_name else None),
        "default_branch": repo.get("default_branch") or "main",
    }


def ensure_repository(
    token: str,
    owner: str,
    name: str,
    private: bool = False,
    description: str = "",
) -> dict:
    """
    Create ``owner/name`` if it does not exist, otherwise reuse it.

    Always operates inside the authenticated user's account.
    """
    existing = get_repository(token, owner, name)
    if existing:
        return _normalise(existing)

    payload = {
        "name": name,
        "private": private,
        "auto_init": False,
    }
    if description:
        payload["description"] = description

    response = requests.post(
        f"{GITHUB_API_URL}/user/repos",
        headers=_headers(token),
        json=payload,
        timeout=30,
    )

    if response.status_code == 201:
        return _normalise(response.json())

    if response.status_code == 422:
        # Repository already exists — reuse it.
        existing = get_repository(token, owner, name)
        if existing:
            return _normalise(existing)

    response.raise_for_status()
    return _normalise(response.json())


def create_repository(
    token: str,
    name: str,
    private: bool = False,
    description: str = "",
) -> dict:
    """Backwards-compatible helper that creates ``name`` for the user."""
    user = get_authenticated_user(token)
    return ensure_repository(
        token,
        user["login"],
        name,
        private=private,
        description=description,
    )

