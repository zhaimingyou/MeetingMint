"""Unit tests for app.security.sessions (task 3.2).

Example-based tests for the server-side session lifecycle (Req 1.2 / 1.5 / 1.6,
design Property 2). They build the schema on a fresh in-memory SQLite engine and
drive :func:`issue_session`, :func:`validate_session`, :func:`invalidate_session`,
and :func:`purge_sessions` with an injected ``now`` so expiry boundaries are
exact and deterministic (no sleeping).

The Property 2 hypothesis property test is task 3.6; these are the example tests.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session as OrmSession

from app.db import make_engine, make_session_factory
from app.models import Session, User, create_all
from app.security.sessions import (
    invalidate_session,
    issue_session,
    purge_sessions,
    validate_session,
)

T0 = datetime(2024, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
TTL = 60  # minutes


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


def _make_user(session: OrmSession, username: str = "alice") -> User:
    user = User(username=username, password_hash="bcrypt$hash")
    session.add(user)
    session.commit()
    return user


# --- issuance + happy-path validation ---------------------------------------


def test_issue_then_validate_succeeds(session: OrmSession) -> None:
    user = _make_user(session)
    issued = issue_session(session, user.id, ttl_minutes=TTL, now=T0)
    session.commit()

    assert issued.token  # opaque token present
    assert issued.invalidated is False
    assert issued.issued_at == T0
    assert issued.expires_at == T0 + timedelta(minutes=TTL)

    validated = validate_session(session, issued.token, now=T0)
    assert validated is not None
    assert validated.id == user.id


def test_validate_just_before_expiry_succeeds(session: OrmSession) -> None:
    user = _make_user(session)
    issued = issue_session(session, user.id, ttl_minutes=TTL, now=T0)
    session.commit()

    just_before = issued.expires_at - timedelta(microseconds=1)
    validated = validate_session(session, issued.token, now=just_before)
    assert validated is not None
    assert validated.id == user.id


def test_validate_at_exact_expiry_fails(session: OrmSession) -> None:
    user = _make_user(session)
    issued = issue_session(session, user.id, ttl_minutes=TTL, now=T0)
    session.commit()

    # Boundary is exclusive: now == expires_at is INVALID.
    assert validate_session(session, issued.token, now=issued.expires_at) is None


def test_validate_after_expiry_fails(session: OrmSession) -> None:
    user = _make_user(session)
    issued = issue_session(session, user.id, ttl_minutes=TTL, now=T0)
    session.commit()

    after = issued.expires_at + timedelta(seconds=1)
    assert validate_session(session, issued.token, now=after) is None


def test_validate_at_issuance_instant_succeeds(session: OrmSession) -> None:
    user = _make_user(session)
    issued = issue_session(session, user.id, ttl_minutes=TTL, now=T0)
    session.commit()

    # Lower bound is inclusive: now == issued_at is VALID.
    validated = validate_session(session, issued.token, now=T0)
    assert validated is not None


# --- invalidation (logout) ---------------------------------------------------


def test_invalidated_token_fails_validation(session: OrmSession) -> None:
    user = _make_user(session)
    issued = issue_session(session, user.id, ttl_minutes=TTL, now=T0)
    session.commit()

    invalidate_session(session, issued.token)
    session.commit()

    # Even well within the TTL window, an invalidated token is rejected.
    assert validate_session(session, issued.token, now=T0) is None


def test_invalidate_is_idempotent_for_unknown_and_already_invalid(
    session: OrmSession,
) -> None:
    user = _make_user(session)
    issued = issue_session(session, user.id, ttl_minutes=TTL, now=T0)
    session.commit()

    # Unknown token: no error.
    invalidate_session(session, "does-not-exist")
    session.commit()

    # Already-invalidated token: second call is a no-op, no error.
    invalidate_session(session, issued.token)
    session.commit()
    invalidate_session(session, issued.token)
    session.commit()

    fetched = session.get(Session, issued.token)
    assert fetched is not None and fetched.invalidated is True


# --- unknown token -----------------------------------------------------------


def test_unknown_token_fails_validation(session: OrmSession) -> None:
    assert validate_session(session, "no-such-token", now=T0) is None


# --- purge -------------------------------------------------------------------


def test_purge_removes_expired_and_invalidated_keeps_valid(
    session: OrmSession,
) -> None:
    user = _make_user(session)

    # Valid (issued at T0, expires at T0+60).
    valid = issue_session(session, user.id, ttl_minutes=TTL, now=T0)
    # Expired (issued two hours before T0).
    expired = issue_session(
        session, user.id, ttl_minutes=TTL, now=T0 - timedelta(hours=2)
    )
    # Invalidated but not expired.
    invalidated = issue_session(session, user.id, ttl_minutes=TTL, now=T0)
    session.commit()
    invalidate_session(session, invalidated.token)
    session.commit()

    deleted = purge_sessions(session, now=T0)
    session.commit()

    assert deleted == 2  # expired + invalidated
    remaining = session.execute(select(Session.token)).scalars().all()
    assert remaining == [valid.token]
    # The surviving session still validates.
    assert validate_session(session, valid.token, now=T0) is not None


def test_purge_treats_exact_expiry_as_expired(session: OrmSession) -> None:
    user = _make_user(session)
    issued = issue_session(session, user.id, ttl_minutes=TTL, now=T0)
    session.commit()

    # now == expires_at: expired for both validation and purge.
    deleted = purge_sessions(session, now=issued.expires_at)
    session.commit()
    assert deleted == 1
    assert session.get(Session, issued.token) is None


def test_purge_with_nothing_to_remove_returns_zero(session: OrmSession) -> None:
    user = _make_user(session)
    issue_session(session, user.id, ttl_minutes=TTL, now=T0)
    session.commit()

    deleted = purge_sessions(session, now=T0)
    session.commit()
    assert deleted == 0


# --- token opacity / uniqueness ----------------------------------------------


def test_tokens_are_unique_and_opaque_across_many_issuances(
    session: OrmSession,
) -> None:
    user = _make_user(session)
    tokens = {issue_session(session, user.id, ttl_minutes=TTL, now=T0).token
              for _ in range(500)}
    session.commit()

    # All 500 tokens are distinct (unguessable, no collisions).
    assert len(tokens) == 500
    # Opaque: no token leaks the user id and each carries real entropy.
    for token in tokens:
        assert user.id not in token
        assert len(token) >= 32


# --- settings-driven default TTL (60 minutes) --------------------------------


def test_default_ttl_uses_settings_60_minutes(
    session: OrmSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.config import get_settings

    monkeypatch.setenv("BOOTSTRAP_ADMIN_USERNAME", "admin")
    monkeypatch.setenv("BOOTSTRAP_ADMIN_PASSWORD", "s3cret-pass")
    monkeypatch.setenv("REDIS_URL", "redis://localhost:6379/0")
    get_settings.cache_clear()
    try:
        user = _make_user(session)
        issued = issue_session(session, user.id, now=T0)  # no explicit ttl
        session.commit()
        assert issued.expires_at == T0 + timedelta(minutes=60)
    finally:
        get_settings.cache_clear()
