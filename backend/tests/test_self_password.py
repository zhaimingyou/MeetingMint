"""Tests for self-service password change (admin-console, task 9).

Covers Req 8: wrong current password is rejected (hash unchanged), a weak/empty
new password is rejected, and a successful change keeps the requesting session
valid while invalidating the user's other sessions (Property 8) and switching
the effective credential.
"""

from __future__ import annotations

import pytest
from sqlalchemy.orm import Session as OrmSession

from app.security.passwords import verify_password
from app.security.sessions import issue_session, validate_session
from app.models import User

PASSWORD = "correct-horse-battery-staple"


def _seed_user_with_two_sessions(db_session_factory) -> tuple[str, str, str]:
    """Create a user with two active sessions; return (user_id, tokenA, tokenB)."""
    from app.security.passwords import hash_password

    s = db_session_factory()
    try:
        u = User(username="bob", password_hash=hash_password(PASSWORD))
        s.add(u)
        s.commit()
        token_a = issue_session(s, u.id, ttl_minutes=60).token
        token_b = issue_session(s, u.id, ttl_minutes=60).token
        s.commit()
        return u.id, token_a, token_b
    finally:
        s.close()


def _valid(db_session_factory, token: str) -> bool:
    s = db_session_factory()
    try:
        return validate_session(s, token) is not None
    finally:
        s.close()


def _hash(db_session_factory, user_id: str) -> str:
    s = db_session_factory()
    try:
        return s.get(User, user_id).password_hash
    finally:
        s.close()


def test_change_password_success_keeps_caller_signs_out_others(
    client, db_session_factory
) -> None:
    user_id, token_a, token_b = _seed_user_with_two_sessions(db_session_factory)
    resp = client.post(
        "/api/auth/password",
        headers={"Authorization": f"Bearer {token_a}"},
        json={"current_password": PASSWORD, "new_password": "a-new-strong-pass"},
    )
    assert resp.status_code == 200
    # The requesting session stays valid; the other device is signed out.
    assert _valid(db_session_factory, token_a) is True
    assert _valid(db_session_factory, token_b) is False
    # The new password now authenticates; the old one does not.
    assert (
        client.post(
            "/api/auth/login", json={"username": "bob", "password": "a-new-strong-pass"}
        ).status_code
        == 200
    )
    assert (
        client.post(
            "/api/auth/login", json={"username": "bob", "password": PASSWORD}
        ).status_code
        == 401
    )


def test_wrong_current_password_rejected_hash_unchanged(
    client, db_session_factory
) -> None:
    user_id, token_a, _ = _seed_user_with_two_sessions(db_session_factory)
    before = _hash(db_session_factory, user_id)
    resp = client.post(
        "/api/auth/password",
        headers={"Authorization": f"Bearer {token_a}"},
        json={"current_password": "wrong", "new_password": "a-new-strong-pass"},
    )
    assert resp.status_code == 401
    assert _hash(db_session_factory, user_id) == before


def test_weak_new_password_rejected_hash_unchanged(client, db_session_factory) -> None:
    user_id, token_a, _ = _seed_user_with_two_sessions(db_session_factory)
    before = _hash(db_session_factory, user_id)
    resp = client.post(
        "/api/auth/password",
        headers={"Authorization": f"Bearer {token_a}"},
        json={"current_password": PASSWORD, "new_password": ""},
    )
    assert resp.status_code == 400
    assert _hash(db_session_factory, user_id) == before


def test_change_password_requires_auth(client) -> None:
    resp = client.post(
        "/api/auth/password",
        json={"current_password": PASSWORD, "new_password": "x"},
    )
    assert resp.status_code == 401
