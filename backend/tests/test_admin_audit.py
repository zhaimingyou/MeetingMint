"""Tests for the audit log writer + endpoint (admin-console, task 6).

Covers:
- Property 9: an Audit_Log_Entry exists iff the action committed (a rolled-back
  unit of work leaves no row), and entries carry no secret values.
- GET /api/admin/audit: newest-first ordering, bounded paging, and admin gating
  (401 unauthenticated, 403 non-admin).
"""

from __future__ import annotations

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session as OrmSession
from sqlalchemy.orm import sessionmaker

from app.audit import record_audit, serialize_entry
from app.db import make_engine, make_session_factory
from app.models import AuditLogEntry, User, create_all
from app.security.passwords import hash_password
from app.security.sessions import issue_session

PASSWORD = "correct-horse-battery-staple"


@pytest.fixture()
def sf() -> sessionmaker[OrmSession]:
    engine = make_engine("sqlite://")
    create_all(engine)
    factory = make_session_factory(engine)
    try:
        yield factory
    finally:
        engine.dispose()


def _admin(session: OrmSession) -> User:
    admin = User(username="root", password_hash=hash_password(PASSWORD), is_admin=True)
    session.add(admin)
    session.flush()
    return admin


# --- Property 9: entry exists iff committed ----------------------------------


def test_audit_entry_persists_on_commit(sf: sessionmaker[OrmSession]) -> None:
    session = sf()
    try:
        admin = _admin(session)
        record_audit(
            session,
            actor=admin,
            action="config.update",
            target_type="config",
            detail={"keys": ["llm_model_name"]},
        )
        session.commit()
    finally:
        session.close()

    session = sf()
    try:
        count = session.execute(
            select(func.count()).select_from(AuditLogEntry)
        ).scalar_one()
    finally:
        session.close()
    assert count == 1


def test_audit_entry_absent_after_rollback(sf: sessionmaker[OrmSession]) -> None:
    session = sf()
    try:
        admin = _admin(session)
        session.commit()  # commit the user so it survives the rollback below
        record_audit(session, actor=admin, action="user.delete", target_id="x")
        session.rollback()  # the action failed -> no "succeeded" audit row
    finally:
        session.close()

    session = sf()
    try:
        count = session.execute(
            select(func.count()).select_from(AuditLogEntry)
        ).scalar_one()
    finally:
        session.close()
    assert count == 0


def test_serialize_parses_detail_json(sf: sessionmaker[OrmSession]) -> None:
    session = sf()
    try:
        admin = _admin(session)
        entry = record_audit(
            session,
            actor=admin,
            action="config.update",
            detail={"keys": ["llm_api_key"], "llm_api_key": "set"},
        )
        session.commit()
        data = serialize_entry(entry)
    finally:
        session.close()
    assert data["action"] == "config.update"
    assert data["actor_username"] == "root"
    assert data["detail"] == {"keys": ["llm_api_key"], "llm_api_key": "set"}
    # The marker is "set", never the secret value.
    assert data["detail"]["llm_api_key"] == "set"


# --- GET /api/admin/audit (HTTP) ---------------------------------------------


def _seed_admin_token(db_session_factory) -> tuple[str, User]:
    s = db_session_factory()
    try:
        admin = User(
            username="admin", password_hash=hash_password(PASSWORD), is_admin=True
        )
        s.add(admin)
        s.commit()
        token = issue_session(s, admin.id, ttl_minutes=60).token
        s.commit()
        return token, admin
    finally:
        s.close()


def test_audit_endpoint_newest_first_and_paged(client, db_session_factory) -> None:
    token, _ = _seed_admin_token(db_session_factory)
    # Seed several audit entries.
    s = db_session_factory()
    try:
        admin = s.execute(select(User).where(User.username == "admin")).scalar_one()
        for i in range(5):
            record_audit(s, actor=admin, action=f"action.{i}", target_id=str(i))
        s.commit()
    finally:
        s.close()

    resp = client.get(
        "/api/admin/audit?page=1&page_size=2",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["total"] == 5
    assert body["page_size"] == 2
    assert len(body["items"]) == 2
    # Newest-first: the last-created action appears before earlier ones.
    assert body["items"][0]["action"] == "action.4"


def test_audit_endpoint_clamps_page_size(client, db_session_factory) -> None:
    token, _ = _seed_admin_token(db_session_factory)
    resp = client.get(
        "/api/admin/audit?page_size=100000",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 200
    assert resp.json()["page_size"] == 200  # clamped to MAX_PAGE_SIZE


def test_audit_endpoint_requires_admin(client, db_session_factory) -> None:
    # Non-admin user.
    s = db_session_factory()
    try:
        bob = User(username="bob", password_hash=hash_password(PASSWORD))
        s.add(bob)
        s.commit()
        bob_token = issue_session(s, bob.id, ttl_minutes=60).token
        s.commit()
    finally:
        s.close()
    resp = client.get(
        "/api/admin/audit", headers={"Authorization": f"Bearer {bob_token}"}
    )
    assert resp.status_code == 403


def test_audit_endpoint_requires_auth(client) -> None:
    resp = client.get("/api/admin/audit")
    assert resp.status_code == 401
