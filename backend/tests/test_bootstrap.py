"""Unit tests for app.security.bootstrap (task 3.3).

Example-based tests for the initial-admin seeding described in design.md's Data
Models note (seed exactly one admin from ``bootstrap_admin_*`` if absent; hash
the password; never persist/log plaintext; idempotent).

Each test builds the schema on a fresh in-memory SQLite engine and drives
:func:`ensure_bootstrap_admin` directly with an injected ``Settings`` so no real
environment or ``/data`` engine is required.
"""

from __future__ import annotations

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session as OrmSession

from app.config import Settings
from app.db import make_engine, make_session_factory
from app.models import User, create_all
from app.security.bootstrap import ensure_bootstrap_admin
from app.security.passwords import verify_password

ADMIN_USERNAME = "admin"
ADMIN_PASSWORD = "s3cret-bootstrap-pass"


@pytest.fixture()
def session() -> OrmSession:
    """A session over a fresh in-memory schema; disposed after the test."""
    engine = make_engine("sqlite://")
    create_all(engine)
    factory = make_session_factory(engine)
    sess = factory()
    try:
        yield sess
    finally:
        sess.close()
        engine.dispose()


def _settings(
    username: str = ADMIN_USERNAME, password: str = ADMIN_PASSWORD
) -> Settings:
    """Build a Settings instance directly (no environment dependency)."""
    return Settings(
        bootstrap_admin_username=username,
        bootstrap_admin_password=password,
        redis_url="redis://localhost:6379/0",
    )  # type: ignore[call-arg]


def _user_count(session: OrmSession) -> int:
    return int(session.execute(select(func.count(User.id))).scalar_one())


# --- creation on empty DB ----------------------------------------------------


def test_creates_admin_on_empty_db(session: OrmSession) -> None:
    created = ensure_bootstrap_admin(session, _settings())
    session.commit()

    assert created is not None
    assert created.username == ADMIN_USERNAME
    # The seeded bootstrap account holds the administrator role (Req 1.2).
    assert created.is_admin is True
    assert created.disabled is False
    # Password verifies via the one-way hash, and the stored hash is not the
    # plaintext.
    assert verify_password(ADMIN_PASSWORD, created.password_hash) is True
    assert created.password_hash != ADMIN_PASSWORD
    assert _user_count(session) == 1


def test_plaintext_never_appears_in_stored_hash(session: OrmSession) -> None:
    created = ensure_bootstrap_admin(session, _settings())
    session.commit()

    assert created is not None
    # The plaintext must not be embedded anywhere in the stored credential.
    assert ADMIN_PASSWORD not in created.password_hash


# --- idempotency -------------------------------------------------------------


def test_calling_twice_creates_only_one_admin(session: OrmSession) -> None:
    first = ensure_bootstrap_admin(session, _settings())
    session.commit()
    second = ensure_bootstrap_admin(session, _settings())
    session.commit()

    assert first is not None  # created on first call
    assert second is None  # second call is a no-op
    assert _user_count(session) == 1


def test_existing_admin_is_not_modified(session: OrmSession) -> None:
    created = ensure_bootstrap_admin(session, _settings())
    session.commit()
    assert created is not None
    original_id = created.id
    original_hash = created.password_hash

    # A subsequent call -- even with a DIFFERENT configured password -- must not
    # overwrite the existing admin's credentials.
    result = ensure_bootstrap_admin(
        session, _settings(password="a-completely-different-password")
    )
    session.commit()

    assert result is None
    refreshed = session.get(User, original_id)
    assert refreshed is not None
    assert refreshed.password_hash == original_hash
    # The original password still verifies; the new one does not.
    assert verify_password(ADMIN_PASSWORD, refreshed.password_hash) is True
    assert (
        verify_password("a-completely-different-password", refreshed.password_hash)
        is False
    )
    assert _user_count(session) == 1


def test_repeated_calls_keep_exactly_one_admin(session: OrmSession) -> None:
    for _ in range(5):
        ensure_bootstrap_admin(session, _settings())
        session.commit()
    assert _user_count(session) == 1


def test_existing_configured_account_role_is_not_modified(
    session: OrmSession,
) -> None:
    """An operator-managed role change must survive a process restart."""
    from app.security.passwords import hash_password

    existing = User(
        username=ADMIN_USERNAME,
        password_hash=hash_password(ADMIN_PASSWORD),
        is_admin=False,
    )
    session.add(existing)
    session.commit()
    original_hash = existing.password_hash

    result = ensure_bootstrap_admin(session, _settings())
    session.commit()

    assert result is None  # not a new creation
    refreshed = session.get(User, existing.id)
    assert refreshed is not None
    assert refreshed.is_admin is False  # role preserved
    assert refreshed.password_hash == original_hash  # password untouched
    assert _user_count(session) == 1
