"""Integration tests for the protected-route auth dependency (task 3.5).

Exercises :func:`app.api.deps.get_current_user` / :data:`app.api.deps.CurrentUser`
end to end by attaching a **test-only** probe route (declared here, not in the
app) that depends on the current user. Covers Requirement 1.1 and design
Property 3 ("Protected endpoints reject unauthenticated access") at the
example/integration level -- the Property 3 hypothesis test is task 3.6.

The probe route returns the authenticated user's id/username on success so we
can assert the route sees the correct user, and the no-content 401 path is
verified to carry an empty body.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import timedelta

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session as OrmSession
from sqlalchemy.orm import sessionmaker

from app.api.deps import CurrentUser
from app.config import Settings, get_settings
from app.db import get_session
from app.main import create_app
from app.models import Session as SessionModel
from app.models import User, utcnow
from app.security.passwords import hash_password
from app.security.sessions import issue_session

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


@pytest.fixture()
def probe_client(
    test_settings: Settings,
    db_session_factory: sessionmaker[OrmSession],
) -> Iterator[TestClient]:
    """A TestClient over the real app plus a temporary protected probe route.

    The probe route is defined here (not shipped in the app) and depends on
    ``CurrentUser`` so the auth dependency is exercised exactly as a real
    protected router would use it. DB and settings dependencies are overridden
    to the in-memory test engine / settings, mirroring conftest's ``client``.
    """
    app: FastAPI = create_app()

    @app.get("/api/_probe")
    def _probe(user: CurrentUser) -> dict[str, str]:  # pragma: no cover - body trivial
        return {"id": user.id, "username": user.username}

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


def _issue_token_for(
    db_session_factory: sessionmaker[OrmSession], user_id: str
) -> str:
    """Issue and commit a valid session, returning its opaque token."""
    sess = db_session_factory()
    try:
        # Pass an explicit TTL so the helper does not fall back to env-based
        # get_settings() (which has no env in the test process).
        session = issue_session(sess, user_id, ttl_minutes=60)
        sess.commit()
        return session.token
    finally:
        sess.close()


# --- success path -----------------------------------------------------------


def test_valid_token_returns_200_and_correct_user(
    probe_client: TestClient,
    seeded_user: User,
    db_session_factory: sessionmaker[OrmSession],
) -> None:
    token = _issue_token_for(db_session_factory, seeded_user.id)
    resp = probe_client.get(
        "/api/_probe", headers={"Authorization": f"Bearer {token}"}
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["id"] == seeded_user.id
    assert body["username"] == USERNAME


def test_valid_token_lowercase_scheme_is_accepted(
    probe_client: TestClient,
    seeded_user: User,
    db_session_factory: sessionmaker[OrmSession],
) -> None:
    token = _issue_token_for(db_session_factory, seeded_user.id)
    resp = probe_client.get(
        "/api/_probe", headers={"Authorization": f"bearer {token}"}
    )
    assert resp.status_code == 200
    assert resp.json()["username"] == USERNAME


# --- rejection paths: 401 with no content (Req 1.1, Property 3) --------------


def test_missing_header_returns_401_no_content(probe_client: TestClient) -> None:
    resp = probe_client.get("/api/_probe")
    assert resp.status_code == 401
    assert resp.content == b""


@pytest.mark.parametrize(
    "header",
    ["Token abc", "Bearer", "Bearer ", "Bearer    ", "Basic abc", "garbage"],
)
def test_malformed_header_returns_401_no_content(
    probe_client: TestClient, header: str
) -> None:
    resp = probe_client.get("/api/_probe", headers={"Authorization": header})
    assert resp.status_code == 401
    assert resp.content == b""


def test_unknown_token_returns_401_no_content(probe_client: TestClient) -> None:
    resp = probe_client.get(
        "/api/_probe", headers={"Authorization": "Bearer not-a-real-token"}
    )
    assert resp.status_code == 401
    assert resp.content == b""


def test_expired_token_returns_401(
    probe_client: TestClient,
    seeded_user: User,
    db_session_factory: sessionmaker[OrmSession],
) -> None:
    # Seed a session whose expiry is already in the past.
    sess = db_session_factory()
    try:
        past = utcnow() - timedelta(minutes=120)
        expired = SessionModel(
            token="expired-token",
            user_id=seeded_user.id,
            issued_at=past,
            expires_at=past + timedelta(minutes=60),
            invalidated=False,
        )
        sess.add(expired)
        sess.commit()
    finally:
        sess.close()

    resp = probe_client.get(
        "/api/_probe", headers={"Authorization": "Bearer expired-token"}
    )
    assert resp.status_code == 401
    assert resp.content == b""


def test_invalidated_token_after_logout_returns_401(
    probe_client: TestClient,
    seeded_user: User,
    db_session_factory: sessionmaker[OrmSession],
) -> None:
    token = _issue_token_for(db_session_factory, seeded_user.id)

    # Sanity: token works before logout.
    assert (
        probe_client.get(
            "/api/_probe", headers={"Authorization": f"Bearer {token}"}
        ).status_code
        == 200
    )

    # Invalidate via the real logout endpoint.
    logout = probe_client.post(
        "/api/auth/logout", headers={"Authorization": f"Bearer {token}"}
    )
    assert logout.status_code in (200, 204)

    resp = probe_client.get(
        "/api/_probe", headers={"Authorization": f"Bearer {token}"}
    )
    assert resp.status_code == 401
    assert resp.content == b""
