"""
CLI-facing endpoints (the token-broker side).

Flow used by DeployX-02 capable CLI builds:

1. ``POST /v1/session``            - CLI sends its GitHub/Vercel tokens once.
                                     The backend verifies them against the
                                     real provider APIs, stores them
                                     encrypted, and returns a session token.
2. ``POST /v1/deployment-ticket``  - CLI exchanges the session for a
                                     short-lived deployment ticket that the
                                     n8n workflow can use.

Provider tokens are accepted here and never returned - not here, not to n8n.
"""

from __future__ import annotations

import logging

from flask import Blueprint, g, jsonify, request

from backend import auth, tickets
from backend.auth import error_response, require_session
from backend.db import new_session, session_scope
from services import github_service, vercel_service

log = logging.getLogger("deployx.backend.oauth")

bp = Blueprint("oauth", __name__, url_prefix="/v1")


def _json_body() -> dict:
    return request.get_json(silent=True) or {}


@bp.post("/session")
def create_session():
    """
    Exchange provider tokens for a DeployX session.

    The tokens are verified against GitHub/Vercel before being stored, so a
    forged username can never create an account.
    """
    body = _json_body()

    github_token = (body.get("github_token") or "").strip()
    vercel_token = (body.get("vercel_token") or "").strip()

    if not github_token and not vercel_token:
        return error_response(
            "invalid_request",
            "A GitHub or Vercel access token is required.",
            400,
        )

    github_user = None
    vercel_user = None

    if github_token:
        try:
            github_user = github_service.get_authenticated_user(github_token)
        except Exception:
            log.warning("Rejected GitHub token during session creation.")
            return error_response(
                "github_token_invalid",
                "The GitHub token could not be verified.",
                401,
            )

    if vercel_token:
        vercel_user = vercel_service.get_authenticated_user(vercel_token)
        if not vercel_user:
            log.warning("Rejected Vercel token during session creation.")
            return error_response(
                "vercel_token_invalid",
                "The Vercel token could not be verified.",
                401,
            )

    with session_scope() as session:
        user = auth.find_or_create_user(session, github_user, vercel_user)

        if github_token:
            auth.set_provider_connection(
                session,
                user,
                "github",
                github_token,
                provider_user_id=github_user.get("id"),
                provider_username=github_user.get("login"),
            )

        if vercel_token:
            auth.set_provider_connection(
                session,
                user,
                "vercel",
                vercel_token,
                provider_user_id=vercel_user.get("sub"),
                provider_username=vercel_user.get("username"),
            )

        session_token, expires_at = auth.issue_session_token(session, user)
        context = auth.user_context(user)

    return jsonify(
        {
            "ok": True,
            "session_token": session_token,
            "expires_at": expires_at.isoformat(),
            "user": context,
        }
    )


@bp.get("/session/me")
@require_session
def session_me():
    session = new_session()
    try:
        user = session.get(type(g.deployx_user), g.deployx_user.id)
        return jsonify({"ok": True, "user": auth.user_context(user)})
    finally:
        session.close()


@bp.post("/deployment-ticket")
@require_session
def create_deployment_ticket():
    """
    Issue a short-lived deployment ticket for the current session.

    Returns only the ticket and non-sensitive deployment context - never a
    provider token.
    """
    body = _json_body()

    session = new_session()
    try:
        user = session.get(type(g.deployx_user), g.deployx_user.id)

        raw, expires_at, ticket = tickets.create_ticket(
            session,
            user,
            deployment_id=body.get("deployment_id"),
            project_name=body.get("project_name"),
            repository=body.get("repository"),
        )

        context = dict(auth.user_context(user))
        context.update(
            {
                "deployment_id": ticket.deployment_id,
                "project_name": ticket.project_name,
                "repository": ticket.repository,
                "branch": body.get("branch") or "main",
            }
        )

        session.commit()

        return jsonify(
            {
                "ok": True,
                "ticket": raw,
                "expires_at": expires_at.isoformat(),
                "context": context,
            }
        )
    finally:
        session.close()


@bp.post("/session/revoke")
@require_session
def revoke_session():
    user = g.deployx_user
    user.session_token_hash = None
    user.session_expires_at = None

    session = new_session()
    try:
        merged = session.get(type(user), user.id)
        merged.session_token_hash = None
        merged.session_expires_at = None
        session.commit()
    finally:
        session.close()

    return jsonify({"ok": True})
