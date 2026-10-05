"""
n8n-facing endpoints.

Contract (DeployX-02):

* Every endpoint requires ``Authorization: Bearer <DEPLOYX_N8N_SERVICE_KEY>``.
* Every request body carries the short-lived ``ticket``.
* The backend resolves the DeployX user **from the ticket**. Identity fields
  in the body (``github_username``, ``vercel_username``, ...) are never
  trusted for authorization; if they contradict the ticket they are treated
  as an attack and rejected.
* Provider tokens are never returned here, never accepted here, and never
  placed in a URL.

Ticket lifecycle (strategy C): the ticket stays valid across the repository
stage *and* the Vercel stage, so it can be reused while n8n polls. It is
closed only when ``/v1/n8n/vercel/deployments/status`` observes a terminal
build state, and it always dies at its TTL. A closed ticket is rejected on
every endpoint.
"""

from __future__ import annotations

import logging

from flask import Blueprint, jsonify, request

from backend import auth, github, tickets, vercel
from backend.auth import (
    ProviderNotConnectedError,
    error_response,
    require_service_key,
)
from backend.db import new_session, session_scope

log = logging.getLogger("deployx.backend.n8n")

bp = Blueprint("n8n", __name__, url_prefix="/v1/n8n")


def _body() -> dict:
    return request.get_json(silent=True) or {}


def _extract_ticket(body: dict) -> str | None:
    """
    Pull the deployment ticket out of an n8n request.

    ``deployment_ticket`` is the canonical field name used by the CLI and the
    DeployX-02 workflow. ``ticket`` is accepted as a legacy alias so an older
    saved workflow keeps working; both are validated identically (hashed,
    expiry-checked, ownership-checked), so accepting the alias does not weaken
    authentication in any way.
    """
    for field in ("deployment_ticket", "ticket"):
        value = body.get(field)
        if isinstance(value, str) and value.strip():
            return value.strip()

    # Header form is also supported so a ticket never has to travel in a URL.
    header = request.headers.get("X-DeployX-Deployment-Ticket")
    if header and header.strip():
        return header.strip()

    return None


def _resolve(session, body: dict, consume: bool = False):
    """Resolve the ticket, returning ``(user, ticket, error_response)``."""
    try:
        user, ticket = tickets.resolve_ticket(
            session, _extract_ticket(body), consume=consume
        )
        return user, ticket, None
    except tickets.TicketError as exc:
        return None, None, error_response(exc.code, exc.message, exc.status)


def _reject_spoofed_identity(body: dict, user):
    """Refuse bodies that claim to be somebody else."""
    for field in ("github_username", "vercel_username"):
        claimed = body.get(field)
        if not claimed:
            continue
        expected = (
            user.github_username if field == "github_username"
            else user.vercel_username
        )
        if not expected:
            continue
        same = (
            str(claimed).lower() == str(expected).lower()
            if field == "github_username"
            else str(claimed) == str(expected)
        )
        if not same:
            log.warning("Rejected spoofed %s in n8n request.", field)
            return error_response(
                "identity_mismatch",
                "The supplied identity does not match the ticket owner.",
                403,
            )
    return None


def _provider_not_connected() -> tuple:
    return error_response(
        "provider_not_connected",
        "This user is not connected to that provider.",
        409,
    )


@bp.post("/deployment-context")
@require_service_key
def deployment_context():
    """Non-sensitive deployment/user context for this ticket."""
    body = _body()

    session = new_session()
    try:
        user, ticket, error = _resolve(session, body)
        if error:
            return error

        spoof_error = _reject_spoofed_identity(body, user)
        if spoof_error:
            return spoof_error

        context = dict(auth.user_context(user))
        context.update(
            {
                "deployment_id": ticket.deployment_id,
                "project_name": ticket.project_name,
                "repository": ticket.repository,
                "branch": body.get("branch") or "main",
                "commit_sha": body.get("commit_sha"),
                "framework": body.get("framework"),
                "project_type": body.get("project_type"),
                "env_var_names": body.get("env_var_names") or [],
            }
        )
        return jsonify({"ok": True, "context": context})
    finally:
        session.close()


