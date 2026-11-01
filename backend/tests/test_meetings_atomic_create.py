"""Example tests for the atomic create ordering -- task 8.3.

Drives ``POST /api/meetings`` through a FastAPI TestClient (content-based format
detection injected via the ``app.api.meetings.detect_format`` seam) and asserts
the atomic-create contract (Req 3.1 / 3.5 / 3.6 / 7.1):

- a valid supported upload -> **201 + {"id": ...}**; exactly one
  ``Meeting_Record`` (owned by the authed user, with ``source_format`` set and
  ``source_audio_path`` pointing at an existing file under ``audio_storage_dir``
  containing the uploaded bytes) and a ``Processing_Job(queued)``; the temp file
  is gone and the final file exists;
- the title default rule: a filename upload titles from the filename, a nameless
  recording blob titles ``会议-...``;
- **DB-failure compensation**: force the insert/commit to raise -> **500**, no
  ``Meeting_Record`` / ``Processing_Job`` row, no leftover final/temp file;
- **file-move-failure compensation**: force the move to raise -> **500**, no row.

(Property tests for upload -- Properties 5/7 -- are task 8.5.)
"""

from __future__ import annotations

import io
import os
from collections.abc import Iterator

import pytest
from sqlalchemy import select
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session as OrmSession
from sqlalchemy.orm import sessionmaker

from app.config import Settings, get_settings
from app.db import get_session, make_engine, make_session_factory
from app.enums import JobStatus
from app.main import create_app
from app.models import MeetingRecord, ProcessingJob, User, create_all
from app.security.passwords import hash_password
from app.security.sessions import issue_session

MAX_UPLOAD_BYTES = 4 * 1024  # 4 KiB
USERNAME = "atomic-uploader"
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
def seeded_user_id(session_factory: sessionmaker[OrmSession]) -> str:
    sess = session_factory()
    try:
        user = User(username=USERNAME, password_hash=hash_password(PASSWORD))
        sess.add(user)
        sess.commit()
        sess.refresh(user)
        return user.id
    finally:
        sess.close()


@pytest.fixture()
def auth_token(
    session_factory: sessionmaker[OrmSession], seeded_user_id: str
) -> str:
    sess = session_factory()
    try:
        session = issue_session(sess, seeded_user_id, ttl_minutes=60)
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

    # Synthetic byte blobs (not real audio); pin the detector to a supported
    # format so the create path is exercised without a real ffprobe binary.
    monkeypatch.setattr("app.api.meetings.detect_format", lambda _p: "wav")

    # No broker runs under test: replace the post-commit enqueue with a no-op so
    # the success path does not attempt a real Celery/Redis connection. The
    # enqueue behavior itself is covered by tests/test_meetings_enqueue.py.
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
    tmp = os.path.join(audio_dir, "tmp")
    if not os.path.isdir(tmp):
        return []
    return [os.path.join(tmp, n) for n in os.listdir(tmp)]


def _final_files(audio_dir: str) -> list[str]:
    """Final audio files written directly under audio_dir (excludes tmp/)."""
    return [
        os.path.join(audio_dir, n)
        for n in os.listdir(audio_dir)
        if os.path.isfile(os.path.join(audio_dir, n))
    ]


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _rows(session_factory: sessionmaker[OrmSession]):
    sess = session_factory()
    try:
        meetings = list(sess.execute(select(MeetingRecord)).scalars())
        jobs = list(sess.execute(select(ProcessingJob)).scalars())
        return meetings, jobs
    finally:
        sess.close()


# --- success: exactly one meeting + queued job, file persisted (Req 3.1/3.6/7.1)


