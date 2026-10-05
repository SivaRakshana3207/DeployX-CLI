"""
DeployX backend client (token broker).

Used by Phase 2 / DeployX-02: after the CLI authenticates with GitHub and
Vercel it hands those tokens to the DeployX backend **once** and receives a
short-lived deployment ticket. The backend then performs the GitHub and
Vercel operations on that user's behalf, so the n8n workflow never receives
a provider token.

The backend is REQUIRED. If it is unreachable or rejects the credentials the
caller must stop: Phase 2 never silently falls back to the legacy DeployX-01
flow, because doing so could deploy under the wrong credentials.
"""

from __future__ import annotations

import requests

from config import DEPLOYX_BACKEND_TIMEOUT, DEPLOYX_BACKEND_URL


class BackendUnavailable(RuntimeError):
    """The DeployX backend could not be used for this request."""


def backend_url() -> str:
    return DEPLOYX_BACKEND_URL


def is_available(timeout: float = 1.5) -> bool:
    """True when a DeployX backend answers /health."""
    try:
        response = requests.get(
            f"{DEPLOYX_BACKEND_URL}/health",
            timeout=timeout,
        )
        return response.status_code == 200

    except (requests.RequestException, OSError):
        return False


def open_session(
    github_token: str,
    vercel_token: str,
    timeout: int = DEPLOYX_BACKEND_TIMEOUT,
) -> dict:
    """
    Hand the user's provider tokens to the backend and get a session.

    The backend verifies them, stores them encrypted and returns a session
    token. Provider tokens are never returned to the CLI again.
    """
    if not github_token and not vercel_token:
        raise BackendUnavailable("No provider tokens to register.")

    try:
        response = requests.post(
            f"{DEPLOYX_BACKEND_URL}/v1/session",
            json={
                "github_token": github_token,
                "vercel_token": vercel_token,
            },
            timeout=timeout,
        )
    except requests.RequestException as exc:
        raise BackendUnavailable("DeployX backend is not reachable.") from exc

    if response.status_code != 200:
        raise BackendUnavailable(
            f"DeployX backend rejected the session ({response.status_code})."
        )

    payload = response.json()

    if not payload.get("session_token"):
        raise BackendUnavailable("DeployX backend returned no session token.")

    return payload


def create_deployment_ticket(
    session_token: str,
    deployment_id: str,
    project_name: str,
    repository: str,
    branch: str = "main",
    timeout: int = DEPLOYX_BACKEND_TIMEOUT,
) -> dict:
    """Ask the backend for a short-lived, single-use deployment ticket."""
    try:
        response = requests.post(
            f"{DEPLOYX_BACKEND_URL}/v1/deployment-ticket",
            json={
                "deployment_id": deployment_id,
                "project_name": project_name,
                "repository": repository,
                "branch": branch,
            },
            headers={
                "Authorization": f"Bearer {session_token}",
            },
            timeout=timeout,
        )
    except requests.RequestException as exc:
        raise BackendUnavailable("DeployX backend is not reachable.") from exc

    if response.status_code != 200:
        raise BackendUnavailable(
            f"DeployX backend rejected the ticket ({response.status_code})."
        )

    payload = response.json()

    if not payload.get("ticket"):
        raise BackendUnavailable("DeployX backend returned no ticket.")

    return payload


def prepare_deployment_ticket(
    github_token: str,
    vercel_token: str,
    deployment_id: str,
    project_name: str,
    repository: str,
    branch: str = "main",
):
    """
    Convenience wrapper: open a session and mint one ticket.

    Returns the raw ticket string. Raises :class:`BackendUnavailable` when the
    backend is unreachable or rejects the request — the caller must stop
    rather than continue without a ticket.
    """
    session = open_session(github_token, vercel_token)

    ticket = create_deployment_ticket(
        session["session_token"],
        deployment_id=deployment_id,
        project_name=project_name,
        repository=repository,
        branch=branch,
    )

    return ticket["ticket"]
