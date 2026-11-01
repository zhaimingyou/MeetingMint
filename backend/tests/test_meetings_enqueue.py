"""Example tests for the post-commit Celery enqueue -- task 8.4.

Covers the contract that ``POST /api/meetings`` enqueues the ``process_meeting``
task by name *after* the create transaction commits, and that an enqueue failure
does not break the request (the row is already committed and queued):

- **enqueue invoked on success**: a valid upload returns 201 and
  ``celery_app.send_task`` is called exactly once with ``"process_meeting"`` and
  ``args=[<meeting_id>]`` matching the returned id;
- **enqueue only on success**: validation-rejected uploads (415 / 400) do not
  enqueue;
- **enqueue failure is swallowed**: when ``send_task`` raises (broker down), the
  upload still returns 201 and the Meeting_Record + queued Processing_Job exist;
- **celery_app constructs lazily**: building the app does not connect, and it
  resolves its broker from ``Settings.redis_url``.
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
from app.worker.celery_app import (
    PROCESS_MEETING_TASK_NAME,
    create_celery_app,
)

USERNAME = "enqueue-uploader"
PASSWORD = "correct-horse-battery-staple"
REDIS_URL = "redis://test-broker:6379/3"


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
        redis_url=REDIS_URL,
        database_url="sqlite://",
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


class _Spy:
    """Records ``send_task`` invocations; optionally raises to simulate a broker error."""

    def __init__(self, raises: bool = False) -> None:
        self.calls: list[tuple[tuple, dict]] = []
        self.raises = raises

    def __call__(self, *args, **kwargs):
        self.calls.append((args, kwargs))
        if self.raises:
            raise RuntimeError("simulated broker unreachable")
        return None


def _make_client(
    upload_settings: Settings, session_factory: sessionmaker[OrmSession]
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


# --- enqueue invoked once after a successful commit -------------------------


def test_enqueue_invoked_after_commit_with_meeting_id(
    upload_settings, session_factory, auth_token, monkeypatch
) -> None:
    monkeypatch.setattr("app.api.meetings.detect_format", lambda _p: "wav")
    spy = _Spy()
    monkeypatch.setattr("app.api.meetings.celery_app.send_task", spy)

    app, client = _make_client(upload_settings, session_factory)
    try:
        with client:
            resp = client.post(
                "/api/meetings",
                headers=_auth(auth_token),
                files={"file": ("a.wav", io.BytesIO(b"abc"), "audio/wav")},
            )
    finally:
        app.dependency_overrides.clear()

    assert resp.status_code == 201
    meeting_id = resp.json()["id"]

    # Enqueued exactly once, by task name, with the new meeting id.
    assert len(spy.calls) == 1
    args, kwargs = spy.calls[0]
    assert args[0] == PROCESS_MEETING_TASK_NAME == "process_meeting"
    assert kwargs.get("args") == [meeting_id]

    # The row was committed before enqueue: meeting + queued job exist.
    meetings, jobs = _rows(session_factory)
    assert len(meetings) == 1 and meetings[0].id == meeting_id
    assert len(jobs) == 1 and jobs[0].status == JobStatus.QUEUED.value


# --- no enqueue on validation-rejected uploads ------------------------------


def test_no_enqueue_on_unsupported_format(
    upload_settings, session_factory, auth_token, monkeypatch
) -> None:
    monkeypatch.setattr("app.api.meetings.detect_format", lambda _p: None)
    spy = _Spy()
    monkeypatch.setattr("app.api.meetings.celery_app.send_task", spy)

    app, client = _make_client(upload_settings, session_factory)
    try:
        with client:
            resp = client.post(
                "/api/meetings",
                headers=_auth(auth_token),
                files={"file": ("x.bin", io.BytesIO(b"abc"), "application/octet-stream")},
            )
    finally:
        app.dependency_overrides.clear()

    assert resp.status_code == 415
    assert spy.calls == []


def test_no_enqueue_on_empty_file(
    upload_settings, session_factory, auth_token, monkeypatch
) -> None:
    monkeypatch.setattr("app.api.meetings.detect_format", lambda _p: "wav")
    spy = _Spy()
    monkeypatch.setattr("app.api.meetings.celery_app.send_task", spy)

    app, client = _make_client(upload_settings, session_factory)
    try:
        with client:
            resp = client.post(
                "/api/meetings",
                headers=_auth(auth_token),
                files={"file": ("empty.wav", io.BytesIO(b""), "audio/wav")},
            )
    finally:
        app.dependency_overrides.clear()

    assert resp.status_code == 400
    assert spy.calls == []


# --- enqueue failure does not break the request -----------------------------


def test_enqueue_failure_still_returns_201_and_persists(
    upload_settings, session_factory, auth_token, monkeypatch
) -> None:
    monkeypatch.setattr("app.api.meetings.detect_format", lambda _p: "wav")
    spy = _Spy(raises=True)
    monkeypatch.setattr("app.api.meetings.celery_app.send_task", spy)

    app, client = _make_client(upload_settings, session_factory)
    try:
        with client:
            resp = client.post(
                "/api/meetings",
                headers=_auth(auth_token),
                files={"file": ("a.wav", io.BytesIO(b"abc"), "audio/wav")},
            )
    finally:
        app.dependency_overrides.clear()

    # Broker error during enqueue is swallowed: still 201, row committed/queued.
    assert resp.status_code == 201
    assert len(spy.calls) == 1
    meeting_id = resp.json()["id"]
    meetings, jobs = _rows(session_factory)
    assert len(meetings) == 1 and meetings[0].id == meeting_id
    assert len(jobs) == 1 and jobs[0].status == JobStatus.QUEUED.value


# --- celery_app constructs lazily and reads Settings.redis_url --------------


def test_celery_app_constructs_without_connecting_and_uses_redis_url(
    upload_settings,
) -> None:
    # Construction performs no broker connection; broker/backend resolve lazily
    # from the provided Settings.redis_url on first config access.
    app = create_celery_app(upload_settings)
    assert app.conf.broker_url == REDIS_URL
    assert app.conf.result_backend == REDIS_URL
