"""Tests for the job status + resubmit endpoints -- task 10.

Covers ``GET /api/meetings/{id}/status`` (task 10.1) and
``POST /api/meetings/{id}/resubmit`` (task 10.2), with both example tests and
the ``hypothesis`` property tests for design Properties 19 and 20
(Req 7.5 / 7.6 / 7.7 / 7.8).

Both endpoints are owner-scoped and auth-guarded:
- an absent/invalid token -> no-content 401 (Req 1.1);
- a meeting that does not exist or is owned by another user -> 404, so the API
  never discloses another user's meeting (Req 7.7 / 8.5).

Isolation
---------
``hypothesis`` disallows function-scoped fixtures under ``@given`` (examples
would share state), so every property example builds a fresh, fully isolated
harness via :func:`_status_harness`: a brand-new in-memory SQLite engine +
schema, a seeded owner + valid bearer token, and an ``app.api.meetings``
enqueue-seam spy. The harness restores the seam and clears overrides on exit so
examples never share state. The enqueue seam is patched so no real Celery/Redis
broker is needed; we assert on the spy to verify re-enqueue happens exactly when
a failed job is reset to queued.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st
from sqlalchemy import select

import app.api.meetings as meetings_mod
from app.config import Settings, get_settings
from app.db import get_session, make_engine, make_session_factory
from app.enums import JobStatus
from app.main import create_app
from app.models import MeetingRecord, ProcessingJob, User, create_all
from app.security.passwords import hash_password
from app.security.sessions import issue_session

USERNAME = "status-owner"
OTHER_USERNAME = "status-stranger"
PASSWORD = "correct-horse-battery-staple"

# All Job_Status values that are NOT failed -- resubmit must reject these and
# leave the status unchanged (Req 7.8).
NON_FAILED_STATUSES = [s for s in JobStatus.values() if s != JobStatus.FAILED.value]


class _EnqueueSpy:
    """Records ``_enqueue_processing_job`` invocations (the upload enqueue seam)."""

    def __init__(self) -> None:
        self.calls: list[str] = []

    def __call__(self, meeting_id: str) -> None:
        self.calls.append(meeting_id)
        return None


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _seed_user(session_factory, username: str) -> str:
    sess = session_factory()
    try:
        user = User(username=username, password_hash=hash_password(PASSWORD))
        sess.add(user)
        sess.commit()
        sess.refresh(user)
        return user.id
    finally:
        sess.close()


def _issue_token(session_factory, user_id: str) -> str:
    sess = session_factory()
    try:
        token = issue_session(sess, user_id, ttl_minutes=60).token
        sess.commit()
        return token
    finally:
        sess.close()


def _seed_meeting_with_job(
    session_factory,
    *,
    owner_id: str,
    status_value: str,
    error_description: str | None = None,
) -> str:
    """Insert a Meeting_Record + its Processing_Job with the given status.

    Returns the new meeting id.
    """
    sess = session_factory()
    try:
        record = MeetingRecord(
            owner_id=owner_id,
            title="meeting",
            source_audio_path="/data/audio/x.wav",
            source_format="wav",
        )
        sess.add(record)
        sess.flush()
        job = ProcessingJob(
            meeting_id=record.id,
            status=status_value,
            error_description=error_description,
        )
        sess.add(job)
        sess.commit()
        return record.id
    finally:
        sess.close()


def _job_status(session_factory, meeting_id: str) -> tuple[str, str | None]:
    sess = session_factory()
    try:
        job = sess.execute(
            select(ProcessingJob).where(ProcessingJob.meeting_id == meeting_id)
        ).scalar_one()
        return job.status, job.error_description
    finally:
        sess.close()


@contextmanager
def _status_harness():
    """Yield ``(client, session_factory, owner_id, token, enqueue_spy)``.

    Builds a fresh in-memory DB + schema, seeds an owner with a valid 60-minute
    token, mounts a ``TestClient`` over ``create_app()`` with DB/settings
    overridden, and patches ``app.api.meetings._enqueue_processing_job`` with a
    spy so re-enqueue is observable without a real broker. Everything is torn
    down on exit.
    """
    from fastapi.testclient import TestClient

    engine = make_engine("sqlite://")
    create_all(engine)
    session_factory = make_session_factory(engine)

    owner_id = _seed_user(session_factory, USERNAME)
    token = _issue_token(session_factory, owner_id)

    test_settings = Settings(
        bootstrap_admin_username="admin",
        bootstrap_admin_password="bootstrap-pass",
        redis_url="redis://localhost:6379/0",
        database_url="sqlite://",
    )  # type: ignore[call-arg]

    saved_enqueue = meetings_mod._enqueue_processing_job
    spy = _EnqueueSpy()
    meetings_mod._enqueue_processing_job = spy

    app = create_app()

    def _override_get_session() -> Iterator:
        s = session_factory()
        try:
            yield s
        finally:
            s.close()

    app.dependency_overrides[get_session] = _override_get_session
    app.dependency_overrides[get_settings] = lambda: test_settings

    client = TestClient(app)
    try:
        yield client, session_factory, owner_id, token, spy
    finally:
        app.dependency_overrides.clear()
        meetings_mod._enqueue_processing_job = saved_enqueue
        engine.dispose()


# --------------------------------------------------------------------------- #
# Property 19
# --------------------------------------------------------------------------- #
# Feature: meeting-minutes-service, Property 19: Status queries reflect job state and surface errors
# Validates: Requirements 7.5, 7.7
@settings(max_examples=100, deadline=None, suppress_health_check=[HealthCheck.too_slow])
@given(
    status_value=st.sampled_from(JobStatus.values()),
    error_description=st.text(min_size=0, max_size=80),
)
def test_property_19_status_reflects_state_and_surfaces_errors(
    status_value: str, error_description: str
) -> None:
    """For any existing Processing_Job, the status request returns its current
    status and includes the recorded error_description exactly when the status
    is failed; omits error_description for every other status."""
    with _status_harness() as (client, session_factory, owner_id, token, _spy):
        meeting_id = _seed_meeting_with_job(
            session_factory,
            owner_id=owner_id,
            status_value=status_value,
            error_description=error_description,
        )

        resp = client.get(f"/api/meetings/{meeting_id}/status", headers=_auth(token))

        assert resp.status_code == 200
        payload = resp.json()
        assert payload["status"] == status_value

        if status_value == JobStatus.FAILED.value:
            # Error description surfaced exactly when failed (Req 7.5).
            assert "error_description" in payload
            assert payload["error_description"] == error_description
        else:
            # Never surfaced for non-failed states.
            assert "error_description" not in payload


# Feature: meeting-minutes-service, Property 19: Status queries reflect job state and surface errors
# Validates: Requirements 7.5, 7.7
@settings(max_examples=100, deadline=None, suppress_health_check=[HealthCheck.too_slow])
@given(unknown_id=st.uuids().map(str))
def test_property_19_unknown_or_foreign_job_returns_not_found(
    unknown_id: str,
) -> None:
    """For any identifier with no Processing_Job (truly unknown) or one owned by
    a different user (foreign), the status request returns 404 -- unknown and
    foreign meetings are indistinguishable (Req 7.7 / 8.5)."""
    with _status_harness() as (client, session_factory, owner_id, token, _spy):
        # A meeting that belongs to a *different* user must look not-found to us.
        stranger_id = _seed_user(session_factory, OTHER_USERNAME)
        foreign_meeting = _seed_meeting_with_job(
            session_factory,
            owner_id=stranger_id,
            status_value=JobStatus.COMPLETED.value,
        )

        # Truly unknown id -> 404.
        resp_unknown = client.get(
            f"/api/meetings/{unknown_id}/status", headers=_auth(token)
        )
        assert resp_unknown.status_code == 404
        assert "status" not in resp_unknown.json()

        # Foreign (other user's) meeting -> 404, never disclosed.
        resp_foreign = client.get(
            f"/api/meetings/{foreign_meeting}/status", headers=_auth(token)
        )
        assert resp_foreign.status_code == 404
        assert "status" not in resp_foreign.json()


# --------------------------------------------------------------------------- #
# Property 20
# --------------------------------------------------------------------------- #
# Feature: meeting-minutes-service, Property 20: Resubmission is permitted only from the failed state
# Validates: Requirements 7.6, 7.8
@settings(max_examples=100, deadline=None, suppress_health_check=[HealthCheck.too_slow])
@given(status_value=st.sampled_from(JobStatus.values()))
def test_property_20_resubmission_only_from_failed(status_value: str) -> None:
    """For any Processing_Job: resubmission resets the status to queued and
    re-enqueues the job when the current status is failed; otherwise it is
    rejected (409) and leaves the existing Job_Status unchanged. The enqueue
    seam is invoked exactly when transitioning out of failed."""
    with _status_harness() as (client, session_factory, owner_id, token, spy):
        meeting_id = _seed_meeting_with_job(
            session_factory,
            owner_id=owner_id,
            status_value=status_value,
            error_description="boom" if status_value == JobStatus.FAILED.value else None,
        )

        resp = client.post(
            f"/api/meetings/{meeting_id}/resubmit", headers=_auth(token)
        )

        final_status, final_error = _job_status(session_factory, meeting_id)

        if status_value == JobStatus.FAILED.value:
            # Reset to queued + re-enqueued exactly once (Req 7.6).
            assert resp.status_code == 200
            assert resp.json()["status"] == JobStatus.QUEUED.value
            assert final_status == JobStatus.QUEUED.value
            assert final_error is None  # stale error cleared on reset
            assert spy.calls == [meeting_id]
        else:
            # Rejected, status preserved, no enqueue (Req 7.8).
            assert resp.status_code == 409
            assert final_status == status_value
            assert spy.calls == []


# --------------------------------------------------------------------------- #
# Example tests
# --------------------------------------------------------------------------- #
def test_status_requires_authentication() -> None:
    """GET status with no token -> no-content 401 (Req 1.1)."""
    with _status_harness() as (client, session_factory, owner_id, _token, _spy):
        meeting_id = _seed_meeting_with_job(
            session_factory, owner_id=owner_id, status_value=JobStatus.QUEUED.value
        )
        resp = client.get(f"/api/meetings/{meeting_id}/status")
        assert resp.status_code == 401
        assert resp.content == b""


def test_resubmit_requires_authentication() -> None:
    """POST resubmit with no token -> no-content 401 (Req 1.1)."""
    with _status_harness() as (client, session_factory, owner_id, _token, spy):
        meeting_id = _seed_meeting_with_job(
            session_factory, owner_id=owner_id, status_value=JobStatus.FAILED.value
        )
        resp = client.post(f"/api/meetings/{meeting_id}/resubmit")
        assert resp.status_code == 401
        assert resp.content == b""
        # No state change, no enqueue on an unauthenticated request.
        assert spy.calls == []
        assert _job_status(session_factory, meeting_id)[0] == JobStatus.FAILED.value


def test_status_unknown_meeting_returns_404() -> None:
    """GET status for an unknown id -> 404 (Req 7.7)."""
    with _status_harness() as (client, _session_factory, _owner_id, token, _spy):
        resp = client.get(
            "/api/meetings/00000000-0000-0000-0000-000000000000/status",
            headers=_auth(token),
        )
        assert resp.status_code == 404


def test_status_failed_job_exposes_error_description() -> None:
    """A failed job's status response carries the recorded error (Req 7.5)."""
    with _status_harness() as (client, session_factory, owner_id, token, _spy):
        meeting_id = _seed_meeting_with_job(
            session_factory,
            owner_id=owner_id,
            status_value=JobStatus.FAILED.value,
            error_description="decode failure: unreadable source",
        )
        resp = client.get(f"/api/meetings/{meeting_id}/status", headers=_auth(token))
        assert resp.status_code == 200
        payload = resp.json()
        assert payload["status"] == JobStatus.FAILED.value
        assert payload["error_description"] == "decode failure: unreadable source"


