"""Unit tests for the data-layer engine/session factory (task 2.1).

These verify the SQLite-specific configuration that both the API and the worker
rely on: WAL journal mode, a ``busy_timeout``, foreign-key enforcement, that a
session can run a trivial query, that the database directory is created on a
fresh path, and that an engine can be built from ``Settings`` pointing at a temp
file. No test writes to ``/data``; all use ``tmp_path`` or in-memory URLs.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from sqlalchemy import text

from app.config import Settings
from app.db import (
    DEFAULT_BUSY_TIMEOUT_MS,
    Base,
    create_engine_from_settings,
    make_engine,
    make_session_factory,
)


def _sqlite_url(db_path: Path) -> str:
    """Build a SQLite URL for an absolute file path (yields four slashes)."""
    return "sqlite:///" + str(db_path)


def test_pragmas_applied_on_connection(tmp_path: Path) -> None:
    """Every connection opens with WAL, the configured busy_timeout, and FKs."""
    engine = make_engine(_sqlite_url(tmp_path / "app.db"))
    try:
        with engine.connect() as conn:
            assert conn.execute(text("PRAGMA journal_mode")).scalar() == "wal"
            assert (
                conn.execute(text("PRAGMA busy_timeout")).scalar()
                == DEFAULT_BUSY_TIMEOUT_MS
            )
            assert conn.execute(text("PRAGMA foreign_keys")).scalar() == 1
    finally:
        engine.dispose()


def test_custom_busy_timeout_is_applied(tmp_path: Path) -> None:
    """A non-default busy_timeout is honored on the connection."""
    engine = make_engine(_sqlite_url(tmp_path / "app.db"), busy_timeout_ms=2500)
    try:
        with engine.connect() as conn:
            assert conn.execute(text("PRAGMA busy_timeout")).scalar() == 2500
    finally:
        engine.dispose()


def test_foreign_keys_can_be_disabled(tmp_path: Path) -> None:
    """foreign_keys pragma reflects the requested setting."""
    engine = make_engine(_sqlite_url(tmp_path / "app.db"), foreign_keys=False)
    try:
        with engine.connect() as conn:
            assert conn.execute(text("PRAGMA foreign_keys")).scalar() == 0
    finally:
        engine.dispose()


def test_session_executes_select_one(tmp_path: Path) -> None:
    """A session built from the factory can execute a trivial query."""
    engine = make_engine(_sqlite_url(tmp_path / "app.db"))
    factory = make_session_factory(engine)
    try:
        session = factory()
        try:
            assert session.execute(text("SELECT 1")).scalar() == 1
            # Pragmas apply to the session's connection too.
            assert session.execute(text("PRAGMA journal_mode")).scalar() == "wal"
            assert session.execute(text("PRAGMA foreign_keys")).scalar() == 1
        finally:
            session.close()
    finally:
        engine.dispose()


def test_directory_created_on_fresh_path(tmp_path: Path) -> None:
    """The parent directory of the SQLite file is created if missing."""
    nested = tmp_path / "does" / "not" / "exist" / "app.db"
    assert not nested.parent.exists()

    engine = make_engine(_sqlite_url(nested))
    try:
        assert nested.parent.exists()
        with engine.connect() as conn:
            assert conn.execute(text("SELECT 1")).scalar() == 1
        # Connecting/writing actually materializes the file.
        assert nested.exists()
    finally:
        engine.dispose()


def test_in_memory_engine_works_and_shares_schema() -> None:
    """An in-memory SQLite engine applies pragmas and persists schema (StaticPool)."""
    engine = make_engine("sqlite://")
    factory = make_session_factory(engine)
    try:
        # Foreign keys + busy_timeout still apply to in-memory connections.
        with engine.connect() as conn:
            assert conn.execute(text("PRAGMA foreign_keys")).scalar() == 1
            assert (
                conn.execute(text("PRAGMA busy_timeout")).scalar()
                == DEFAULT_BUSY_TIMEOUT_MS
            )

        # Schema created on one session is visible to a later session because
        # StaticPool keeps a single shared connection.
        s1 = factory()
        try:
            s1.execute(text("CREATE TABLE t (id INTEGER PRIMARY KEY)"))
            s1.execute(text("INSERT INTO t (id) VALUES (1)"))
            s1.commit()
        finally:
            s1.close()

        s2 = factory()
        try:
            assert s2.execute(text("SELECT count(*) FROM t")).scalar() == 1
        finally:
            s2.close()
    finally:
        engine.dispose()


def test_create_engine_from_settings_uses_database_url(tmp_path: Path) -> None:
    """create_engine_from_settings builds against settings.database_url (temp file)."""
    db_path = tmp_path / "from_settings.db"
    settings = Settings(  # type: ignore[call-arg]
        _env_file=None,
        bootstrap_admin_username="admin",
        bootstrap_admin_password="s3cret-pass",
        redis_url="redis://localhost:6379/0",
        database_url=_sqlite_url(db_path),
    )

    engine = create_engine_from_settings(settings)
    try:
        with engine.connect() as conn:
            assert conn.execute(text("SELECT 1")).scalar() == 1
            assert conn.execute(text("PRAGMA journal_mode")).scalar() == "wal"
        assert db_path.exists()
    finally:
        engine.dispose()


def test_base_is_declarative_with_metadata() -> None:
    """Base exposes SQLAlchemy metadata for models (2.2) and migrations (2.3)."""
    assert hasattr(Base, "metadata")
    assert hasattr(Base, "registry")


def test_importing_db_does_not_build_default_engine(tmp_path: Path) -> None:
    """Importing app.db / app.main must not require the real /data engine.

    The module-level default engine is lazy: it is only constructed on the first
    get_engine() call, so importing never touches settings.database_url.
    """
    import app.db as db_module
    import app.main  # noqa: F401  -- importing must not raise

    assert db_module._engine is None
    assert db_module._session_factory is None