def test_valid_upload_creates_record_job_and_file(
    client, session_factory, auth_token, seeded_user_id, audio_dir
) -> None:
    body = b"hello-meeting-audio"
    resp = client.post(
        "/api/meetings",
        headers=_auth(auth_token),
        files={"file": ("team-sync.wav", io.BytesIO(body), "audio/wav")},
    )

    assert resp.status_code == 201
    meeting_id = resp.json()["id"]
    assert meeting_id

    meetings, jobs = _rows(session_factory)
    # Exactly one Meeting_Record, owned by the authed user.
    assert len(meetings) == 1
    record = meetings[0]
    assert record.id == meeting_id
    assert record.owner_id == seeded_user_id
    assert record.source_format == "wav"

    # source_audio_path points at an existing file under audio_storage_dir
    # holding the uploaded bytes; final filename matches the record id.
    assert record.source_audio_path == os.path.join(audio_dir, f"{meeting_id}.wav")
    assert os.path.isfile(record.source_audio_path)
    with open(record.source_audio_path, "rb") as fh:
        assert fh.read() == body

    # Exactly one Processing_Job, queued, tied to the meeting (Req 7.1).
    assert len(jobs) == 1
    assert jobs[0].meeting_id == meeting_id
    assert jobs[0].status == JobStatus.QUEUED.value

    # Temp file gone; exactly one final file on disk.
    assert _temp_files(audio_dir) == []
    assert len(_final_files(audio_dir)) == 1


def test_valid_upload_creates_exactly_one_meeting(
    client, session_factory, auth_token, audio_dir
) -> None:
    resp = client.post(
        "/api/meetings",
        headers=_auth(auth_token),
        files={"file": ("a.wav", io.BytesIO(b"abc"), "audio/wav")},
    )
    assert resp.status_code == 201
    meetings, jobs = _rows(session_factory)
    assert len(meetings) == 1
    assert len(jobs) == 1


# --- title default rule ------------------------------------------------------


def test_title_defaults_to_filename(
    client, session_factory, auth_token
) -> None:
    resp = client.post(
        "/api/meetings",
        headers=_auth(auth_token),
        files={"file": ("Q3 planning.wav", io.BytesIO(b"abc"), "audio/wav")},
    )
    assert resp.status_code == 201
    assert resp.json()["title"] == "Q3 planning.wav"
    meetings, _ = _rows(session_factory)
    assert meetings[0].title == "Q3 planning.wav"


def test_title_defaults_to_meeting_prefix_for_nameless_blob(
    client, session_factory, auth_token
) -> None:
    # A recording blob whose filename is blank/whitespace -> 会议-<created_at>.
    resp = client.post(
        "/api/meetings",
        headers=_auth(auth_token),
        files={"file": ("   ", io.BytesIO(b"abc"), "audio/wav")},
    )
    assert resp.status_code == 201
    title = resp.json()["title"]
    assert title.startswith("会议-")
    meetings, _ = _rows(session_factory)
    assert meetings[0].title == title


# --- DB-failure compensation (Req 3.5) --------------------------------------


def test_db_failure_leaves_no_row_and_no_file(
    client, session_factory, auth_token, audio_dir, monkeypatch
) -> None:
    """Forcing the create transaction to raise -> 500, no row, no stray file."""

    def _boom(*_args, **_kwargs):
        raise RuntimeError("simulated DB failure")

    monkeypatch.setattr("app.api.meetings._persist_meeting_record", _boom)

    resp = client.post(
        "/api/meetings",
        headers=_auth(auth_token),
        files={"file": ("x.wav", io.BytesIO(b"bytes"), "audio/wav")},
    )

    assert resp.status_code == 500
    meetings, jobs = _rows(session_factory)
    assert meetings == []
    assert jobs == []
    # The moved final file was compensated away; no temp left either.
    assert _final_files(audio_dir) == []
    assert _temp_files(audio_dir) == []


# --- file-move-failure compensation (Req 3.5) -------------------------------


def test_file_move_failure_leaves_no_row(
    client, session_factory, auth_token, audio_dir, monkeypatch
) -> None:
    """Forcing the fsync+move to raise -> 500, no Meeting_Record created."""

    def _boom(*_args, **_kwargs):
        raise OSError("simulated move failure")

    monkeypatch.setattr("app.api.meetings._fsync_and_move", _boom)

    resp = client.post(
        "/api/meetings",
        headers=_auth(auth_token),
        files={"file": ("x.wav", io.BytesIO(b"bytes"), "audio/wav")},
    )

    assert resp.status_code == 500
    meetings, jobs = _rows(session_factory)
    assert meetings == []
    assert jobs == []
    assert _final_files(audio_dir) == []
    assert _temp_files(audio_dir) == []
