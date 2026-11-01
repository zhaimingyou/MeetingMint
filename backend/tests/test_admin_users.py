"""Tests for the user management API (admin-console, task 8).

Covers create/list/update/delete/password-reset, the at-least-one-admin
invariant (Property 7), session revocation on disable/delete/reset/revoke-admin
(Property 8), duplicate-username 409, disabled-login rejection (Req 7.9), and
admin gating.
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session as OrmSession
from sqlalchemy.orm import sessionmaker

from app.config import Settings, get_settings
from app.db import get_session, make_engine, make_session_factory
from app.main import create_app
from app.models import MeetingRecord, ProcessingJob, Session as SessionModel, User, create_all
from app.enums import JobStatus
from app.security.passwords import hash_password
from app.security.sessions import issue_session, validate_session

PASSWORD = "correct-horse-battery-staple"


def _settings() -> Settings:
    return Settings(
        bootstrap_admin_username="admin",
        bootstrap_admin_password="pw",
        redis_url="redis://localhost:6379/0",
        database_url="sqlite://",
        max_credential_length=256,
    )  # type: ignore[call-arg]


@pytest.fixture()
def harness() -> Iterator[tuple[TestClient, sessionmaker[OrmSession], dict]]:
    engine = make_engine("sqlite://")
    create_all(engine)
    sf = make_session_factory(engine)

    ids: dict = {}
    s = sf()
    try:
        admin = User(username="admin", password_hash=hash_password(PASSWORD), is_admin=True)
        admin2 = User(username="admin2", password_hash=hash_password(PASSWORD), is_admin=True)
        bob = User(username="bob", password_hash=hash_password(PASSWORD), is_admin=False)
        s.add_all([admin, admin2, bob])
        s.commit()
        ids["admin"] = admin.id
        ids["admin2"] = admin2.id
        ids["bob"] = bob.id
        ids["admin_token"] = issue_session(s, admin.id, ttl_minutes=60).token
        ids["bob_token"] = issue_session(s, bob.id, ttl_minutes=60).token
        s.commit()
    finally:
        s.close()

    base = _settings()
    app = create_app()

    def _override_get_session() -> Iterator[OrmSession]:
        sess = sf()
        try:
            yield sess
        finally:
            sess.close()

    app.dependency_overrides[get_session] = _override_get_session
    app.dependency_overrides[get_settings] = lambda: base
    try:
        with TestClient(app) as client:
            yield client, sf, ids
    finally:
        app.dependency_overrides.clear()
        engine.dispose()


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _has_valid_session(sf, token: str) -> bool:
    s = sf()
    try:
        return validate_session(s, token) is not None
    finally:
        s.close()


# --- list / create -----------------------------------------------------------


def test_list_users_excludes_password_hash(harness) -> None:
    client, _, ids = harness
    resp = client.get("/api/admin/users", headers=_auth(ids["admin_token"]))
    assert resp.status_code == 200
    rows = resp.json()
    assert {r["username"] for r in rows} == {"admin", "admin2", "bob"}
    assert all("password_hash" not in r for r in rows)


def test_create_user_then_can_login(harness) -> None:
    client, _, ids = harness
    resp = client.post(
        "/api/admin/users",
        headers=_auth(ids["admin_token"]),
        json={"username": "carol", "password": "carol-pass-123", "is_admin": False},
    )
    assert resp.status_code == 201
    assert resp.json()["username"] == "carol"
    login = client.post(
        "/api/auth/login", json={"username": "carol", "password": "carol-pass-123"}
    )
    assert login.status_code == 200


def test_create_duplicate_username_conflicts(harness) -> None:
    client, _, ids = harness
    resp = client.post(
        "/api/admin/users",
        headers=_auth(ids["admin_token"]),
        json={"username": "bob", "password": "another-pass"},
    )
    assert resp.status_code == 409


def test_create_rejects_empty_password(harness) -> None:
    client, _, ids = harness
    resp = client.post(
        "/api/admin/users",
        headers=_auth(ids["admin_token"]),
        json={"username": "dave", "password": ""},
    )
    assert resp.status_code == 400


# --- disable / sessions (Property 8) + disabled login (Req 7.9) --------------


def test_disable_user_invalidates_sessions_and_blocks_login(harness) -> None:
    client, sf, ids = harness
    assert _has_valid_session(sf, ids["bob_token"]) is True
    resp = client.put(
        f"/api/admin/users/{ids['bob']}",
        headers=_auth(ids["admin_token"]),
        json={"disabled": True},
    )
    assert resp.status_code == 200
    assert resp.json()["disabled"] is True
    # Bob's existing session is now invalid.
    assert _has_valid_session(sf, ids["bob_token"]) is False
    # And a fresh login is rejected like invalid credentials.
    login = client.post(
        "/api/auth/login", json={"username": "bob", "password": PASSWORD}
    )
    assert login.status_code == 401


def test_password_reset_invalidates_sessions(harness) -> None:
    client, sf, ids = harness
    assert _has_valid_session(sf, ids["bob_token"]) is True
    resp = client.post(
        f"/api/admin/users/{ids['bob']}/password",
        headers=_auth(ids["admin_token"]),
        json={"password": "brand-new-pass"},
    )
    assert resp.status_code == 200
    assert _has_valid_session(sf, ids["bob_token"]) is False
    # The new password works.
    login = client.post(
        "/api/auth/login", json={"username": "bob", "password": "brand-new-pass"}
    )
    assert login.status_code == 200


def test_revoke_admin_invalidates_sessions(harness) -> None:
    client, sf, ids = harness
    admin2_token = None
    s = sf()
    try:
        admin2_token = issue_session(s, ids["admin2"], ttl_minutes=60).token
        s.commit()
    finally:
        s.close()
    assert _has_valid_session(sf, admin2_token) is True
    resp = client.put(
        f"/api/admin/users/{ids['admin2']}",
        headers=_auth(ids["admin_token"]),
        json={"is_admin": False},
    )
    assert resp.status_code == 200
    assert _has_valid_session(sf, admin2_token) is False


# --- at-least-one-admin invariant (Property 7) -------------------------------


def test_cannot_demote_last_admin(harness) -> None:
    client, sf, ids = harness
    # Demote admin2 first (admin remains the only enabled admin).
    client.put(
        f"/api/admin/users/{ids['admin2']}",
        headers=_auth(ids["admin_token"]),
        json={"is_admin": False},
    )
    # Now demoting the last admin must be rejected.
    resp = client.put(
        f"/api/admin/users/{ids['admin']}",
        headers=_auth(ids["admin_token"]),
        json={"is_admin": False},
    )
    assert resp.status_code == 409
    # The admin is still an admin (change rolled back).
    s = sf()
    try:
        assert s.get(User, ids["admin"]).is_admin is True
    finally:
        s.close()


def test_cannot_disable_last_admin(harness) -> None:
    client, _, ids = harness
    client.put(
        f"/api/admin/users/{ids['admin2']}",
        headers=_auth(ids["admin_token"]),
        json={"disabled": True},
    )
    resp = client.put(
        f"/api/admin/users/{ids['admin']}",
        headers=_auth(ids["admin_token"]),
        json={"disabled": True},
    )
    assert resp.status_code == 409


def test_cannot_delete_last_admin(harness) -> None:
    client, sf, ids = harness
    client.delete(
        f"/api/admin/users/{ids['admin2']}", headers=_auth(ids["admin_token"])
    )
    resp = client.delete(
        f"/api/admin/users/{ids['admin']}", headers=_auth(ids["admin_token"])
    )
    assert resp.status_code == 409
    s = sf()
    try:
        assert s.get(User, ids["admin"]) is not None
    finally:
        s.close()


# --- delete cascades + self-delete -------------------------------------------


def test_delete_user_cascades_meetings(harness) -> None:
    client, sf, ids = harness
    # Give bob a meeting + job.
    s = sf()
    try:
        m = MeetingRecord(
            owner_id=ids["bob"],
            title="t",
            source_audio_path="/p.webm",
            source_format="webm",
        )
        s.add(m)
        s.flush()
        s.add(ProcessingJob(meeting_id=m.id, status=JobStatus.COMPLETED.value))
        s.commit()
        meeting_id = m.id
    finally:
        s.close()

    resp = client.delete(
        f"/api/admin/users/{ids['bob']}", headers=_auth(ids["admin_token"])
    )
    assert resp.status_code == 200
    s = sf()
    try:
        assert s.get(User, ids["bob"]) is None
        assert s.get(MeetingRecord, meeting_id) is None  # cascaded
    finally:
        s.close()


def test_admin_can_delete_self_when_another_admin_exists(harness) -> None:
    client, sf, ids = harness
    # admin deletes their own account; admin2 remains -> allowed, audit actor
    # FK is set null by the cascade.
    resp = client.delete(
        f"/api/admin/users/{ids['admin']}", headers=_auth(ids["admin_token"])
    )
    assert resp.status_code == 200
    s = sf()
    try:
        assert s.get(User, ids["admin"]) is None
        # The audit row survives with a null actor (username snapshot kept).
        from app.models import AuditLogEntry

        entry = s.execute(
            select(AuditLogEntry).where(AuditLogEntry.action == "user.delete")
        ).scalar_one()
        assert entry.actor_user_id is None
        assert entry.actor_username == "admin"
    finally:
        s.close()


# --- 404 + authz -------------------------------------------------------------


def test_update_unknown_user_404(harness) -> None:
    client, _, ids = harness
    resp = client.put(
        "/api/admin/users/no-such-id",
        headers=_auth(ids["admin_token"]),
        json={"disabled": True},
    )
    assert resp.status_code == 404


def test_user_routes_require_admin(harness) -> None:
    client, _, ids = harness
    assert client.get("/api/admin/users", headers=_auth(ids["bob_token"])).status_code == 403
    assert (
        client.post(
            "/api/admin/users",
            headers=_auth(ids["bob_token"]),
            json={"username": "x", "password": "y"},
        ).status_code
        == 403
    )


def test_user_routes_require_auth(harness) -> None:
    client, _, _ = harness
    assert client.get("/api/admin/users").status_code == 401
