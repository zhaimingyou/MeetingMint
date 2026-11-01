"""Integration tests for the auth API router (task 3.4).

Drives ``POST /api/auth/login`` and ``POST /api/auth/logout`` through a FastAPI
TestClient with an in-memory database and deterministic test settings (see
``conftest.py``). Covers Requirements 1.2 (valid -> 200 + token), 1.3 (invalid
-> 401), 1.4 (empty/over-256 field -> 400, precedence over 401), and 1.5 (logout
invalidates; idempotent).

The Property tests for login/auth are task 3.6; these are example/integration
tests.
"""

from __future__ import annotations

import pytest
from sqlalchemy.orm import Session as OrmSession
from sqlalchemy.orm import sessionmaker

from app.models import User
from app.security.passwords import hash_password
from app.security.sessions import validate_session

USERNAME = "alice"
PASSWORD = "correct-horse-battery-staple"


@pytest.fixture()
def seeded_user(db_session_factory: sessionmaker[OrmSession]) -> User:
    """Insert a known user with a hashed password into the test DB."""
    sess = db_session_factory()
    try:
        user = User(username=USERNAME, password_hash=hash_password(PASSWORD))
        sess.add(user)
        sess.commit()
        sess.refresh(user)
        return user
    finally:
        sess.close()


# --- login: valid credentials (Req 1.2) -------------------------------------


def test_login_valid_returns_200_and_valid_token(
    client, seeded_user: User, db_session_factory: sessionmaker[OrmSession]
) -> None:
    resp = client.post(
        "/api/auth/login", json={"username": USERNAME, "password": PASSWORD}
    )
    assert resp.status_code == 200
    body = resp.json()
    token = body["token"]
    assert token
    assert body["token_type"] == "bearer"

    # The issued token validates against the same database.
    sess = db_session_factory()
    try:
        user = validate_session(sess, token)
        assert user is not None
        assert user.username == USERNAME
    finally:
        sess.close()


# --- login: invalid credentials (Req 1.3) -----------------------------------


def test_login_wrong_password_returns_401_no_token(
    client, seeded_user: User
) -> None:
    resp = client.post(
        "/api/auth/login", json={"username": USERNAME, "password": "wrong"}
    )
    assert resp.status_code == 401
    assert "token" not in resp.json()


def test_login_unknown_user_returns_401_no_token(client) -> None:
    resp = client.post(
        "/api/auth/login", json={"username": "nobody", "password": PASSWORD}
    )
    assert resp.status_code == 401
    assert "token" not in resp.json()


def test_login_unknown_user_creates_no_session(
    client, db_session_factory: sessionmaker[OrmSession]
) -> None:
    client.post(
        "/api/auth/login", json={"username": "ghost", "password": "whatever"}
    )
    from sqlalchemy import select

    from app.models import Session as SessionModel

    sess = db_session_factory()
    try:
        rows = sess.execute(select(SessionModel)).scalars().all()
        assert rows == []
    finally:
        sess.close()


# --- login: out-of-bounds fields (Req 1.4) ----------------------------------


def test_login_empty_username_returns_400(client) -> None:
    resp = client.post(
        "/api/auth/login", json={"username": "", "password": PASSWORD}
    )
    assert resp.status_code == 400


def test_login_empty_password_returns_400(client) -> None:
    resp = client.post(
        "/api/auth/login", json={"username": USERNAME, "password": ""}
    )
    assert resp.status_code == 400


def test_login_username_over_256_returns_400(client) -> None:
    resp = client.post(
        "/api/auth/login",
        json={"username": "x" * 257, "password": PASSWORD},
    )
    assert resp.status_code == 400


def test_login_password_over_256_returns_400(client) -> None:
    resp = client.post(
        "/api/auth/login",
        json={"username": USERNAME, "password": "y" * 257},
    )
    assert resp.status_code == 400


def test_login_at_256_boundary_is_not_rejected_for_length(client) -> None:
    # Exactly 256 chars is allowed (not out of bounds); unknown user -> 401, not 400.
    resp = client.post(
        "/api/auth/login",
        json={"username": "z" * 256, "password": "p" * 256},
    )
    assert resp.status_code == 401


def test_login_400_takes_precedence_over_401(client) -> None:
    # Empty field on a non-existent user must still be 400 (malformed beats auth).
    resp = client.post(
        "/api/auth/login", json={"username": "", "password": ""}
    )
    assert resp.status_code == 400


# --- logout (Req 1.5) --------------------------------------------------------


def test_logout_invalidates_token(
    client, seeded_user: User, db_session_factory: sessionmaker[OrmSession]
) -> None:
    login = client.post(
        "/api/auth/login", json={"username": USERNAME, "password": PASSWORD}
    )
    token = login.json()["token"]

    # Token is valid before logout.
    sess = db_session_factory()
    try:
        assert validate_session(sess, token) is not None
    finally:
        sess.close()

    resp = client.post(
        "/api/auth/logout", headers={"Authorization": f"Bearer {token}"}
    )
    assert resp.status_code in (200, 204)

    # After logout the token no longer validates.
    sess = db_session_factory()
    try:
        assert validate_session(sess, token) is None
    finally:
        sess.close()


def test_logout_unknown_token_is_idempotent(client) -> None:
    resp = client.post(
        "/api/auth/logout", headers={"Authorization": "Bearer garbage-token"}
    )
    assert resp.status_code in (200, 204)


def test_logout_without_authorization_header_is_idempotent(client) -> None:
    resp = client.post("/api/auth/logout")
    assert resp.status_code in (200, 204)


def test_logout_malformed_header_is_idempotent(client) -> None:
    resp = client.post(
        "/api/auth/logout", headers={"Authorization": "NotBearer xyz"}
    )
    assert resp.status_code in (200, 204)
