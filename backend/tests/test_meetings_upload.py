"""Tests for the upload endpoint -- task 8.1 (streaming + size/empty checks).

Drives ``POST /api/meetings`` through a FastAPI TestClient. These are example /
integration tests for the task-8.1 slice: streaming the body to a temp file
while counting bytes, 413 on exceeding ``max_upload_bytes`` (without trusting
``Content-Length``), 400 on a 0-byte upload, 401 when unauthenticated, and the
no-leftover-temp-file guarantee. (Property tests for upload -- Properties 5/6/7
-- are task 8.5.)

The shared ``conftest`` ``client`` fixture pins a 500 MB default upload limit
and a ``/data`` audio dir, neither of which suits these tests. So this module
builds its own TestClient over ``create_app()`` with settings overridden to a
small ``max_upload_bytes`` and an ``audio_storage_dir`` rooted at a ``tmp_path``,
and the ``get_session`` dependency pointed at an in-memory engine.
"""

from __future__ import annotations

import io
import os
from collections.abc import Iterator

import pytest
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session as OrmSession
from sqlalchemy.orm import sessionmaker

from app.config import Settings, get_settings
from app.db import get_session, make_engine, make_session_factory
from app.main import create_app
from app.models import User, create_all
from app.security.passwords import hash_password
from app.security.sessions import issue_session

# A small, test-friendly upload cap so 413 is reachable without huge bodies.
MAX_UPLOAD_BYTES = 4 * 1024  # 4 KiB

USERNAME = "uploader"
PASSWORD = "correct-horse-battery-staple"


@pytest.fixture()
def audio_dir(tmp_path) -> str:
    """An audio storage directory under tmp_path (never touches /data)."""
    d = tmp_path / "audio"
    d.mkdir()
    return str(d)


@pytest.fixture()
def upload_settings(audio_dir: str) -> Settings:
    """Settings with a tiny upload cap and a tmp audio dir."""
    return Settings(
        bootstrap_admin_username="admin",
        bootstrap_admin_password="bootstrap-pass",
        redis_url="redis://localhost:6379/0",
        database_url="sqlite://",
        max_upload_bytes=MAX_UPLOAD_BYTES,
        audio_storage_dir=audio_dir,
    )  # type: ignore[call-arg]


@pytest.fixture()
def engine() -> Iterator[Engine]:
    eng = make_engine("sqlite://")
    create_all(eng)
    try:
        yield eng
    finally:
        eng.dispose()


@pytest.fixture()
def session_factory(engine: Engine) -> sessionmaker[OrmSession]:
    return make_session_factory(engine)


@pytest.fixture()
def auth_token(session_factory: sessionmaker[OrmSession]) -> str:
    """Seed a user and return a valid bearer token for them."""
    sess = session_factory()
    try:
        user = User(username=USERNAME, password_hash=hash_password(PASSWORD))
        sess.add(user)
        sess.commit()
        sess.refresh(user)
        session = issue_session(sess, user.id, ttl_minutes=60)
        sess.commit()
        return session.token
    finally:
        sess.close()


@pytest.fixture()
def client(
    upload_settings: Settings,
    session_factory: sessionmaker[OrmSession],
    monkeypatch: pytest.MonkeyPatch,
) -> Iterator["TestClient"]:  # noqa: F821
    from fastapi.testclient import TestClient

    # Task 8.2 added content-based ffprobe format detection to the finalize
    # seam. These 8.1 tests exercise the size/empty/auth slice with synthetic
    # byte blobs (not real audio) and run where ffprobe may be absent, so pin
    # the injectable detector to a supported format. The 413/400/401 paths
    # short-circuit before detection and are unaffected; the valid-upload paths
    # then reach the provisional 201 as task 8.1 intended.
    monkeypatch.setattr("app.api.meetings.detect_format", lambda _path: "wav")

    # No broker runs under test: stub the post-commit enqueue so the 201 path
    # does not attempt a real Celery/Redis connection (enqueue is covered by
    # tests/test_meetings_enqueue.py).
    monkeypatch.setattr("app.api.meetings._enqueue_processing_job", lambda _id: None)

    app = create_app()

    def _override_get_session() -> Iterator[OrmSession]:
        sess = session_factory()
        try:
            yield sess
        finally:
            sess.close()

    app.dependency_overrides[get_session] = _override_get_session
    app.dependency_overrides[get_settings] = lambda: upload_settings

    with TestClient(app) as test_client:
        yield test_client

    app.dependency_overrides.clear()


def _temp_files(audio_dir: str) -> list[str]:
    """List any leftover temp upload files under the audio dir's tmp area."""
    tmp = os.path.join(audio_dir, "tmp")
    if not os.path.isdir(tmp):
        return []
    return [os.path.join(tmp, n) for n in os.listdir(tmp)]


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