@bp.post("/github/repos/create")
@require_service_key
def github_repos_create():
    """Create (or reuse) a repository inside the ticket owner's account."""
    body = _body()

    with session_scope() as session:
        user, ticket, error = _resolve(session, body, consume=False)
        if error:
            return error

        spoof_error = _reject_spoofed_identity(body, user)
        if spoof_error:
            return spoof_error

        name = (body.get("name") or "").strip()
        if not name:
            return error_response(
                "invalid_request", "A repository name is required.", 400
            )

        try:
            # The owner always comes from the stored GitHub credential.
            repository = github.create_repository(
                session,
                user,
                name,
                private=bool(body.get("private", False)),
                description=body.get("description") or "",
            )
        except ProviderNotConnectedError:
            return _provider_not_connected()
        except github.ProviderError as exc:
            return error_response(exc.code, exc.message, exc.status)

        return jsonify({"ok": True, "repository": repository})


@bp.post("/github/repos/get")
@require_service_key
def github_repos_get():
    """Fetch a repository, enforcing ticket-owner ownership."""
    body = _body()

    session = new_session()
    try:
        user, ticket, error = _resolve(session, body)
        if error:
            return error

        repository_name = (body.get("repository") or "").strip()
        if not repository_name:
            return error_response(
                "invalid_request", "A repository (owner/name) is required.", 400
            )

        try:
            repository = github.get_repository(session, user, repository_name)
        except ProviderNotConnectedError:
            return _provider_not_connected()
        except github.ProviderError as exc:
            return error_response(exc.code, exc.message, exc.status)

        return jsonify({"ok": True, "repository": repository})
    finally:
        session.close()


@bp.post("/vercel/deployments/create")
@require_service_key
def vercel_deployments_create():
    """Create a Vercel deployment inside the ticket owner's account."""
    body = _body()

    with session_scope() as session:
        user, ticket, error = _resolve(session, body, consume=False)
        if error:
            return error

        spoof_error = _reject_spoofed_identity(body, user)
        if spoof_error:
            return spoof_error

        name = (body.get("name") or ticket.project_name or "").strip()
        if not name:
            return error_response(
                "invalid_request", "A deployment name is required.", 400
            )

        try:
            deployment = vercel.create_deployment(
                session,
                user,
                name,
                git_source=body.get("git_source") or {},
                # team_id can only *reject* another user's scope.
                team_id=body.get("team_id"),
                target=body.get("target") or "production",
            )
        except ProviderNotConnectedError:
            return _provider_not_connected()
        except vercel.ProviderError as exc:
            return error_response(exc.code, exc.message, exc.status)

        return jsonify({"ok": True, "deployment": deployment})


@bp.post("/vercel/deployments/status")
@require_service_key
def vercel_deployments_status():
    """
    Deployment state for the ticket owner's Vercel account.

    Strategy C: the ticket stays valid across the whole polling window and is
    closed *here*, once the build reaches a terminal state (READY / ERROR /
    CANCELED). A closed ticket is then rejected on every later call.
    """
    body = _body()

    with session_scope() as session:
        user, ticket, error = _resolve(session, body)
        if error:
            return error

        try:
            deployment = vercel.get_deployment_status(
                session, user, (body.get("deployment_id") or "").strip()
            )
        except ProviderNotConnectedError:
            return _provider_not_connected()
        except vercel.ProviderError as exc:
            return error_response(exc.code, exc.message, exc.status)

        closed = vercel.is_terminal_state(deployment.get("ready_state"))
        if closed:
            tickets.close_ticket(session, ticket)

        return jsonify(
            {
                "ok": True,
                "deployment": deployment,
                "ticket_closed": closed,
            }
        )


@bp.post("/vercel/env-names")
@require_service_key
def vercel_env_names():
    """Environment variable NAMES only - values are never returned."""
    body = _body()

    session = new_session()
    try:
        user, ticket, error = _resolve(session, body)
        if error:
            return error

        try:
            names = vercel.get_env_var_names(
                session, user, (body.get("project_id") or "").strip()
            )
        except ProviderNotConnectedError:
            return _provider_not_connected()
        except vercel.ProviderError as exc:
            return error_response(exc.code, exc.message, exc.status)

        return jsonify({"ok": True, "env_var_names": names})
    finally:
        session.close()
