"""Shared pytest fixtures.

Provides reusable fixtures for HTTP-level tests of the Backend_API:

- :func:`test_settings` -- a :class:`app.config.Settings` built from a minimal,
  deterministic environment (bootstrap admin + redis URL satisfied) pointing at
  an in-memory database, so tests never touch ``/data`` or require real infra.
- :func:`db_engine` / :func:`db_session_factory` -- a fresh in-memory SQLite
  engine with the full schema created via ``app.models.create_all``.
- :func:`client` -- a FastAPI :class:`TestClient` over ``create_app()`` with the
  ``get_session`` and ``get_settings`` dependencies overridden to use the
  in-memory engine and test settings.

Fixtures are opt-in (not autouse), so the existing unit tests that build their
own engines/settings are unaffected.
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session as OrmSession
from sqlalchemy.orm import sessionmaker

from app.config import Settings, get_settings
from app.db import get_session, make_engine, make_session_factory
from app.main import create_app
from app.models import create_all


def pytest_configure(config: pytest.Config) -> None:
    """Lower bcrypt's work factor for the whole test session.

    Production password hashing uses bcrypt's default cost (~12 rounds,
    ~250ms/hash). Several property tests exercise bcrypt hundreds of times:

    - Property 1 hashes each generated example (twice, to prove the salt
      differs);
    - owner-scoped HTTP property tests seed users per example;
    - Property 4 logs in with invalid credentials per example, and the login
      route runs ``verify_password`` against a fixed dummy hash on every
      unknown-user attempt (timing-attack resistance).

    At the default cost these run for tens of seconds of pure CPU, long enough
    for the execution environment's watchdog to hard-kill the whole test
    process (it dies with no traceback and flat memory -- a process-group
    SIGKILL).

    Pinning ``bcrypt.gensalt`` to the minimum cost keeps every freshly produced
    hash a genuine, randomly salted, one-way ``$2b$...`` string -- exactly what
    the properties validate -- while cutting per-hash cost ~80x. The cost factor
    is a deployment tuning parameter, not a validated requirement, so this does
    not weaken any assertion.

    ``verify_password`` reads the cost from the *stored* hash, so the login
    route's ``_DUMMY_PASSWORD_HASH`` (computed at import time, before this hook)
    stays expensive to verify until we re-hash it here at the reduced cost.
    """
    import bcrypt

    real_gensalt = bcrypt.gensalt

    def fast_gensalt(rounds: int = 4, prefix: bytes = b"2b") -> bytes:
        return real_gensalt(rounds, prefix)

    bcrypt.gensalt = fast_gensalt  # type: ignore[assignment]

    # Re-hash the login route's fixed dummy at the now-reduced cost so the
    # unknown-user verify path is cheap. Its plaintext is irrelevant (it must
    # never match a real password); only the verification cost matters here.
    import app.api.auth as auth_mod
    from app.security.passwords import hash_password

    auth_mod._DUMMY_PASSWORD_HASH = hash_password(
        "dummy-password-for-timing-equalization"
    )


@pytest.fixture()
def test_settings() -> Settings:
    """A deterministic Settings instance for HTTP tests (in-memory DB)."""
    return Settings(
        bootstrap_admin_username="admin",
        bootstrap_admin_password="bootstrap-pass",
        redis_url="redis://localhost:6379/0",
        database_url="sqlite://",
        max_credential_length=256,
        session_ttl_minutes=60,
    )  # type: ignore[call-arg]


@pytest.fixture()
def db_engine() -> Iterator[Engine]:
    """A fresh in-memory SQLite engine with all tables created."""
    engine = make_engine("sqlite://")
    create_all(engine)
    try:
        yield engine
    finally:
        engine.dispose()


@pytest.fixture()
def db_session_factory(db_engine: Engine) -> sessionmaker[OrmSession]:
    """A session factory bound to the in-memory test engine."""
    return make_session_factory(db_engine)


@pytest.fixture()
def db_session(db_session_factory: sessionmaker[OrmSession]) -> Iterator[OrmSession]:
    """A standalone session for seeding/asserting outside the HTTP request."""
    sess = db_session_factory()
    try:
        yield sess
    finally:
        sess.close()


@pytest.fixture()
def client(
    test_settings: Settings,
    db_session_factory: sessionmaker[OrmSession],
) -> Iterator["TestClient"]:  # noqa: F821
    """A TestClient with DB and settings dependencies overridden for tests."""
    from fastapi.testclient import TestClient

    app = create_app()

    def _override_get_session() -> Iterator[OrmSession]:
        sess = db_session_factory()
        try:
            yield sess
        finally:
            sess.close()

    app.dependency_overrides[get_session] = _override_get_session
    app.dependency_overrides[get_settings] = lambda: test_settings

    with TestClient(app) as test_client:
        yield test_client

    app.dependency_overrides.clear()