def test_status_completed_job_omits_error_description() -> None:
    """A non-failed job's status response omits error_description (Req 7.5)."""
    with _status_harness() as (client, session_factory, owner_id, token, _spy):
        meeting_id = _seed_meeting_with_job(
            session_factory, owner_id=owner_id, status_value=JobStatus.COMPLETED.value
        )
        resp = client.get(f"/api/meetings/{meeting_id}/status", headers=_auth(token))
        assert resp.status_code == 200
        assert "error_description" not in resp.json()


def test_resubmit_from_failed_resets_to_queued_and_enqueues_once() -> None:
    """Resubmitting a failed job resets it to queued and enqueues once (Req 7.6)."""
    with _status_harness() as (client, session_factory, owner_id, token, spy):
        meeting_id = _seed_meeting_with_job(
            session_factory,
            owner_id=owner_id,
            status_value=JobStatus.FAILED.value,
            error_description="boom",
        )
        resp = client.post(
            f"/api/meetings/{meeting_id}/resubmit", headers=_auth(token)
        )
        assert resp.status_code == 200
        assert resp.json()["status"] == JobStatus.QUEUED.value
        status_value, error = _job_status(session_factory, meeting_id)
        assert status_value == JobStatus.QUEUED.value
        assert error is None
        assert spy.calls == [meeting_id]


