"""Programmatic Alembic migration runner (task 2.3).

Exposes :func:`run_migrations`, a thin, importable helper that applies all
pending Alembic migrations up to ``head``. It is invoked at deploy/startup
(wired by a later task) so the SQLite schema on the shared volume is created or
upgraded before the API and worker serve traffic. Importing this module does
**not** run anything -- the upgrade only happens when :func:`run_migrations` is
called.

Path resolution (so this works from the repo root and inside the container):
the Alembic config file (``alembic.ini``) and the migration script directory
(``migrations/``) are located relative to the **backend package**, not the
current working directory. ``app`` lives at ``<backend>/app``, so the backend
project root is ``Path(app.__file__).parent.parent``. The ``script_location`` is
then set to an absolute path on the :class:`~alembic.config.Config`, overriding
the relative value in ``alembic.ini``. This means a container that copies the
backend tree to, say, ``/app`` (giving ``/app/alembic.ini`` and
``/app/migrations``) resolves correctly regardless of the process's CWD.

Assumptions:
- ``alembic.ini`` and ``migrations/`` sit at the backend project root, beside
  the ``app`` package (the repository's ``backend/`` directory). Task 14's
  container image must preserve this relative layout.
- The effective database URL is chosen by ``migrations/env.py``: the explicit
  ``database_url`` argument here wins (it is set as ``sqlalchemy.url`` on the
  Config); otherwise env.py falls back to ``ALEMBIC_DATABASE_URL`` and then to
  ``app.config.get_settings().database_url``.
"""

from __future__ import annotations

from pathlib import Path

from alembic import command
from alembic.config import Config

import app

# Backend project root: <backend>/app/__init__.py -> parents[1] == <backend>.
_BACKEND_ROOT = Path(app.__file__).resolve().parent.parent
ALEMBIC_INI_PATH = _BACKEND_ROOT / "alembic.ini"
SCRIPT_LOCATION = _BACKEND_ROOT / "migrations"


def make_alembic_config(database_url: str | None = None) -> Config:
    """Build an Alembic :class:`Config` with absolute, CWD-independent paths.

    ``script_location`` is forced to the absolute ``migrations/`` directory so
    Alembic finds ``env.py`` and the ``versions/`` scripts no matter where the
    process runs. When ``database_url`` is provided it is set as
    ``sqlalchemy.url`` so ``env.py`` uses it (its highest-precedence source).
    """
    cfg = Config(str(ALEMBIC_INI_PATH))
    cfg.set_main_option("script_location", str(SCRIPT_LOCATION))
    if database_url is not None:
        cfg.set_main_option("sqlalchemy.url", database_url)
    return cfg


def run_migrations(database_url: str | None = None) -> None:
    """Apply all pending migrations up to ``head``.

    Parameters
    ----------
    database_url:
        Optional explicit SQLAlchemy URL to migrate. When omitted, the URL is
        resolved by ``migrations/env.py`` (``ALEMBIC_DATABASE_URL`` env var, then
        ``app.config.get_settings().database_url``). Passing an explicit URL is
        the most direct way to target a specific database (used by tests and by
        the startup wiring).

    This calls Alembic's ``upgrade`` to ``head``; it is idempotent -- running it
    against an already-current database is a no-op.
    """
    cfg = make_alembic_config(database_url)
    command.upgrade(cfg, "head")
