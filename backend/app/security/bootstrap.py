"""Initial admin bootstrap seeding (task 3.3).

At startup the service must guarantee that the single initial administrator
account described by ``Settings.bootstrap_admin_username`` /
``Settings.bootstrap_admin_password`` exists, so an operator can log in to a
freshly deployed instance. This module implements that seeding for the design's
Data Models note: *"At startup the service seeds exactly one initial account
from ``bootstrap_admin_username`` / ``bootstrap_admin_password`` (hashing the
password) if that username does not yet exist; the plaintext password is read
from the environment/secret and never persisted or committed."*

Behavior
--------
- **Idempotent.** If a :class:`~app.models.User` with the configured username
  already exists, nothing is created or modified -- in particular an existing
  admin's ``password_hash`` is **never** overwritten. Repeated calls leave
  exactly one admin row.
- **Hashed only.** When the admin is absent it is created with
  ``password_hash = hash_password(settings.bootstrap_admin_password)`` (a
  one-way salted bcrypt hash). The plaintext password is never stored and never
  logged.
- **No plaintext in logs.** The only thing logged is the username and whether
  the account was created; the password value never appears in any log line.

Transaction / commit contract
------------------------------
Mirroring :mod:`app.security.sessions`, **the caller owns the transaction.**
:func:`ensure_bootstrap_admin` operates on a caller-supplied SQLAlchemy session
and ``add`` / ``flush`` only -- it never commits. This keeps it composable with
request- or startup-scoped transactions. For the common startup case where
there is no surrounding transaction, :func:`run_bootstrap` is a thin convenience
wrapper that opens an :func:`app.db.session_scope` (which commits on clean exit)
and calls :func:`ensure_bootstrap_admin` inside it.

This module provides the function plus the convenience wrapper only; wiring it
into the FastAPI startup lifespan is a later task.
"""

from __future__ import annotations

import logging

from sqlalchemy import select
from sqlalchemy.orm import Session as OrmSession

from app.config import Settings, get_settings
from app.models import User
from app.security.passwords import hash_password

__all__ = ["ensure_bootstrap_admin", "run_bootstrap"]

logger = logging.getLogger(__name__)


def ensure_bootstrap_admin(
    db: OrmSession,
    settings: Settings | None = None,
) -> User | None:
    """Seed the initial admin from settings if it does not already exist.

    Looks up a :class:`~app.models.User` whose ``username`` equals
    ``settings.bootstrap_admin_username``. If found, returns ``None`` and makes
    no changes (the existing account -- including its password hash -- is left
    untouched). If absent, creates a new user with a freshly hashed password and
    flushes it into the caller's transaction, returning the created
    :class:`~app.models.User`.

    The change is flushed but **not committed**; the caller commits (see module
    docstring). Use :func:`run_bootstrap` for the standalone startup case.

    Args:
        db: Caller-owned SQLAlchemy session.
        settings: Configuration to read the bootstrap username/password from;
            defaults to the process-wide :func:`app.config.get_settings`.

    Returns:
        The newly created admin :class:`~app.models.User`, or ``None`` if an
        account with the configured username already existed.
    """
    if settings is None:
        settings = get_settings()

    username = settings.bootstrap_admin_username

    existing = db.execute(
        select(User).where(User.username == username)
    ).scalar_one_or_none()
    if existing is not None:
        # Bootstrap is creation-only: operator-managed password and role changes
        # must survive process restarts.
        logger.info(
            "Bootstrap account already present; leaving unchanged "
            "(username=%r, created=False)",
            username,
        )
        return None

    # Hash the plaintext before storage; the plaintext is never persisted or
    # logged -- only the username and the created flag are recorded below.
    user = User(
        username=username,
        password_hash=hash_password(settings.bootstrap_admin_password),
        is_admin=True,
    )
    db.add(user)
    db.flush()
    logger.info(
        "Seeded bootstrap admin (username=%r, created=True)", username
    )
    return user


def run_bootstrap(settings: Settings | None = None) -> bool:
    """Open a committing session and ensure the bootstrap admin exists.

    Convenience wrapper for startup use where there is no surrounding
    transaction: opens an :func:`app.db.session_scope` (which commits on clean
    exit, rolls back on error) and calls :func:`ensure_bootstrap_admin` within
    it.

    Args:
        settings: Configuration; defaults to :func:`app.config.get_settings`.

    Returns:
        ``True`` if a new admin was created, ``False`` if one already existed.
    """
    # Imported lazily so importing this module does not force the process-wide
    # /data engine into existence (keeps import-only test paths clean).
    from app.db import session_scope

    with session_scope() as db:
        created = ensure_bootstrap_admin(db, settings)
        return created is not None
