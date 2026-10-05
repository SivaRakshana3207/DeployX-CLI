"""
Vercel operations executed with the *DeployX user's own* stored token.

Same broker contract as the GitHub helper: the token is decrypted in-process,
used for outbound HTTPS calls, and never returned, logged or embedded in a
URL.

Environment variables are handled by name only - DeployX never reads or
returns environment variable *values*.
"""

from __future__ import annotations

import logging

import requests
from sqlalchemy.orm import Session

from backend import config
from backend import github
from backend.auth import get_provider_token
from backend.models import User

log = logging.getLogger("deployx.backend.vercel")

VERCEL_API_URL = "https://api.vercel.com"

# Provider slug - request fields can never change this.
_PROVIDER = "vercel"


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
        "Accept": "application/json",
        "User-Agent": "DeployX-Backend",
    }


def _safe_error_detail(response) -> str:
    """
    Extract a short, safe reason from a Vercel error response.

    Vercel's ``error.message`` explains the real cause (for example a missing
    GitHub integration). Only the human-readable message is used; nothing that
    could contain credentials is included.
    """
    try:
        payload = response.json() or {}
    except Exception:
        return ""

    error = payload.get("error") if isinstance(payload, dict) else None

    if isinstance(error, dict):
        return str(error.get("message") or "")[:300]

    if isinstance(error, str):
        return error[:300]

    message = payload.get("message") if isinstance(payload, dict) else None
    return str(message or "")[:300]


def get_authenticated_user(session: Session, user: User) -> dict:
    """
    Return the Vercel identity and default scope that owns the token.

    ``team_id``/``team_slug`` identify the scope Vercel will deploy into, so
    the backend can reject requests that target somebody else's scope.

    The call deliberately does **not** force ``teamId``: Vercel answers 403
    "Not authorized" for a team the token cannot access, which would make every
    personal-scope account look like a bad credential.
    """
    token = get_provider_token(session, user, _PROVIDER)

    def _fetch() -> "requests.Response":
        return requests.get(
            f"{VERCEL_API_URL}/v2/user",
            headers=_headers(token),
            timeout=config.HTTP_TIMEOUT,
        )

    try:
        response = _fetch()
    except requests.RequestException as exc:
        log.warning("Vercel request failed: %s", type(exc).__name__)
        raise ProviderError(
            "provider_unavailable", "Vercel could not be reached.", 502
        ) from exc

    # A token scoped to a team only sees that team. Fall back to the team's own
    # listing so the deployment lands in the scope that actually owns the token.
    if response.status_code in (401, 403):
        detail = _safe_error_detail(response)
        log.warning(
            "Vercel /v2/user returned HTTP %s (%s); trying team listing.",
            response.status_code,
            detail or "no detail",
        )
        try:
            teams = requests.get(
                f"{VERCEL_API_URL}/v2/teams",
                headers=_headers(token),
                timeout=config.HTTP_TIMEOUT,
            )
        except requests.RequestException as exc:
            raise ProviderError(
                "provider_unavailable", "Vercel could not be reached.", 502
            ) from exc

        if teams.status_code == 200:
            entries = (teams.json() or {}).get("teams") or []
            if entries:
                team = entries[0]
                return {
                    "id": team.get("id"),
                    "username": team.get("name") or team.get("slug"),
                    "team_id": str(team.get("id")),
                    "team_slug": team.get("slug"),
                }

        raise ProviderError(
            "provider_error",
            "Vercel rejected the stored credential"
            + (f": {detail}" if detail else "."),
            502,
        )

    if response.status_code != 200:
        log.warning("Vercel returned HTTP %s for /v2/user.", response.status_code)
        detail = _safe_error_detail(response)
        raise ProviderError(
            "provider_error",
            "Vercel rejected the stored credential"
            + (f": {detail}" if detail else "."),
            502,
        )

    data = response.json() or {}
    account = data.get("user") or data.get("userInfo") or {}

    team_id = data.get("teamId")
    if team_id is None:
        team_id = (data.get("team") or {}).get("id")

    return {
        "id": account.get("id") or team_id,
        "username": account.get("username") or account.get("email"),
        "team_id": str(team_id) if team_id is not None else None,
        "team_slug": (data.get("team") or {}).get("slug"),
    }


