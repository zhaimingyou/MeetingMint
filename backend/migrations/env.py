"""Alembic migration environment (task 2.3).

This wires Alembic to the application's data layer so migrations always match
the runtime schema and configuration:

- ``target_metadata`` is ``app.db.Base.metadata``. Importing :mod:`app.models`
  registers every ORM table (users, sessions, meeting_records, processing_jobs,
  transcripts, transcript_segments, meeting_minutes, action_items) on that
  shared metadata, so autogenerate compares against the real model definitions.
- The database URL is resolved at runtime (never hardcoded) with this
  precedence:
    1. the ``sqlalchemy.url`` main option if set programmatically
       (:func:`app.migrations_runner.run_migrations` sets this), else
    2. the ``ALEMBIC_DATABASE_URL`` environment variable (convenient for an
       ad-hoc ``alembic upgrade head`` against a throwaway SQLite file), else
    3. ``app.config.get_settings().database_url`` (the runtime default).
  Resolving via settings keeps migrations aligned with the app's own config.
- For SQLite, ``render_as_batch=True`` is enabled so future ALTER migrations
  (which SQLite does not support directly) are emitted using batch mode.

The engine is built with :func:`app.db.make_engine` so the same SQLite pragmas
the application relies on (WAL journal mode, ``busy_timeout``, foreign-key
enforcement) are applied while migrations run.
"""

from __future__ import annotations

import os

from alembic import context

# Import the app metadata. Importing app.models registers ALL tables on
# Base.metadata; importing app.db gives us Base + the engine builder. Neither
# import constructs Settings (get_settings is lazy), so importing this env is
# cheap and does not require the full runtime environment.
from app.db import Base, make_engine
import app.models  # noqa: F401  (side effect: registers tables on Base.metadata)

# Alembic Config object, providing access to values within alembic.ini.
config = context.config

# Target metadata for 'autogenerate' support.
target_metadata = Base.metadata


def _resolve_database_url() -> str:
    """Return the database URL using the documented precedence."""
    url = config.get_main_option("sqlalchemy.url")
    if url:
        return url

    env_url = os.environ.get("ALEMBIC_DATABASE_URL")
    if env_url:
        return env_url

    # Lazy import so a configured URL path never forces Settings construction
    # (which requires bootstrap admin / redis env vars).
    from app.config import get_settings

    return get_settings().database_url


def _is_sqlite(url: str) -> bool:
    return url.startswith("sqlite")


def run_migrations_offline() -> None:
    """Run migrations in 'offline' mode (emit SQL without a DBAPI connection)."""
    url = _resolve_database_url()
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        render_as_batch=_is_sqlite(url),
        compare_type=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """Run migrations in 'online' mode against a live connection."""
    url = _resolve_database_url()
    connectable = make_engine(url)
    try:
        with connectable.connect() as connection:
            context.configure(
                connection=connection,
                target_metadata=target_metadata,
                render_as_batch=_is_sqlite(url),
                compare_type=True,
            )
            with context.begin_transaction():
                context.run_migrations()
    finally:
        connectable.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
