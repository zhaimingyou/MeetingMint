"""Tests for global job monitoring (admin-console, task 10).

Covers cross-user listing, status filtering, paging bounds, no-content-leak
(Req 9.6), and the admin (cross-user) resubmit: failed -> queued + re-enqueue,
non-failed -> 409, unknown -> 404, plus admin gating.
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session as OrmSession
from sqlalchemy.orm import sessionmaker

import app.api.meetings as meetings_mod
from app.config import Settings, get_settings
from app.db import get_session, make_engine, make_session_factory
from app.enums import JobStatus
from app.main import create_app
from app.models import AuditLogEntry, MeetingRecord, ProcessingJob, User, create_all
from app.security.passwords import hash_password
from app.security.sessions import issue_session

PASSWORD = "correct-horse-battery-staple"


def _settings() -> Settings:
    return Settings(
        bootstrap_admin_username="admin",
        bootstrap_admin_password="pw",
        redis_url="redis://localhost:6379/0",
        database_url="sqlite://",
    )  # type: ignore[call-arg]


class _EnqueueSpy:
    def __init__(self) -> None:
        self.calls: list[str] = []

    def __call__(self, meeting_id: str) -> None:
        self.calls.append(meeting_id)


@pytest.fixture()
def harness() -> Iterator[tuple]:
    engine = make_engine("sqlite://")
    create_all(engine)
    sf = make_session_factory(engine)

    ids: dict = {}
    s = sf()
    try:
        admin = User(username="admin", password_hash=hash_password(PASSWORD), is_admin=True)
        alice = User(username="alice", password_hash=hash_password(PASSWORD))
        bob = User(username="bob", password_hash=hash_password(PASSWORD))
        s.add_all([admin, alice, bob])
        s.commit()
        ids["admin_token"] = issue_session(s, admin.id, ttl_minutes=60).token
        ids["bob_token"] = issue_session(s, bob.id, ttl_minutes=60).token

        # alice: a failed meeting; bob: a completed meeting.
        m_failed = MeetingRecord(
            owner_id=alice.id, title="alice-failed", source_audio_path="/a.webm",
            source_format="webm",
        )
        m_done = MeetingRecord(
            owner_id=bob.id, title="bob-done", source_audio_path="/b.webm",
            source_format="webm",
        )
        s.add_all([m_failed, m_done])
        s.flush()
        s.add(
            ProcessingJob(
                meeting_id=m_failed.id,
                status=JobStatus.FAILED.value,
                error_description="boom",
            )
        )
        s.add(ProcessingJob(meeting_id=m_done.id, status=JobStatus.COMPLETED.value))
        s.commit()
        ids["failed_meeting"] = m_failed.id
        ids["done_meeting"] = m_done.id
    finally:
        s.close()

    base = _settings()
    spy = _EnqueueSpy()
    saved_enqueue = meetings_mod._enqueue_processing_job
    meetings_mod._enqueue_processing_job = spy

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
            yield client, sf, ids, spy
    finally:
        app.dependency_overrides.clear()
        meetings_mod._enqueue_processing_job = saved_enqueue
        engine.dispose()


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def test_list_jobs_cross_user(harness) -> None:
    client, _, ids, _ = harness
    resp = client.get("/api/admin/jobs", headers=_auth(ids["admin_token"]))
    assert resp.status_code == 200
    body = resp.json()
    assert body["total"] == 2
    owners = {item["owner_username"] for item in body["items"]}
    assert owners == {"alice", "bob"}
    # The failed item carries its error; the completed one does not, and no
    # transcript/minutes content is present.
    failed = next(i for i in body["items"] if i["status"] == "failed")
    assert failed["error_description"] == "boom"
    done = next(i for i in body["items"] if i["status"] == "completed")
    assert "error_description" not in done
    assert all("transcript" not in i and "minutes" not in i for i in body["items"])


def test_list_jobs_status_filter(harness) -> None:
    client, _, ids, _ = harness
    resp = client.get(
        "/api/admin/jobs?status=failed", headers=_auth(ids["admin_token"])
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["total"] == 1
    assert body["items"][0]["status"] == "failed"


def test_list_jobs_clamps_page_size(harness) -> None:
    client, _, ids, _ = harness
    resp = client.get(
        "/api/admin/jobs?page_size=99999", headers=_auth(ids["admin_token"])
    )
    assert resp.json()["page_size"] == 200


def test_admin_resubmit_failed_requeues_and_enqueues(harness) -> None:
    client, sf, ids, spy = harness
    resp = client.post(
        f"/api/admin/jobs/{ids['failed_meeting']}/resubmit",
        headers=_auth(ids["admin_token"]),
    )
    assert resp.status_code == 200
    assert resp.json()["status"] == "queued"
    assert spy.calls == [ids["failed_meeting"]]
    s = sf()
    try:
        job = s.execute(
            select(ProcessingJob).where(
                ProcessingJob.meeting_id == ids["failed_meeting"]
            )
        ).scalar_one()
        assert job.status == JobStatus.QUEUED.value
        assert job.error_description is None
    finally:
        s.close()


def test_admin_resubmit_enqueue_failure_restores_failed_state(harness) -> None:
    client, sf, ids, _ = harness
    saved_enqueue = meetings_mod._enqueue_processing_job

    def _fail_enqueue(_meeting_id: str) -> None:
        raise RuntimeError("broker unavailable")

    meetings_mod._enqueue_processing_job = _fail_enqueue
    try:
        with TestClient(client.app, raise_server_exceptions=False) as failing_client:
            response = failing_client.post(
                f"/api/admin/jobs/{ids['failed_meeting']}/resubmit",
                headers=_auth(ids["admin_token"]),
            )
    finally:
        meetings_mod._enqueue_processing_job = saved_enqueue

    assert response.status_code == 500
    s = sf()
    try:
        job = s.execute(
            select(ProcessingJob).where(
                ProcessingJob.meeting_id == ids["failed_meeting"]
            )
        ).scalar_one()
        assert job.status == JobStatus.FAILED.value
        assert job.error_description == "failed to enqueue processing job"
        audits = s.execute(
            select(AuditLogEntry).where(AuditLogEntry.action == "job.resubmit")
        ).scalars().all()
        assert audits == []
    finally:
        s.close()


def test_admin_resubmit_non_failed_conflict(harness) -> None:
    client, _, ids, spy = harness
    resp = client.post(
        f"/api/admin/jobs/{ids['done_meeting']}/resubmit",
        headers=_auth(ids["admin_token"]),
    )
    assert resp.status_code == 409
    assert spy.calls == []  # not enqueued


def test_admin_resubmit_unknown_404(harness) -> None:
    client, _, ids, _ = harness
    resp = client.post(
        "/api/admin/jobs/no-such-meeting/resubmit",
        headers=_auth(ids["admin_token"]),
    )
    assert resp.status_code == 404


def test_jobs_routes_require_admin(harness) -> None:
    client, _, ids, _ = harness
    assert client.get("/api/admin/jobs", headers=_auth(ids["bob_token"])).status_code == 403
    assert (
        client.post(
            f"/api/admin/jobs/{ids['failed_meeting']}/resubmit",
            headers=_auth(ids["bob_token"]),
        ).status_code
        == 403
    )


def test_jobs_routes_require_auth(harness) -> None:
    client, _, _, _ = harness
    assert client.get("/api/admin/jobs").status_code == 401
