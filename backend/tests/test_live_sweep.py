"""Tests for the inactivity sweep finalizer (task 8; Property 36)."""

from __future__ import annotations

import asyncio
import os
import tempfile
from datetime import timedelta
from uuid import uuid4

import app.api.live as live_mod
import app.api.meetings as meetings_mod
from app.api.live import reset_coordination, sweep_inactive_live_sessions
from app.config import Settings
from app.db import make_engine, make_session_factory
from app.enums import JobStatus
from app.models import MeetingRecord, ProcessingJob, User, create_all, utcnow
from app.security.passwords import hash_password
from sqlalchemy import select


class _EnqueueSpy:
    def __init__(self) -> None:
        self.calls: list[str] = []

    def __call__(self, meeting_id: str) -> None:
        self.calls.append(meeting_id)


def _settings(audio_dir: str, db_path: str, inactivity: int = 300) -> Settings:
    return Settings(
        bootstrap_admin_username="admin",
        bootstrap_admin_password="bootstrap-pass",
        redis_url="redis://localhost:6379/0",
        database_url=f"sqlite:///{db_path}",
        audio_storage_dir=audio_dir,
        live_inactivity_timeout_seconds=inactivity,
    )  # type: ignore[call-arg]


def _seed(sf, audio_dir, *, last_activity, recording_size: int) -> str:
    sess = sf()
    try:
        u = User(username=f"u-{uuid4()}", password_hash=hash_password("p"))
        sess.add(u)
        sess.flush()
        mid = str(uuid4())
        path = os.path.join(audio_dir, f"{mid}.webm")
        if recording_size > 0:
            with open(path, "wb") as fh:
                fh.write(b"\x00" * recording_size)
        sess.add(
            MeetingRecord(
                id=mid, owner_id=u.id, title="会议-x",
                source_audio_path=path, source_format="webm",
            )
        )
        sess.add(
            ProcessingJob(
                meeting_id=mid, status=JobStatus.LIVE.value, owner_id=u.id,
                last_seq=0 if recording_size else None,
                recording_bytes=recording_size, last_activity_at=last_activity,
            )
        )
        sess.commit()
        return mid
    finally:
        sess.close()


# Feature: realtime-meeting-transcription, Property 36: Inactivity finalizes a live session as if it were ended, via the same exactly-once compare-and-set.
# Validates: Requirements 7.3
def test_property_36_sweep_finalizes_only_idle_sessions() -> None:
    work = tempfile.mkdtemp(prefix="sweep_")
    audio_dir = os.path.join(work, "audio")
    os.makedirs(audio_dir, exist_ok=True)
    db_path = os.path.join(work, "app.db")
    engine = make_engine(f"sqlite:///{db_path}")
    create_all(engine)
    sf = make_session_factory(engine)
    reset_coordination()

    saved = meetings_mod._enqueue_processing_job
    spy = _EnqueueSpy()
    meetings_mod._enqueue_processing_job = spy
    try:
        settings = _settings(audio_dir, db_path, inactivity=300)
        now = utcnow()
        # Idle 10 minutes ago (> 300s) with audio -> finalized to queued.
        idle = _seed(sf, audio_dir, last_activity=now - timedelta(minutes=10), recording_size=1024)
        # Active 10 seconds ago (< 300s) -> left live.
        active = _seed(sf, audio_dir, last_activity=now - timedelta(seconds=10), recording_size=1024)

        finalized = asyncio.run(
            sweep_inactive_live_sessions(session_factory=sf, settings=settings, now=now)
        )

        assert finalized == [idle]
        assert spy.calls == [idle]

        def _status(mid):
            s = sf()
            try:
                return s.execute(
                    select(ProcessingJob).where(ProcessingJob.meeting_id == mid)
                ).scalar_one().status
            finally:
                s.close()

        assert _status(idle) == JobStatus.QUEUED.value
        assert _status(active) == JobStatus.LIVE.value
    finally:
        meetings_mod._enqueue_processing_job = saved
        engine.dispose()


def test_sweep_races_explicit_end_enqueues_once() -> None:
    """A sweep racing an explicit finalize still enqueues exactly once."""
    from app.api.live import finalize_live_meeting

    work = tempfile.mkdtemp(prefix="sweep2_")
    audio_dir = os.path.join(work, "audio")
    os.makedirs(audio_dir, exist_ok=True)
    db_path = os.path.join(work, "app.db")
    engine = make_engine(f"sqlite:///{db_path}")
    create_all(engine)
    sf = make_session_factory(engine)
    reset_coordination()

    saved = meetings_mod._enqueue_processing_job
    spy = _EnqueueSpy()
    meetings_mod._enqueue_processing_job = spy
    try:
        settings = _settings(audio_dir, db_path, inactivity=30)
        now = utcnow()
        mid = _seed(sf, audio_dir, last_activity=now - timedelta(minutes=5), recording_size=2048)

        async def _race():
            return await asyncio.gather(
                sweep_inactive_live_sessions(session_factory=sf, settings=settings, now=now),
                finalize_live_meeting(mid, session_factory=sf),
            )

        asyncio.run(_race())
        assert spy.calls == [mid]
    finally:
        meetings_mod._enqueue_processing_job = saved
        engine.dispose()