def assert_scope_matches(
    identity: dict,
    requested_team_id: str | None,
) -> None:
    """
    A ticket may only deploy into the scope the token belongs to.

    This is what stops Alice's ticket from deploying into Bob's team.
    """
    if requested_team_id is None:
        return

    own_team = identity.get("team_id")
    if own_team and str(requested_team_id) != str(own_team):
        log.warning(
            "Rejected cross-account Vercel scope: %s != %s",
            requested_team_id,
            own_team,
        )
        raise ProviderError(
            "identity_mismatch",
            "The requested Vercel scope does not belong to the authenticated user.",
            403,
        )


# Terminal build states: once one of these is observed the deployment is
# finished, so the deployment ticket can be closed (strategy C).
TERMINAL_STATES = {"ready", "error", "canceled", "cancelled", "failed"}


def is_terminal_state(state) -> bool:
    """True when a Vercel readyState means the deployment has finished."""
    return str(state or "").strip().lower() in TERMINAL_STATES


def _public_url(payload: dict) -> str | None:
    """
    The address a visitor can actually open.

    Vercel's ``url`` is the immutable per-deployment host, which on a team
    account is protected and answers with a Vercel login page. The production
    alias (``<project>.vercel.app``) is the public address for the deployment,
    so it is preferred when present. ``url`` remains the fallback.
    """
    aliases = payload.get("alias") or []

    if isinstance(aliases, list):
        for alias in aliases:
            if not isinstance(alias, str) or not alias:
                continue
            # Skip the per-commit / per-branch hosts; they are protected too.
            if "-git-" in alias:
                continue
            return alias

    return payload.get("url")


def _normalise_deployment(payload: dict) -> dict:
    """Safe subset of a Vercel deployment payload (no secrets)."""
    project = payload.get("project") or {}
    return {
        "id": payload.get("id") or payload.get("uid"),
        "name": payload.get("name"),
        # Needed by the env-names step, so expose it with every deployment.
        "project_id": payload.get("projectId") or project.get("id"),
        "url": _public_url(payload),
        # The immutable per-deployment host, kept for logs/inspection.
        "deployment_url": payload.get("url"),
        "ready_state": payload.get("readyState") or payload.get("ready_state"),
        "inspector_url": payload.get("inspectorUrl"),
        "target": payload.get("target"),
    }


def _repo_belongs_to_user(identity: dict, org: str, repo: str) -> bool:
    """True when ``org/repo`` is the repository owner's own namespace."""
    owner = str(identity.get("username") or "").strip().lower()

    return bool(owner) and str(org).strip().lower() == owner


def _fetch_repo_id(session: Session, user: User, org: str, repo: str):
    """
    Resolve the numeric GitHub repository id using the user's own token.

    ``github.get_repository`` already refuses a repository that is not owned by
    the ticket's user, so this preserves multi-user isolation.
    """
    try:
        found = github.get_repository(session, user, f"{org}/{repo}")
    except github.ProviderError as exc:
        raise ProviderError(exc.code, exc.message, exc.status) from exc

    if not found:
        raise ProviderError(
            "not_found",
            "The GitHub repository was not found for this user.",
            404,
        )

    return found.get("repo_id")


