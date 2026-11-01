"""Data-layer engine and session factory (task 2.1).

This module builds the SQLAlchemy 2.0 :class:`~sqlalchemy.engine.Engine` for the
service's SQLite database and exposes the session machinery shared by both the
Backend_API and the Job_Processor (Celery worker).

Design rationale (see design.md -> "Persistence" and "Async Job Processing"):

- The API and the worker are **separate processes** that open the *same* SQLite
  file on a shared Docker volume. To allow one concurrent writer plus multiple
  readers without spurious "database is locked" errors, every connection is
  opened in **WAL journal mode** with a ``busy_timeout`` so a brief writer
  overlap retries instead of failing immediately.
- ``PRAGMA foreign_keys=ON`` is set per connection because SQLite disables
  foreign-key enforcement by default, and the data model relies on referential
  integrity (no-orphan / cascade guarantees in later tasks).
- These pragmas are applied via a ``connect`` event listener so they run for
  **every** pooled connection in both processes -- there is no single "open the
  DB" call we could otherwise hang them on.
- The worker keeps write transactions short and never holds one across a long
  ffmpeg/ASR/LLM call; this module only provides the plumbing for that
  discipline (short-lived sessions, ``session_scope`` committing promptly).

Pooling choice: for a file-based SQLite database SQLAlchemy's default pool is
appropriate (low-concurrency, single-server tool). ``check_same_thread=False``
is set so a connection created in one thread can be reused by another (FastAPI
threadpool / Celery worker threads); concurrency is still bounded by worker
settings and the single-writer WAL model. For an in-memory database (used by
some tests) a :class:`~sqlalchemy.pool.StaticPool` is used so the single shared
connection -- and therefore the schema -- persists across sessions.

Engine construction is **lazy**: the real ``/data`` engine is not built at import
time, so importing this module (and therefore ``app.main``) never requires the
database file or its directory to exist. Tests can build their own engine
against a temp file or in-memory URL via :func:`make_engine` /
:func:`create_engine_from_settings` without touching ``/data``.
"""

from __future__ import annotations

import logging
import os
from collections.abc import Iterator
from contextlib import contextmanager

from sqlalchemy import create_engine
from sqlalchemy.engine import Engine
from sqlalchemy.engine.url import URL, make_url
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker
from sqlalchemy.pool import StaticPool

from app.config import Settings, get_settings

logger = logging.getLogger(__name__)

# A few seconds is enough for a brief writer overlap to clear under WAL while
# still surfacing a genuine deadlock reasonably quickly.
DEFAULT_BUSY_TIMEOUT_MS = 5000


class Base(DeclarativeBase):
    """Declarative base for all ORM models.

    Task 2.2 defines the ORM models (``User``, ``Session``, ``Meeting_Record``,
    etc.) by importing this ``Base``; task 2.3 uses ``Base.metadata`` for the
    initial Alembic migration. Keep it here so the data layer has a single,
    import-cheap source of truth that does not pull in the model modules.
    """


def _is_sqlite(url: URL) -> bool:
    return url.get_backend_name() == "sqlite"


def _is_memory_sqlite(url: URL) -> bool:
    """True for an in-memory SQLite URL (``sqlite://`` or ``:memory:``)."""
    return _is_sqlite(url) and (not url.database or url.database == ":memory:")


def _ensure_sqlite_directory(url: URL) -> None:
    """Create the parent directory of a file-based SQLite database if missing.

    Makes the default ``sqlite:////data/db/app.db`` work on a fresh volume.
    No-ops for non-SQLite URLs and for in-memory databases, so tests using a
    temp path or ``:memory:`` are unaffected.
    """
    if not _is_sqlite(url) or _is_memory_sqlite(url):
        return
    assert url.database is not None  # narrowed by _is_memory_sqlite check
    directory = os.path.dirname(os.path.abspath(url.database))
    if directory:
        os.makedirs(directory, exist_ok=True)


def _register_sqlite_pragmas(
    engine: Engine, *, busy_timeout_ms: int, foreign_keys: bool
) -> None:
    """Apply WAL, busy_timeout, and foreign_keys to every SQLite connection."""
    from sqlalchemy import event

    @event.listens_for(engine, "connect")
    def _set_sqlite_pragma(dbapi_connection, connection_record):  # noqa: ANN001, ARG001
        cursor = dbapi_connection.cursor()
        try:
            # WAL permits one writer + concurrent readers. For an in-memory DB
            # SQLite ignores this and reports "memory"; harmless.
            cursor.execute("PRAGMA journal_mode=WAL")
            cursor.execute(f"PRAGMA busy_timeout={int(busy_timeout_ms)}")
            if foreign_keys:
                cursor.execute("PRAGMA foreign_keys=ON")
        finally:
            cursor.close()


