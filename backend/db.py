"""
Database engine / session helpers for the DeployX backend.

SQLite is used for local development. The engine is created lazily so tests
can point ``DEPLOYX_DATABASE_URL`` at a temporary database and call
``reset_engine()``.
"""

from __future__ import annotations

import logging
from contextlib import contextmanager
from typing import Iterator

from sqlalchemy import create_engine
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from backend import config
from backend.models import Base

log = logging.getLogger("deployx.backend.db")

_engine: Engine | None = None
_session_factory: sessionmaker | None = None


def _engine_options() -> dict:
    options = {"future": True, "pool_pre_ping": True}
    if config.DATABASE_URL.startswith("sqlite"):
        # Flask serves requests from a thread pool.
        options["connect_args"] = {"check_same_thread": False}
    return options


def get_engine() -> Engine:
    global _engine
    if _engine is None:
        _engine = create_engine(config.DATABASE_URL, **_engine_options())
        log.debug("Database engine created.")
    return _engine


def get_session_factory() -> sessionmaker:
    global _session_factory
    if _session_factory is None:
        _session_factory = sessionmaker(
            bind=get_engine(),
            expire_on_commit=False,
            future=True,
        )
    return _session_factory


def init_db() -> None:
    """Create tables that do not exist yet (safe to call repeatedly)."""
    Base.metadata.create_all(get_engine())


def new_session() -> Session:
    return get_session_factory()()


@contextmanager
def session_scope() -> Iterator[Session]:
    session = new_session()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def reset_engine() -> None:
    """Dispose of the engine (used by tests after changing the URL)."""
    global _engine, _session_factory
    if _engine is not None:
        _engine.dispose()
    _engine = None
    _session_factory = None