@pytest.mark.parametrize(
    "status_value",
    [
        JobStatus.QUEUED.value,
        JobStatus.PREPROCESSING.value,
        JobStatus.TRANSCRIBING.value,
        JobStatus.GENERATING.value,
        JobStatus.COMPLETED.value,
    ],
)
def test_resubmit_from_non_failed_rejected_and_unchanged(status_value: str) -> None:
    """Resubmitting a non-failed job -> 409, status preserved, no enqueue (Req 7.8)."""
    with _status_harness() as (client, session_factory, owner_id, token, spy):
        meeting_id = _seed_meeting_with_job(
            session_factory, owner_id=owner_id, status_value=status_value
        )
        resp = client.post(
            f"/api/meetings/{meeting_id}/resubmit", headers=_auth(token)
        )
        assert resp.status_code == 409
        assert _job_status(session_factory, meeting_id)[0] == status_value
        assert spy.calls == []


def test_resubmit_unknown_meeting_returns_404() -> None:
    """POST resubmit for an unknown id -> 404 (Req 7.7)."""
    with _status_harness() as (client, _session_factory, _owner_id, token, spy):
        resp = client.post(
            "/api/meetings/00000000-0000-0000-0000-000000000000/resubmit",
            headers=_auth(token),
        )
        assert resp.status_code == 404
        assert spy.calls == []


def test_resubmit_foreign_meeting_returns_404_and_unchanged() -> None:
    """Resubmitting another user's failed meeting -> 404, unchanged (Req 7.7 / 8.5)."""
    with _status_harness() as (client, session_factory, _owner_id, token, spy):
        stranger_id = _seed_user(session_factory, OTHER_USERNAME)
        foreign_meeting = _seed_meeting_with_job(
            session_factory, owner_id=stranger_id, status_value=JobStatus.FAILED.value
        )
        resp = client.post(
            f"/api/meetings/{foreign_meeting}/resubmit", headers=_auth(token)
        )
        assert resp.status_code == 404
        # Foreign job untouched and not enqueued.
        assert _job_status(session_factory, foreign_meeting)[0] == JobStatus.FAILED.value
        assert spy.calls == []