# --- 413: exceeds max_upload_bytes (Req 3.3) --------------------------------


def test_upload_over_limit_returns_413(client, auth_token: str, audio_dir: str) -> None:
    body = b"x" * (MAX_UPLOAD_BYTES + 1)
    resp = client.post(
        "/api/meetings",
        headers=_auth(auth_token),
        files={"file": ("big.wav", io.BytesIO(body), "audio/wav")},
    )
    assert resp.status_code == 413
    # Message indicates the maximum allowed size (Req 3.3).
    detail = resp.json()["detail"]
    assert str(MAX_UPLOAD_BYTES) in detail
    # No temp file left behind.
    assert _temp_files(audio_dir) == []


def test_upload_well_over_limit_returns_413(
    client, auth_token: str, audio_dir: str
) -> None:
    body = b"y" * (MAX_UPLOAD_BYTES * 10)
    resp = client.post(
        "/api/meetings",
        headers=_auth(auth_token),
        files={"file": ("huge.mp3", io.BytesIO(body), "audio/mpeg")},
    )
    assert resp.status_code == 413
    assert _temp_files(audio_dir) == []


def test_upload_does_not_trust_content_length(
    client, auth_token: str, audio_dir: str
) -> None:
    """A header that understates the size must not let an oversized body pass.

    The size decision is based on bytes actually read, so even if a client
    declares a small ``Content-Length`` while sending more, the running count
    trips the 413. We send a body well over the limit and force a tiny,
    dishonest ``Content-Length`` header; the endpoint must still reject it.
    """
    body = b"z" * (MAX_UPLOAD_BYTES * 4)
    # Build the multipart payload, then override Content-Length to a tiny lie.
    req = client.build_request(
        "POST",
        "/api/meetings",
        headers=_auth(auth_token),
        files={"file": ("liar.wav", io.BytesIO(body), "audio/wav")},
    )
    req.headers["Content-Length"] = "10"  # dishonest, far smaller than the body
    resp = client.send(req)
    assert resp.status_code == 413
    assert _temp_files(audio_dir) == []


# --- 400: 0-byte upload (Req 3.4) -------------------------------------------


def test_upload_empty_file_returns_400(
    client, auth_token: str, audio_dir: str
) -> None:
    resp = client.post(
        "/api/meetings",
        headers=_auth(auth_token),
        files={"file": ("empty.wav", io.BytesIO(b""), "audio/wav")},
    )
    assert resp.status_code == 400
    assert "empty" in resp.json()["detail"].lower()
    assert _temp_files(audio_dir) == []


# --- valid upload passes 8.1 checks -----------------------------------------


def test_upload_small_valid_file_passes_checks(
    client, auth_token: str, audio_dir: str
) -> None:
    body = b"a" * (MAX_UPLOAD_BYTES // 2)
    resp = client.post(
        "/api/meetings",
        headers=_auth(auth_token),
        files={"file": ("ok.wav", io.BytesIO(body), "audio/wav")},
    )
    # Real contract (task 8.3): 201 with the new record id; the validated temp
    # file is moved to its final location, so the tmp staging area is empty.
    assert resp.status_code == 201
    assert "id" in resp.json()
    assert _temp_files(audio_dir) == []


def test_upload_at_exact_limit_is_accepted(
    client, auth_token: str, audio_dir: str
) -> None:
    """A body of exactly max_upload_bytes is within budget (not over)."""
    body = b"b" * MAX_UPLOAD_BYTES
    resp = client.post(
        "/api/meetings",
        headers=_auth(auth_token),
        files={"file": ("exact.wav", io.BytesIO(body), "audio/wav")},
    )
    assert resp.status_code == 201
    assert "id" in resp.json()
    assert _temp_files(audio_dir) == []


# --- 401: unauthenticated (Req 1.1) -----------------------------------------


def test_upload_without_token_returns_401(client, audio_dir: str) -> None:
    body = b"a" * 16
    resp = client.post(
        "/api/meetings",
        files={"file": ("ok.wav", io.BytesIO(body), "audio/wav")},
    )
    assert resp.status_code == 401
    # No content on auth failure, and nothing written to disk.
    assert resp.content == b""
    assert _temp_files(audio_dir) == []


def test_upload_with_invalid_token_returns_401(client, audio_dir: str) -> None:
    body = b"a" * 16
    resp = client.post(
        "/api/meetings",
        headers=_auth("garbage-token"),
        files={"file": ("ok.wav", io.BytesIO(body), "audio/wav")},
    )
    assert resp.status_code == 401
    assert _temp_files(audio_dir) == []