def create_deployment(
    session: Session,
    user: User,
    name: str,
    git_source: dict,
    team_id: str | None = None,
    target: str = "production",
) -> dict:
    """
    Create a deployment from a GitHub source using the user's own token.

    The scope is verified against the authenticated identity first, so a
    ticket can never deploy into another user's Vercel account/team.
    """
    identity = get_authenticated_user(session, user)
    assert_scope_matches(identity, team_id)

    token = get_provider_token(session, user, _PROVIDER)

    body = {
        "name": name,
        "gitSource": {
            "type": git_source.get("type", "github"),
            "org": git_source.get("org"),
            "repo": git_source.get("repo"),
            "ref": git_source.get("ref", "main"),
        },
        "target": target,
    }

    # Vercel requires the numeric GitHub repository id in gitSource. The
    # workflow cannot know it, so it is resolved here with the *same user's own*
    # GitHub token - never a shared or developer credential.
    repo_id = git_source.get("repo_id") or git_source.get("repoId")

    if not repo_id:
        org = git_source.get("org")
        repo = git_source.get("repo")

        if org and repo:
            repo_id = _fetch_repo_id(session, user, org, repo)

    if repo_id:
        body["gitSource"]["repoId"] = repo_id
    else:
        raise ProviderError(
            "provider_error",
            "Vercel needs the GitHub repository id, which could not be "
            "resolved for the requested repository.",
            502,
        )

    # ``teamId`` must be omitted entirely for a personal-scope token: Vercel
    # answers 400 "should NOT have additional property `teamId`" when it is
    # present without a real team, and a stringified ``None`` is exactly that.
    own_team = (identity.get("team_id") or "").strip()

    if own_team and own_team.lower() not in ("none", "null"):
        body["teamId"] = own_team

    try:
        response = requests.post(
            f"{VERCEL_API_URL}/v13/deployments",
            headers=_headers(token),
            json=body,
            timeout=config.HTTP_TIMEOUT,
        )
    except requests.RequestException as exc:
        log.warning("Vercel create deployment failed: %s", type(exc).__name__)
        raise ProviderError(
            "provider_unavailable", "Vercel could not be reached.", 502
        ) from exc

    if response.status_code not in (200, 201):
        log.warning(
            "Vercel create returned HTTP %s: %s",
            response.status_code,
            _safe_error_detail(response) or "no detail",
        )
        detail = _safe_error_detail(response)
        raise ProviderError(
            "provider_error",
            "Vercel could not create the deployment"
            + (f": {detail}" if detail else "."),
            502,
        )

    return _normalise_deployment(response.json() or {})


def get_deployment_status(
    session: Session,
    user: User,
    deployment_id: str,
) -> dict:
    """Return the current state of one of the user's own deployments."""
    if not deployment_id:
        raise ProviderError(
            "invalid_request", "A deployment id is required.", 400
        )

    token = get_provider_token(session, user, _PROVIDER)

    try:
        response = requests.get(
            f"{VERCEL_API_URL}/v13/deployments/{deployment_id}",
            headers=_headers(token),
            timeout=config.HTTP_TIMEOUT,
        )
    except requests.RequestException as exc:
        log.warning("Vercel status failed: %s", type(exc).__name__)
        raise ProviderError(
            "provider_unavailable", "Vercel could not be reached.", 502
        ) from exc

    if response.status_code == 404:
        raise ProviderError(
            "not_found", "The deployment was not found.", 404
        )

    if response.status_code != 200:
        log.warning("Vercel status returned HTTP %s.", response.status_code)
        raise ProviderError(
            "provider_error", "Vercel could not return the deployment.", 502
        )

    return _normalise_deployment(response.json() or {})


def get_env_var_names(
    session: Session,
    user: User,
    project_id: str,
) -> list:
    """
    Return environment variable **names** for a Vercel project.

    Values are intentionally dropped: DeployX only ever needs the names.
    """
    if not project_id:
        raise ProviderError("invalid_request", "A project id is required.", 400)

    token = get_provider_token(session, user, _PROVIDER)

    try:
        response = requests.get(
            f"{VERCEL_API_URL}/v9/projects/{project_id}/env",
            headers=_headers(token),
            timeout=config.HTTP_TIMEOUT,
        )
    except requests.RequestException as exc:
        log.warning("Vercel env failed: %s", type(exc).__name__)
        raise ProviderError(
            "provider_unavailable", "Vercel could not be reached.", 502
        ) from exc

    if response.status_code != 200:
        log.warning("Vercel env returned HTTP %s.", response.status_code)
        raise ProviderError(
            "provider_error", "Vercel could not return the environment.", 502
        )

    payload = response.json() or {}
    entries = payload.get("envs") if isinstance(payload, dict) else payload

    names = []
    for entry in entries or []:
        key = entry.get("key")
        if key:
            names.append(key)

    return sorted(set(names))
