"""Tests for the REST /end fallback and delete-of-live handling (task 9.2)."""

from __future__ import annotations

import os
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager

from sqlalchemy import func, select

import app.api.live as live_mod
import app.api.meetings as meetings_mod
from app.config import Settings, get_settings
from app.db import get_session, make_engine, make_session_factory
from app.enums import JobStatus
from app.main import create_app
from app.models import MeetingRecord, ProcessingJob, User, create_all
from app.security.passwords import hash_password
from app.security.sessions import issue_session

PASSWORD = "correct-horse-battery-staple"


class _EnqueueSpy:
    def __init__(self) -> None:
        self.calls: list[str] = []

    def __call__(self, meeting_id: str) -> None:
        self.calls.append(meeting_id)


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


@contextmanager
def _harness() -> Iterator[tuple]:
    from fastapi.testclient import TestClient

    work_dir = tempfile.mkdtemp(prefix="live_end_")
    audio_dir = os.path.join(work_dir, "audio")
    os.makedirs(audio_dir, exist_ok=True)
    db_path = os.path.join(work_dir, "app.db")
    engine = make_engine(f"sqlite:///{db_path}")
    create_all(engine)
    sf = make_session_factory(engine)

    s = sf()
    try:
        owner = User(username="end-owner", password_hash=hash_password(PASSWORD))
        other = User(username="end-stranger", password_hash=hash_password(PASSWORD))
        s.add_all([owner, other])
        s.commit()
        owner_id, other_id = owner.id, other.id
        token = issue_session(s, owner_id, ttl_minutes=60).token
        other_token = issue_session(s, other_id, ttl_minutes=60).token
        s.commit()
    finally:
        s.close()

    test_settings = Settings(
        bootstrap_admin_username="admin",
        bootstrap_admin_password="bootstrap-pass",
        redis_url="redis://localhost:6379/0",
        database_url=f"sqlite:///{db_path}",
        audio_storage_dir=audio_dir,
    )  # type: ignore[call-arg]

    live_mod.reset_coordination()
    saved_factory = live_mod._get_session_factory
    live_mod._get_session_factory = lambda: sf
    saved_enqueue = meetings_mod._enqueue_processing_job
    spy = _EnqueueSpy()
    meetings_mod._enqueue_processing_job = spy

    app = create_app()

    def _override_get_session() -> Iterator:
        sess = sf()
        try:
            yield sess
        finally:
            sess.close()

    app.dependency_overrides[get_session] = _override_get_session
    app.dependency_overrides[get_settings] = lambda: test_settings

    try:
        with TestClient(app) as client:
            yield client, sf, audio_dir, token, other_token, spy
    finally:
        app.dependency_overrides.clear()
        live_mod._get_session_factory = saved_factory
        meetings_mod._enqueue_processing_job = saved_enqueue
        engine.dispose()


def _start_live(client, token, audio_dir) -> str:
    resp = client.post("/api/meetings/live", headers=_auth(token))
    assert resp.status_code == 201, resp.text
    mid = resp.json()["id"]
    # Simulate some captured audio so /end queues (not 0-byte).
    with open(os.path.join(audio_dir, f"{mid}.webm"), "wb") as fh:
        fh.write(b"\x00" * 1024)
    sf_path = None  # noqa: F841
    return mid


def _set_recording_bytes(sf, mid: str, n: int) -> None:
    s = sf()
    try:
        job = s.execute(
            select(ProcessingJob).where(ProcessingJob.meeting_id == mid)
        ).scalar_one()
        job.recording_bytes = n
        s.commit()
    finally:
        s.close()


def test_end_finalizes_live_meeting_to_queued() -> None:
    with _harness() as (client, sf, audio_dir, token, other_token, spy):
        mid = _start_live(client, token, audio_dir)
        _set_recording_bytes(sf, mid, 1024)
        resp = client.post(f"/api/meetings/{mid}/end", headers=_auth(token))
        assert resp.status_code == 200, resp.text
        assert resp.json()["status"] == JobStatus.QUEUED.value
        assert spy.calls == [mid]


def test_end_unknown_or_foreign_is_404() -> None:
    with _harness() as (client, sf, audio_dir, token, other_token, spy):
        assert client.post("/api/meetings/nope/end", headers=_auth(token)).status_code == 404
        mid = _start_live(client, token, audio_dir)
        # A stranger cannot end the owner's meeting (owner-scoped 404).
        assert (
            client.post(f"/api/meetings/{mid}/end", headers=_auth(other_token)).status_code
            == 404
        )


def test_end_requires_authentication() -> None:
    with _harness() as (client, sf, audio_dir, token, other_token, spy):
        mid = _start_live(client, token, audio_dir)
        assert client.post(f"/api/meetings/{mid}/end").status_code == 401


def test_delete_of_live_meeting_aborts_and_removes() -> None:
    with _harness() as (client, sf, audio_dir, token, other_token, spy):
        mid = _start_live(client, token, audio_dir)
        source = os.path.join(audio_dir, f"{mid}.webm")
        assert os.path.isfile(source)
        resp = client.delete(f"/api/meetings/{mid}", headers=_auth(token))
        assert resp.status_code == 200, resp.text
        # The live session was marked finalizing (abort signal) before delete.
        assert live_mod.get_coordination().is_finalizing(mid)
        # Row + audio gone.
        s = sf()
        try:
            assert (
                s.execute(select(func.count()).select_from(MeetingRecord)).scalar_one()
                == 0
            )
        finally:
            s.close()
        assert not os.path.exists(source)
