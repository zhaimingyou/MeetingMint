"""Authorization layer tests (admin-console, task 5).

Covers the admin gate (Property 1, in part): a route guarded by ``AdminUser``
rejects an absent/invalid token with a no-content 401 and an authenticated
non-administrator with a 403, and admits an administrator. Also covers the
``/api/auth/me`` identity endpoint and ``is_admin`` on the login response.

(The exhaustive "every /api/admin/* route is gated" sweep is added in task 12
once all admin routers are registered.)
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.deps import (
    AdminUser,
    AuthenticationError,
    AuthorizationError,
    authentication_exception_handler,
    authorization_exception_handler,
)
from app.db import get_session, make_engine, make_session_factory
from app.models import User, create_all
from app.security.passwords import hash_password
from app.security.sessions import issue_session

PASSWORD = "correct-horse-battery-staple"


@pytest.fixture()
def admin_app() -> Iterator[tuple[TestClient, str, str]]:
    """An app with a single AdminUser-guarded route + seeded admin/non-admin."""
    engine = make_engine("sqlite://")
    create_all(engine)
    sf = make_session_factory(engine)

    s = sf()
    try:
        admin = User(
            username="admin", password_hash=hash_password(PASSWORD), is_admin=True
        )
        bob = User(username="bob", password_hash=hash_password(PASSWORD), is_admin=False)
        s.add_all([admin, bob])
        s.commit()
        admin_token = issue_session(s, admin.id, ttl_minutes=60).token
        bob_token = issue_session(s, bob.id, ttl_minutes=60).token
        s.commit()
    finally:
        s.close()

    app = FastAPI()
    app.add_exception_handler(AuthenticationError, authentication_exception_handler)
    app.add_exception_handler(AuthorizationError, authorization_exception_handler)

    @app.get("/admin-only")
    def admin_only(user: AdminUser) -> dict:
        return {"ok": True, "username": user.username}

    def _override_get_session() -> Iterator:
        sess = sf()
        try:
            yield sess
        finally:
            sess.close()

    app.dependency_overrides[get_session] = _override_get_session

    try:
        with TestClient(app) as client:
            yield client, admin_token, bob_token
    finally:
        app.dependency_overrides.clear()
        engine.dispose()


def test_admin_route_rejects_missing_token_with_no_content_401(
    admin_app: tuple[TestClient, str, str],
) -> None:
    client, _, _ = admin_app
    resp = client.get("/admin-only")
    assert resp.status_code == 401
    assert resp.content == b""  # no body


def test_admin_route_rejects_invalid_token_401(
    admin_app: tuple[TestClient, str, str],
) -> None:
    client, _, _ = admin_app
    resp = client.get("/admin-only", headers={"Authorization": "Bearer nope"})
    assert resp.status_code == 401


def test_admin_route_forbids_non_admin_403(
    admin_app: tuple[TestClient, str, str],
) -> None:
    client, _, bob_token = admin_app
    resp = client.get("/admin-only", headers={"Authorization": f"Bearer {bob_token}"})
    assert resp.status_code == 403
    assert resp.json() == {"detail": "administrator privilege required"}


def test_admin_route_admits_admin_200(
    admin_app: tuple[TestClient, str, str],
) -> None:
    client, admin_token, _ = admin_app
    resp = client.get(
        "/admin-only", headers={"Authorization": f"Bearer {admin_token}"}
    )
    assert resp.status_code == 200
    assert resp.json() == {"ok": True, "username": "admin"}


# --- /api/auth/me + login is_admin (use the shared app via the client fixture) #


def _seed_user(db_session_factory, username: str, *, is_admin: bool) -> str:
    s = db_session_factory()
    try:
        u = User(
            username=username,
            password_hash=hash_password(PASSWORD),
            is_admin=is_admin,
        )
        s.add(u)
        s.commit()
        token = issue_session(s, u.id, ttl_minutes=60).token
        s.commit()
        return token
    finally:
        s.close()


def test_login_response_includes_is_admin(client, db_session_factory) -> None:
    _seed_user(db_session_factory, "admin", is_admin=True)
    resp = client.post(
        "/api/auth/login", json={"username": "admin", "password": PASSWORD}
    )
    assert resp.status_code == 200
    assert resp.json()["is_admin"] is True


def test_me_returns_identity_and_role(client, db_session_factory) -> None:
    token = _seed_user(db_session_factory, "bob", is_admin=False)
    resp = client.get("/api/auth/me", headers={"Authorization": f"Bearer {token}"})
    assert resp.status_code == 200
    assert resp.json() == {"username": "bob", "is_admin": False}


def test_me_requires_authentication(client) -> None:
    resp = client.get("/api/auth/me")
    assert resp.status_code == 401
    assert resp.content == b""


# --- Property 1: ALL /api/admin/* routes are gated (route-table sweep) --------


def test_property_1_all_admin_routes_reject_unauth_and_nonadmin(
    client, db_session_factory
) -> None:
    """Every registered /api/admin/* route returns 401 (no token) and 403 (non-admin).

    Drives the live route table so any admin route added later is covered
    automatically. The auth dependency runs during dependency resolution and
    raises before body validation, so a missing/empty body never preempts the
    401/403 (an empty JSON body is sent for write methods regardless).
    """
    # Seed a non-admin account + token.
    s = db_session_factory()
    try:
        bob = User(username="bob", password_hash=hash_password(PASSWORD))
        s.add(bob)
        s.commit()
        bob_token = issue_session(s, bob.id, ttl_minutes=60).token
        s.commit()
    finally:
        s.close()

    from app.main import create_app

    app = create_app()
    checked = 0
    for path, operations in app.openapi()["paths"].items():
        if not path.startswith("/api/admin"):
            continue
        concrete = path.replace("{user_id}", "u").replace("{meeting_id}", "m")
        for method_name in operations:
            method = method_name.upper()
            if method not in {"GET", "POST", "PUT", "PATCH", "DELETE"}:
                continue
            checked += 1
            kwargs: dict = (
                {"json": {}} if method in {"POST", "PUT", "PATCH"} else {}
            )

            # No token -> 401 (no content).
            r = client.request(method, concrete, **kwargs)
            assert r.status_code == 401, (method, concrete, r.status_code)

            # Authenticated non-admin -> 403.
            r = client.request(
                method,
                concrete,
                headers={"Authorization": f"Bearer {bob_token}"},
                **kwargs,
            )
            assert r.status_code == 403, (method, concrete, r.status_code)

    # Sanity: the sweep actually exercised the admin surface.
    assert checked >= 8