def make_engine(
    database_url: str | URL,
    *,
    busy_timeout_ms: int = DEFAULT_BUSY_TIMEOUT_MS,
    foreign_keys: bool = True,
    echo: bool = False,
) -> Engine:
    """Build a configured :class:`Engine` for ``database_url``.

    For SQLite URLs this sets ``check_same_thread=False``, ensures the database
    file's directory exists, selects a :class:`StaticPool` for in-memory
    databases, and registers the WAL / ``busy_timeout`` / ``foreign_keys``
    pragmas on every connection. Non-SQLite URLs are created with defaults
    (the data layer is SQLite today but stays portable).
    """
    url = make_url(database_url)
    connect_args: dict[str, object] = {}
    engine_kwargs: dict[str, object] = {"echo": echo}

    if _is_sqlite(url):
        connect_args["check_same_thread"] = False
        _ensure_sqlite_directory(url)
        if _is_memory_sqlite(url):
            # A single shared connection keeps the in-memory schema alive across
            # sessions in the same process (useful for fast tests).
            engine_kwargs["poolclass"] = StaticPool

    engine = create_engine(url, connect_args=connect_args, **engine_kwargs)

    if _is_sqlite(url):
        _register_sqlite_pragmas(
            engine, busy_timeout_ms=busy_timeout_ms, foreign_keys=foreign_keys
        )
    return engine


def create_engine_from_settings(
    settings: Settings | None = None,
    *,
    busy_timeout_ms: int = DEFAULT_BUSY_TIMEOUT_MS,
    foreign_keys: bool = True,
    echo: bool = False,
) -> Engine:
    """Build an :class:`Engine` from ``settings.database_url``.

    ``settings`` defaults to the process-wide :func:`app.config.get_settings`.
    Tests can pass a ``Settings`` pointing at a temp-file or in-memory database
    to avoid touching ``/data``.
    """
    if settings is None:
        settings = get_settings()
    return make_engine(
        settings.database_url,
        busy_timeout_ms=busy_timeout_ms,
        foreign_keys=foreign_keys,
        echo=echo,
    )


def make_session_factory(engine: Engine) -> sessionmaker[Session]:
    """Return a :class:`sessionmaker` bound to ``engine``.

    Sessions are short-lived: ``autoflush`` stays on for normal ORM ergonomics,
    ``autocommit`` is off (explicit transactions), and ``expire_on_commit`` is
    disabled so attributes remain usable after a short transaction commits --
    matching the worker's "commit before the long call" discipline.
    """
    return sessionmaker(
        bind=engine,
        class_=Session,
        autoflush=True,
        expire_on_commit=False,
    )


# --- Lazily-constructed, process-wide default engine / session factory -------
#
# Built on first use from get_settings() so importing this module never forces
# the real /data engine into existence (keeps app.main importable in tests).

_engine: Engine | None = None
_session_factory: sessionmaker[Session] | None = None


def get_engine() -> Engine:
    """Return the process-wide default engine, building it on first use."""
    global _engine
    if _engine is None:
        _engine = create_engine_from_settings()
    return _engine


def get_sessionmaker() -> sessionmaker[Session]:
    """Return the process-wide default session factory (``SessionLocal``)."""
    global _session_factory
    if _session_factory is None:
        _session_factory = make_session_factory(get_engine())
    return _session_factory


def reset_engine_cache() -> None:
    """Dispose and clear the cached default engine / session factory.

    Primarily for tests that change ``database_url`` between cases. Disposes the
    existing engine's connection pool so file handles are released.
    """
    global _engine, _session_factory
    if _engine is not None:
        _engine.dispose()
    _engine = None
    _session_factory = None


def get_session() -> Iterator[Session]:
    """Yield a short-lived session, then close it.

    Suitable as a FastAPI dependency (``Depends(get_session)``): the route owns
    the transaction and commits explicitly; the session is always closed when
    the request ends. The caller is responsible for ``commit()`` /
    ``rollback()``.
    """
    session = get_sessionmaker()()
    try:
        yield session
    finally:
        session.close()


@contextmanager
def session_scope() -> Iterator[Session]:
    """Context manager for a short transactional unit of work (worker use).

    Commits on clean exit, rolls back on exception, and always closes the
    session. Worker stages wrap each status/result write in a ``session_scope``
    so the transaction commits *before* any long ffmpeg/ASR/LLM call -- a write
    transaction is never held across a long-running call.
    """
    session = get_sessionmaker()()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()
