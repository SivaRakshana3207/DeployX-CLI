"""
Short-lived deployment tickets.

A ticket lets the DeployX-02 n8n workflow act *as a DeployX user* without
ever receiving a provider token:

* the ticket value is cryptographically random and shown exactly once,
* only its SHA-256 hash is stored,
* it expires quickly (default 10 minutes),
* it stays valid across the whole deployment: the repository stage and the
  Vercel stage both reuse the same ticket (strategy C),
* it is *closed* only when the deployment reaches a terminal state
  (READY / ERROR / CANCELED / failed). A closed ticket is dead everywhere,
  so replays are rejected on every endpoint,
* otherwise it simply expires at its TTL.

A closed ticket is what stops a replay: ``resolve_ticket`` rejects any
ticket whose ``used_at`` has been set, regardless of endpoint.
"""

from __future__ import annotations

import logging
from datetime import timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from backend import config, crypto
from backend.models import DeploymentTicket, User, utcnow

log = logging.getLogger("deployx.backend.tickets")


class TicketError(RuntimeError):
    """Raised when a ticket cannot be used. ``code`` is a safe error code."""

    def __init__(self, code: str, message: str, status: int = 401):
        super().__init__(message)
        self.code = code
        self.message = message
        self.status = status


def create_ticket(
    session: Session,
    user: User,
    deployment_id: str | None = None,
    project_name: str | None = None,
    repository: str | None = None,
    ttl_seconds: int | None = None,
) -> tuple:
    """
    Issue a ticket for ``user``.

    Returns ``(raw_ticket, expires_at, ticket)``. The raw value is never
    recoverable from the database afterwards.
    """
    ttl = ttl_seconds or config.TICKET_TTL_SECONDS
    raw = crypto.generate_ticket(32)
    expires_at = utcnow() + timedelta(seconds=ttl)

    ticket = DeploymentTicket(
        user_id=user.id,
        ticket_hash=crypto.hash_token(raw),
        deployment_id=deployment_id,
        project_name=project_name,
        repository=repository,
        expires_at=expires_at,
    )
    session.add(ticket)
    session.flush()

    log.info(
        "Issued deployment ticket %s for user %s (ttl=%ss).",
        ticket.id,
        user.id,
        ttl,
    )
    return raw, expires_at, ticket


def resolve_ticket(
    session: Session,
    raw_ticket: str | None,
    consume: bool = False,
) -> tuple:
    """
    Resolve a ticket to ``(user, ticket)``.

    A ticket that has already been closed (or that has expired) is rejected
    on *every* endpoint, so a closed ticket cannot be replayed.

    ``consume=True`` closes the ticket immediately; normally the ticket is
    closed later by :func:`close_ticket` once the deployment finishes.
    """
    if not raw_ticket:
        raise TicketError("invalid_ticket", "A deployment ticket is required.")

    ticket = session.scalar(
        select(DeploymentTicket).where(
            DeploymentTicket.ticket_hash == crypto.hash_token(raw_ticket)
        )
    )

    if ticket is None:
        raise TicketError("invalid_ticket", "The deployment ticket is not valid.")

    if ticket.expires_at < utcnow():
        raise TicketError(
            "ticket_expired", "The deployment ticket has expired.", 401
        )

    if ticket.used_at is not None:
        raise TicketError(
            "ticket_used",
            "The deployment ticket has already been used.",
            401,
        )

    if consume:
        ticket.used_at = utcnow()
        session.flush()

    user = session.get(User, ticket.user_id)
    if user is None:
        raise TicketError("invalid_ticket", "The deployment ticket is not valid.")

    return user, ticket


def close_ticket(session: Session, ticket) -> bool:
    """
    Close a ticket once the deployment reached a terminal state.

    Returns True when this call actually closed it.
    """
    if ticket is None or ticket.used_at is not None:
        return False

    ticket.used_at = utcnow()
    session.flush()

    log.info("Closed deployment ticket %s.", ticket.id)
    return True
