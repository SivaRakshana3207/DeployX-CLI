"""
SQLAlchemy models for the DeployX backend.

Three core entities:

* ``User``              — one DeployX user, with provider identity info and
                          a hashed CLI session token.
* ``ProviderConnection``— one encrypted provider token per user/provider.
* ``DeploymentTicket``  — a short-lived, hashed ticket that lets the n8n
                          workflow act *as that user* without ever seeing
                          a provider token.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import (
    DateTime,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


def utcnow() -> datetime:
    """Timezone-aware UTC now (stored naive-UTC for SQLite friendliness)."""
    return datetime.now(timezone.utc).replace(tzinfo=None)


class Base(DeclarativeBase):
    pass


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)

    # Provider identity discovered from the user's own credentials.
    # These are display/lookup values only - never used to authorize n8n.
    github_username: Mapped[Optional[str]] = mapped_column(
        String(255), nullable=True, index=True
    )
    github_user_id: Mapped[Optional[str]] = mapped_column(
        String(128), nullable=True, index=True
    )
    vercel_username: Mapped[Optional[str]] = mapped_column(
        String(255), nullable=True, index=True
    )
    vercel_user_id: Mapped[Optional[str]] = mapped_column(
        String(128), nullable=True, index=True
    )

    # Hashed CLI session token (the broker hands this back to the CLI).
    session_token_hash: Mapped[Optional[str]] = mapped_column(
        String(64), nullable=True, index=True
    )
    session_expires_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime, nullable=True
    )

    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=utcnow, onupdate=utcnow
    )

    connections: Mapped[list["ProviderConnection"]] = relationship(
        back_populates="user",
        cascade="all, delete-orphan",
        lazy="selectin",
    )
    tickets: Mapped[list["DeploymentTicket"]] = relationship(
        back_populates="user",
        cascade="all, delete-orphan",
        lazy="selectin",
    )


class ProviderConnection(Base):
    __tablename__ = "provider_connections"
    __table_args__ = (
        UniqueConstraint("user_id", "provider", name="uq_user_provider"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), index=True
    )
    provider: Mapped[str] = mapped_column(String(32), index=True)

    # Fernet ciphertext. The plaintext token is never stored or logged.
    encrypted_access_token: Mapped[str] = mapped_column(Text)

    provider_user_id: Mapped[Optional[str]] = mapped_column(
        String(128), nullable=True
    )
    provider_username: Mapped[Optional[str]] = mapped_column(
        String(255), nullable=True
    )
    expires_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime, nullable=True
    )

    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=utcnow, onupdate=utcnow
    )

    user: Mapped["User"] = relationship(back_populates="connections")


class DeploymentTicket(Base):
    __tablename__ = "deployment_tickets"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), index=True
    )

    # Only the SHA-256 hash of the ticket is stored.
    ticket_hash: Mapped[str] = mapped_column(
        String(64), unique=True, index=True
    )

    deployment_id: Mapped[Optional[str]] = mapped_column(
        String(64), nullable=True, index=True
    )
    project_name: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    repository: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)

    expires_at: Mapped[datetime] = mapped_column(DateTime, index=True)
    used_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

    user: Mapped["User"] = relationship(back_populates="tickets")
