"""Tests for finalize_live_meeting -- exactly-once finalization (task 6).

Covers the zero-byte boundary and **Property 35**: ending finalizes exactly once
across concurrent triggers (atomic compare-and-set), enqueuing the existing
pipeline exactly once when the recording has audio and never when it is empty.

finalize is async and uses a per-meeting ``asyncio.Lock``; concurrent triggers
are driven within a single event loop (``asyncio.gather``) so the same lock
instance serializes them. ``reset_coordination()`` is called per test for
isolation. The enqueue is observed via a spy on the shared
``app.api.meetings._enqueue_processing_job`` seam (no real broker).
"""

from __future__ import annotations

import asyncio
import os
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager

import app.api.live as live_mod
import app.api.meetings as meetings_mod
from app.api.live import finalize_live_meeting, reset_coordination
from app.db import make_engine, make_session_factory
from app.enums import JobStatus
from app.models import MeetingRecord, ProcessingJob, User, create_all
from app.security.passwords import hash_password


class _EnqueueSpy:
    def __init__(self) -> None:
        self.calls: list[str] = []

    def __call__(self, meeting_id: str) -> None:
        self.calls.append(meeting_id)


@contextmanager
def _harness() -> Iterator[tuple]:
    """Yield (session_factory, audio_dir, enqueue_spy) with a file-based engine."""
    work_dir = tempfile.mkdtemp(prefix="live_final_")
    audio_dir = os.path.join(work_dir, "audio")
    os.makedirs(audio_dir, exist_ok=True)
    db_path = os.path.join(work_dir, "app.db")
    engine = make_engine(f"sqlite:///{db_path}")
    create_all(engine)
    session_factory = make_session_factory(engine)

    reset_coordination()
    saved = meetings_mod._enqueue_processing_job
    spy = _EnqueueSpy()
    meetings_mod._enqueue_processing_job = spy
    try:
        yield session_factory, audio_dir, spy
    finally:
        meetings_mod._enqueue_processing_job = saved
        engine.dispose()


def _seed_live_meeting(
    session_factory, audio_dir: str, *, recording_size: int
) -> str:
    """Create an owner + a live meeting; write a recording of ``recording_size``."""
    from uuid import uuid4

    sess = session_factory()
    try:
        user = User(username=f"u-{uuid4()}", password_hash=hash_password("p"))
        sess.add(user)
        sess.flush()
        owner_id = user.id
        mid = str(uuid4())
        source_path = os.path.join(audio_dir, f"{mid}.webm")
        if recording_size > 0:
            with open(source_path, "wb") as fh:
                fh.write(b"\x00" * recording_size)
        sess.add(
            MeetingRecord(
                id=mid,
                owner_id=owner_id,
                title="会议-x",
                source_audio_path=source_path,
                source_format="webm",
            )
        )
        sess.add(
            ProcessingJob(
                meeting_id=mid,
                status=JobStatus.LIVE.value,
                owner_id=owner_id,
                last_seq=0 if recording_size > 0 else None,
                recording_bytes=recording_size,
            )
        )
        sess.commit()
        return mid
    finally:
        sess.close()


def _status(session_factory, mid: str) -> tuple[str, str | None]:
    sess = session_factory()
    try:
        from sqlalchemy import select

        job = sess.execute(
            select(ProcessingJob).where(ProcessingJob.meeting_id == mid)
        ).scalar_one()
        return job.status, job.error_description
    finally:
        sess.close()


def test_finalize_with_audio_queues_and_enqueues_once() -> None:
    with _harness() as (sf, audio_dir, spy):
        mid = _seed_live_meeting(sf, audio_dir, recording_size=2048)
        result = asyncio.run(finalize_live_meeting(mid, session_factory=sf))
        assert result is JobStatus.QUEUED
        assert _status(sf, mid)[0] == JobStatus.QUEUED.value
        assert spy.calls == [mid]


def test_finalize_zero_bytes_fails_and_does_not_enqueue() -> None:
    with _harness() as (sf, audio_dir, spy):
        mid = _seed_live_meeting(sf, audio_dir, recording_size=0)
        result = asyncio.run(finalize_live_meeting(mid, session_factory=sf))
        assert result is JobStatus.FAILED
        status_value, error = _status(sf, mid)
        assert status_value == JobStatus.FAILED.value
        assert error == "未捕获到音频"
        assert spy.calls == []


def test_finalize_is_idempotent_when_called_again() -> None:
    with _harness() as (sf, audio_dir, spy):
        mid = _seed_live_meeting(sf, audio_dir, recording_size=512)
        asyncio.run(finalize_live_meeting(mid, session_factory=sf))
        # A second call after the session already left 'live' is a no-op.
        reset_coordination()
        again = asyncio.run(finalize_live_meeting(mid, session_factory=sf))
        assert again is JobStatus.QUEUED  # current (already-final) status
        assert spy.calls == [mid]  # still only one enqueue


# Feature: realtime-meeting-transcription, Property 35: Ending finalizes correctly across the zero-byte boundary, exactly once across concurrent triggers.
# Validates: Requirements 6.2, 6.5
def test_property_35_exactly_once_under_concurrent_triggers() -> None:
    with _harness() as (sf, audio_dir, spy):
        mid = _seed_live_meeting(sf, audio_dir, recording_size=4096)

        async def _race() -> list:
            # Many triggers fire concurrently for the same session.
            return await asyncio.gather(
                *(finalize_live_meeting(mid, session_factory=sf) for _ in range(10))
            )

        results = asyncio.run(_race())
        # All observe the queued terminal status; the pipeline is enqueued once.
        assert all(r is JobStatus.QUEUED for r in results)
        assert _status(sf, mid)[0] == JobStatus.QUEUED.value
        assert spy.calls == [mid], spy.calls


# Feature: realtime-meeting-transcription, Property 35: zero-byte boundary under concurrency.
# Validates: Requirements 6.2, 6.5
def test_property_35_zero_byte_concurrent_triggers_fail_once() -> None:
    with _harness() as (sf, audio_dir, spy):
        mid = _seed_live_meeting(sf, audio_dir, recording_size=0)

        async def _race() -> list:
            return await asyncio.gather(
                *(finalize_live_meeting(mid, session_factory=sf) for _ in range(6))
            )

        results = asyncio.run(_race())
        assert all(r is JobStatus.FAILED for r in results)
        assert _status(sf, mid)[0] == JobStatus.FAILED.value
        assert spy.calls == []
