"""Endpoint integration tests for content-based format detection -- task 8.2.

Drives ``POST /api/meetings`` through a FastAPI TestClient, injecting the
module-level detector seam (``app.api.meetings.detect_format``) so the ffprobe
verdict is simulated without a real binary:

- a file detected as an **unsupported** format -> **415** + a message naming
  mp3/wav/m4a/webm, with **no leftover temp file** (Req 3.2);
- a file detected as a **supported** format -> passes to the finalize stub
  (provisional **201**), echoing the detected ``source_format``.

Mirrors the bespoke client setup in ``test_meetings_upload.py`` (own TestClient
with overridden settings + in-memory DB) since the shared conftest client pins a
500 MB cap and a ``/data`` audio dir.
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

MAX_UPLOAD_BYTES = 4 * 1024  # 4 KiB
USERNAME = "fmt-uploader"
PASSWORD = "correct-horse-battery-staple"


@pytest.fixture()
def audio_dir(tmp_path) -> str:
    d = tmp_path / "audio"
    d.mkdir()
    return str(d)


@pytest.fixture()
def upload_settings(audio_dir: str) -> Settings:
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


def _make_client(
    upload_settings: Settings,
    session_factory: sessionmaker[OrmSession],
):
    from fastapi.testclient import TestClient

    app = create_app()

    def _override_get_session() -> Iterator[OrmSession]:
        sess = session_factory()
        try:
            yield sess
        finally:
            sess.close()

    app.dependency_overrides[get_session] = _override_get_session
    app.dependency_overrides[get_settings] = lambda: upload_settings
    return app, TestClient(app)


def _temp_files(audio_dir: str) -> list[str]:
    tmp = os.path.join(audio_dir, "tmp")
    if not os.path.isdir(tmp):
        return []
    return [os.path.join(tmp, n) for n in os.listdir(tmp)]


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


# --- 415: unsupported format (Req 3.2) --------------------------------------


def test_unsupported_format_returns_415(
    upload_settings, session_factory, auth_token, audio_dir, monkeypatch
) -> None:
    # Detector reports None (e.g. a video-only or unknown container).
    monkeypatch.setattr("app.api.meetings.detect_format", lambda _p: None)
    app, client = _make_client(upload_settings, session_factory)
    try:
        with client:
            resp = client.post(
                "/api/meetings",
                headers=_auth(auth_token),
                # Name says .wav, but content detection (not the name) decides.
                files={"file": ("clip.wav", io.BytesIO(b"not-real-audio"), "audio/wav")},
            )
    finally:
        app.dependency_overrides.clear()

    assert resp.status_code == 415
    detail = resp.json()["detail"]
    # Message names every supported format (Req 3.2).
    for fmt in ("mp3", "wav", "m4a", "webm"):
        assert fmt in detail
    # No Meeting_Record created and no leftover temp file.
    assert _temp_files(audio_dir) == []


def test_unsupported_format_lists_supported_formats_field(
    upload_settings, session_factory, auth_token, audio_dir, monkeypatch
) -> None:
    monkeypatch.setattr("app.api.meetings.detect_format", lambda _p: None)
    app, client = _make_client(upload_settings, session_factory)
    try:
        with client:
            resp = client.post(
                "/api/meetings",
                headers=_auth(auth_token),
                files={"file": ("x.bin", io.BytesIO(b"zzzz"), "application/octet-stream")},
            )
    finally:
        app.dependency_overrides.clear()

    assert resp.status_code == 415
    assert resp.json()["supported_formats"] == ["mp3", "wav", "m4a", "webm"]


def test_detected_format_not_in_settings_supported_returns_415(
    upload_settings, session_factory, auth_token, audio_dir, monkeypatch
) -> None:
    """A format the detector recognizes but configuration does not allow -> 415."""
    monkeypatch.setattr("app.api.meetings.detect_format", lambda _p: "flac")
    app, client = _make_client(upload_settings, session_factory)
    try:
        with client:
            resp = client.post(
                "/api/meetings",
                headers=_auth(auth_token),
                files={"file": ("a.flac", io.BytesIO(b"abcd"), "audio/flac")},
            )
    finally:
        app.dependency_overrides.clear()

    assert resp.status_code == 415
    assert _temp_files(audio_dir) == []


# --- 201: supported format passes to the atomic create ----------------------


@pytest.mark.parametrize("fmt", ["mp3", "wav", "m4a", "webm"])
def test_supported_format_passes_to_finalize(
    upload_settings, session_factory, auth_token, audio_dir, monkeypatch, fmt
) -> None:
    monkeypatch.setattr("app.api.meetings.detect_format", lambda _p: fmt)
    # No broker under test: stub the post-commit enqueue (covered separately by
    # tests/test_meetings_enqueue.py) so the 201 path makes no Celery/Redis call.
    monkeypatch.setattr("app.api.meetings._enqueue_processing_job", lambda _id: None)
    app, client = _make_client(upload_settings, session_factory)
    try:
        with client:
            resp = client.post(
                "/api/meetings",
                headers=_auth(auth_token),
                # Misleading extension on purpose: detection drives the verdict.
                files={"file": ("recording.bin", io.BytesIO(b"audio-bytes"), "application/octet-stream")},
            )
    finally:
        app.dependency_overrides.clear()

    # Real contract (task 8.3): 201 + the new record id; the detected format is
    # persisted on the row (asserted in the atomic-create suite) and the final
    # audio file is named with that extension.
    assert resp.status_code == 201
    body = resp.json()
    assert "id" in body
    final_path = os.path.join(audio_dir, f"{body['id']}.{fmt}")
    assert os.path.isfile(final_path)
    assert _temp_files(audio_dir) == []
