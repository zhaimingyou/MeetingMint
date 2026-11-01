"""Integration tests for POST /api/auth/register (self-service signup).

Drives the public registration endpoint through a FastAPI TestClient with an
in-memory database. Covers:

- the registration gate (closed -> 403, open -> 201);
- field validation (empty / over-length -> 400);
- duplicate username -> 409;
- that a registered account is a non-admin, enabled, hashed, and usable to log
  in immediately (no approval step).

The default ``client`` fixture uses ``test_settings`` where
``allow_public_registration`` defaults to False, so the closed path is the
baseline. A separate fixture overrides ``get_settings`` with a settings instance
that opens registration.
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session as OrmSession
from sqlalchemy.orm import sessionmaker

from app.config import Settings, get_settings
from app.db import get_session
from app.main import create_app
from app.models import User

USERNAME = "newbie"
PASSWORD = "a-reasonable-password"


@pytest.fixture()
def open_client(
    test_settings: Settings,
    db_session_factory: sessionmaker[OrmSession],
) -> Iterator[TestClient]:
    """A TestClient with registration OPEN (allow_public_registration=True).

    Overrides ``get_settings`` with a settings instance identical to
    ``test_settings`` except the registration flag is enabled. The DB dependency
    points at the same in-memory engine as the default ``client`` fixture.
    """
    open_settings = test_settings.model_copy(
        update={"allow_public_registration": True}
    )
    app = create_app()

    def _override_get_session() -> Iterator[OrmSession]:
        sess = db_session_factory()
        try:
            yield sess
        finally:
            sess.close()

    app.dependency_overrides[get_session] = _override_get_session
    app.dependency_overrides[get_settings] = lambda: open_settings

    with TestClient(app) as test_client:
        yield test_client

    app.dependency_overrides.clear()


# --- gate: closed by default -> 403 ------------------------------------------


def test_register_closed_by_default_returns_403(client) -> None:
    resp = client.post(
        "/api/auth/register", json={"username": USERNAME, "password": PASSWORD}
    )
    assert resp.status_code == 403
    assert resp.json()["detail"]


def test_register_closed_does_not_create_user(
    client, db_session_factory: sessionmaker[OrmSession]
) -> None:
    client.post(
        "/api/auth/register", json={"username": USERNAME, "password": PASSWORD}
    )
    sess = db_session_factory()
    try:
        assert sess.execute(select(User)).scalars().all() == []
    finally:
        sess.close()


# --- open: successful registration -------------------------------------------


def test_register_open_creates_non_admin_enabled_user(
    open_client, db_session_factory: sessionmaker[OrmSession]
) -> None:
    resp = open_client.post(
        "/api/auth/register", json={"username": USERNAME, "password": PASSWORD}
    )
    assert resp.status_code == 201
    body = resp.json()
    assert body["username"] == USERNAME
    assert body["is_admin"] is False
    assert body["disabled"] is False
    assert body["id"]
    assert body["created_at"]

    # The row exists with a hashed password (never the plaintext).
    sess = db_session_factory()
    try:
        user = sess.execute(select(User)).scalar_one()
        assert user.username == USERNAME
        assert user.is_admin is False
        assert user.disabled is False
        assert user.password_hash  # bcrypt hash, not empty
        assert PASSWORD not in user.password_hash  # plaintext never stored
    finally:
        sess.close()


def test_registered_user_can_log_in_immediately(open_client) -> None:
    open_client.post(
        "/api/auth/register", json={"username": USERNAME, "password": PASSWORD}
    )
    login = open_client.post(
        "/api/auth/login", json={"username": USERNAME, "password": PASSWORD}
    )
    assert login.status_code == 200
    assert login.json()["token"]


def test_register_does_not_issue_session(
    open_client, db_session_factory: sessionmaker[OrmSession]
) -> None:
    from app.models import Session as SessionModel

    open_client.post(
        "/api/auth/register", json={"username": USERNAME, "password": PASSWORD}
    )
    sess = db_session_factory()
    try:
        assert sess.execute(select(SessionModel)).scalars().all() == []
    finally:
        sess.close()


# --- field validation (400) --------------------------------------------------


def test_register_empty_username_returns_400(open_client) -> None:
    resp = open_client.post(
        "/api/auth/register", json={"username": "", "password": PASSWORD}
    )
    assert resp.status_code == 400


def test_register_empty_password_returns_400(open_client) -> None:
    resp = open_client.post(
        "/api/auth/register", json={"username": USERNAME, "password": ""}
    )
    assert resp.status_code == 400


def test_register_over_length_username_returns_400(open_client) -> None:
    resp = open_client.post(
        "/api/auth/register",
        json={"username": "x" * 257, "password": PASSWORD},
    )
    assert resp.status_code == 400


def test_register_over_length_password_returns_400(open_client) -> None:
    resp = open_client.post(
        "/api/auth/register",
        json={"username": USERNAME, "password": "y" * 257},
    )
    assert resp.status_code == 400


# --- duplicate username (409) ------------------------------------------------


def test_register_duplicate_username_returns_409(open_client) -> None:
    first = open_client.post(
        "/api/auth/register", json={"username": USERNAME, "password": PASSWORD}
    )
    assert first.status_code == 201

    second = open_client.post(
        "/api/auth/register",
        json={"username": USERNAME, "password": "different-pw"},
    )
    assert second.status_code == 409
    assert second.json()["detail"]


def test_register_duplicate_does_not_overwrite_existing(
    open_client, db_session_factory: sessionmaker[OrmSession]
) -> None:
    open_client.post(
        "/api/auth/register", json={"username": USERNAME, "password": PASSWORD}
    )
    # Attempt to re-register with a different password.
    open_client.post(
        "/api/auth/register",
        json={"username": USERNAME, "password": "different-pw"},
    )

    # The original password still works (the row was not overwritten).
    login = open_client.post(
        "/api/auth/login", json={"username": USERNAME, "password": PASSWORD}
    )
    assert login.status_code == 200

    # And only one row exists.
    sess = db_session_factory()
    try:
        users = sess.execute(select(User).where(User.username == USERNAME)).scalars().all()
        assert len(users) == 1
    finally:
        sess.close()


# --- gate ordering: closed beats validation ----------------------------------


def test_register_closed_takes_precedence_over_400(client) -> None:
    # Even with empty fields, a closed instance returns 403 (gate is checked
    # first), never revealing whether the username is valid or taken.
    resp = client.post(
        "/api/auth/register", json={"username": "", "password": ""}
    )
    assert resp.status_code == 403


# --- runtime override: flip the switch without a restart ---------------------


def test_register_runtime_override_opens_registration(
    client, db_session_factory: sessionmaker[OrmSession]
) -> None:
    """A persisted runtime override flips the gate without a restart.

    Writes ``allow_public_registration = "true"`` directly into the
    ``runtime_config`` table (as the admin config API would), then confirms the
    same base ``client`` (flag False) now accepts registration because the
    endpoint reads the effective configuration.
    """
    from app.config_store import apply_changes

    sess = db_session_factory()
    try:
        apply_changes(sess, {"allow_public_registration": "true"})
        sess.commit()
    finally:
        sess.close()

    resp = client.post(
        "/api/auth/register", json={"username": USERNAME, "password": PASSWORD}
    )
    assert resp.status_code == 201
