"""Container startup entrypoint (task 14).

This is the small wiring the API and worker containers run *before* serving,
finally connecting the pieces earlier tasks deliberately left un-invoked at
import time:

1. :func:`app.startup.load_settings_or_exit` -- fail-fast config validation
   (Req 10.4 / 10.5): a misconfigured deployment (e.g. ``*_IMPL=http`` without
   the endpoint URLs) logs every offending key and aborts with a non-zero exit
   instead of starting in a broken state.
2. data-directory creation -- a named Docker volume mounts empty at ``/data``,
   hiding any dirs baked into the image, and SQLite will not create the
   database file's parent directory; so the audio dir and the DB's parent dir
   are ensured here.
3. :func:`app.migrations_runner.run_migrations` -- apply Alembic migrations up
   to ``head`` so the schema on the shared volume exists/upgrades before any
   request or job is served (idempotent).
4. :func:`app.security.bootstrap.run_bootstrap` -- seed the single initial admin
   from ``BOOTSTRAP_ADMIN_*`` if absent (idempotent; never overwrites, never
   logs the plaintext).

Roles (see docker-compose.yml)
------------------------------
- **API** runs the full wiring then starts uvicorn::

      python -m app.entrypoint && uvicorn app.main:app --host 0.0.0.0 --port 8000

- **Worker** validates config + ensures the data dirs only (the API owns
  migration + bootstrap, so two processes never race on the same SQLite file)
  then starts Celery::

      python -m app.entrypoint --dirs-only && celery -A app.worker.celery_app:celery_app worker

Both paths are idempotent and safe to re-run on container restart.
"""

from __future__ import annotations

import logging
import os
import sys

from app.config import Settings

logger = logging.getLogger("app.entrypoint")


def _sqlite_path(database_url: str) -> str | None:
    """Return the on-disk file path for a SQLite URL, else ``None``.

    Handles the absolute ``sqlite:////data/db/app.db`` form (four slashes) and
    the relative ``sqlite:///rel.db`` form. Non-SQLite URLs (e.g. a future
    server-managed database) return ``None``.
    """
    if not database_url.startswith("sqlite"):
        return None
    _, _, path = database_url.partition("sqlite:///")
    return path or None


def ensure_data_dirs(settings: Settings) -> None:
    """Create the audio storage dir and the SQLite database's parent dir."""
    os.makedirs(settings.audio_storage_dir, exist_ok=True)
    db_path = _sqlite_path(settings.database_url)
    if db_path:
        db_dir = os.path.dirname(db_path)
        if db_dir:
            os.makedirs(db_dir, exist_ok=True)


def main(argv: list[str] | None = None) -> None:
    """Run startup wiring. With ``--dirs-only`` skip migration + bootstrap.

    Order: validate config (fail fast) -> ensure data dirs -> (full mode only)
    apply migrations -> seed bootstrap admin.
    """
    logging.basicConfig(
        level=os.environ.get("LOG_LEVEL", "INFO"),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    args = list(sys.argv[1:] if argv is None else argv)

    # Imported lazily so the fail-fast validator (and the heavy Alembic/engine
    # machinery below) are only touched when actually wiring up a container.
    from app.startup import load_settings_or_exit

    settings = load_settings_or_exit()  # aborts non-zero on invalid config

    ensure_data_dirs(settings)
    if "--dirs-only" in args:
        logger.info("entrypoint: config valid; ensured data dirs (dirs-only mode)")
        return

    from app.migrations_runner import run_migrations
    from app.security.bootstrap import run_bootstrap

    run_migrations()
    # Fail fast if an encrypted Configuration_Secret exists but CONFIG_SECRET_KEY
    # is absent/invalid (Req 4.6). Runs after migrations so runtime_config exists.
    from app.db import get_sessionmaker
    from app.startup import ensure_runtime_secrets_or_exit

    ensure_runtime_secrets_or_exit(get_sessionmaker())
    created = run_bootstrap(settings)
    logger.info(
        "entrypoint complete: config valid; migrations applied; "
        "bootstrap admin created=%s",
        created,
    )


if __name__ == "__main__":
    main()
