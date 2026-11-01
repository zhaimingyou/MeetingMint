"""Tests for the WS /api/meetings/{id}/live Streaming_Connection handler (task 7).

Covers handshake auth rejection (Property 31), sequence-numbered idempotent
append + resume + assembly (Properties 34/43), best-effort relay (Property 38),
graceful degradation (Property 39), preview-not-persisted (Property 37), and the
append/finalize mutual exclusion + supersede (Property 44). Driven with Starlette
``TestClient.websocket_connect`` and the deterministic mock streaming client (no
ffmpeg, no network).
"""

from __future__ import annotations

import asyncio
import os
import struct
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from uuid import uuid4

import pytest
from fastapi import WebSocketDisconnect
from sqlalchemy import func, select

import app.api.live as live_mod
import app.api.meetings as meetings_mod
from app.api.live import finalize_live_meeting, get_coordination, reset_coordination
from app.config import Settings, get_settings
from app.db import get_session, make_engine, make_session_factory
from app.enums import JobStatus
from app.main import create_app
from app.models import MeetingRecord, ProcessingJob, Transcript, User, create_all
from app.security.passwords import hash_password
from app.security.sessions import issue_session

PASSWORD = "correct-horse-battery-staple"


class _EnqueueSpy:
    def __init__(self) -> None:
        self.calls: list[str] = []

    def __call__(self, meeting_id: str) -> None:
        self.calls.append(meeting_id)


def _seq(seq: int, payload: bytes) -> bytes:
    return struct.pack("<I", seq) + payload


@contextmanager
def _harness() -> Iterator[tuple]:
    from fastapi.testclient import TestClient

    work_dir = tempfile.mkdtemp(prefix="live_ws_")
    audio_dir = os.path.join(work_dir, "audio")
    os.makedirs(audio_dir, exist_ok=True)
    db_path = os.path.join(work_dir, "app.db")
    engine = make_engine(f"sqlite:///{db_path}")
    create_all(engine)
    sf = make_session_factory(engine)

    s = sf()
    try:
        owner = User(username="ws-owner", password_hash=hash_password(PASSWORD))
        stranger = User(username="ws-stranger", password_hash=hash_password(PASSWORD))
        s.add_all([owner, stranger])
        s.commit()
        owner_id, stranger_id = owner.id, stranger.id
        token = issue_session(s, owner_id, ttl_minutes=60).token
        stranger_token = issue_session(s, stranger_id, ttl_minutes=60).token
        s.commit()
    finally:
        s.close()

    test_settings = Settings(
        bootstrap_admin_username="admin",
        bootstrap_admin_password="bootstrap-pass",
        redis_url="redis://localhost:6379/0",
        database_url=f"sqlite:///{db_path}",
        audio_storage_dir=audio_dir,
        streaming_asr_impl="mock",
    )  # type: ignore[call-arg]

    reset_coordination()
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
            yield client, sf, audio_dir, owner_id, token, stranger_token, spy
    finally:
        app.dependency_overrides.clear()
        live_mod._get_session_factory = saved_factory
        meetings_mod._enqueue_processing_job = saved_enqueue
        engine.dispose()


def _seed_live(sf, audio_dir: str, owner_id: str) -> str:
    mid = str(uuid4())
    s = sf()
    try:
        s.add(
            MeetingRecord(
                id=mid,
                owner_id=owner_id,
                title="会议-x",
                source_audio_path=os.path.join(audio_dir, f"{mid}.webm"),
                source_format="webm",
            )
        )
        s.add(
            ProcessingJob(
                meeting_id=mid,
                status=JobStatus.LIVE.value,
                owner_id=owner_id,
                last_seq=None,
                recording_bytes=0,
            )
        )
        s.commit()
        return mid
    finally:
        s.close()


def _job(sf, mid: str) -> ProcessingJob:
    s = sf()
    try:
        return s.execute(
            select(ProcessingJob).where(ProcessingJob.meeting_id == mid)
        ).scalar_one()
    finally:
        s.close()


def _url(mid: str) -> str:
    return f"/api/meetings/{mid}/live"


def _subs(token: str) -> list[str]:
    return ["mm.live.v1", token]


# Feature: realtime-meeting-transcription, Property 31: Streaming_Connection rejects unauthenticated access.
# Validates: Requirements 2.6
def test_property_31_ws_rejects_unauthenticated_and_foreign_and_nonlive() -> None:
    with _harness() as (client, sf, audio_dir, owner_id, token, stranger_token, spy):
        mid = _seed_live(sf, audio_dir, owner_id)

        # Absent token (only the version subprotocol).
        with pytest.raises(WebSocketDisconnect):
            with client.websocket_connect(_url(mid), subprotocols=["mm.live.v1"]) as ws:
                ws.receive_json()
        # Unknown token.
        with pytest.raises(WebSocketDisconnect):
            with client.websocket_connect(_url(mid), subprotocols=_subs("bogus")) as ws:
                ws.receive_json()
        # Valid token but a foreign user (stranger does not own the meeting).
        with pytest.raises(WebSocketDisconnect):
            with client.websocket_connect(
                _url(mid), subprotocols=_subs(stranger_token)
            ) as ws:
                ws.receive_json()
        # Valid + owned but the meeting is not live.
        s = sf()
        try:
            job = s.execute(
                select(ProcessingJob).where(ProcessingJob.meeting_id == mid)
            ).scalar_one()
            job.status = JobStatus.QUEUED.value
            s.commit()
        finally:
            s.close()
        with pytest.raises(WebSocketDisconnect):
            with client.websocket_connect(_url(mid), subprotocols=_subs(token)) as ws:
                ws.receive_json()


def test_ws_happy_path_resume_relay_and_end_finalizes() -> None:
    with _harness() as (client, sf, audio_dir, owner_id, token, stranger_token, spy):
        mid = _seed_live(sf, audio_dir, owner_id)
        with client.websocket_connect(_url(mid), subprotocols=_subs(token)) as ws:
            resume = ws.receive_json()
            assert resume == {"type": "resume", "next_seq": 0}
            # Send the header increment (seq 0) and the next (seq 1).
            ws.send_bytes(_seq(0, b"HEADERCHUNK"))
            frame = ws.receive_json()  # Property 38: relayed transcript
            assert frame["type"] == "transcript"
            assert isinstance(frame["result"]["segments"], list)
            ws.send_bytes(_seq(1, b"CLUSTER-1"))
            ws.receive_json()
            # End the meeting.
            ws.send_text('{"type": "end"}')
        # Finalized to queued and enqueued exactly once.
        assert _job(sf, mid).status == JobStatus.QUEUED.value
        assert spy.calls == [mid]
        # The recording is the in-order concatenation of the increments.
        with open(os.path.join(audio_dir, f"{mid}.webm"), "rb") as fh:
            assert fh.read() == b"HEADERCHUNK" + b"CLUSTER-1"


# Feature: realtime-meeting-transcription, Property 34: The captured recording is the exactly-once, sequence-ordered concatenation of all increments.
# Feature: realtime-meeting-transcription, Property 43: Idempotent-by-sequence append and resume restore a complete, in-order recording.
# Validates: Requirements 5.1, 5.5, 5.6, 6.1
def test_property_34_43_idempotent_append_and_gap_resume() -> None:
    with _harness() as (client, sf, audio_dir, owner_id, token, stranger_token, spy):
        mid = _seed_live(sf, audio_dir, owner_id)
        with client.websocket_connect(_url(mid), subprotocols=_subs(token)) as ws:
            assert ws.receive_json()["next_seq"] == 0
            ws.send_bytes(_seq(0, b"AAA"))
            ws.receive_json()
            ws.send_bytes(_seq(1, b"BBB"))
            ws.receive_json()
            # Re-deliver seq 1 (a reconnect replay): ignored, no duplicate bytes,
            # and no transcript frame is produced -> verify via a follow-up gap.
            ws.send_bytes(_seq(1, b"BBB"))
            # A gap (seq 5 when 2 is expected) triggers a resume re-request.
            ws.send_bytes(_seq(5, b"ZZZ"))
            resume = ws.receive_json()
            assert resume == {"type": "resume", "next_seq": 2}
            ws.send_text('{"type": "end"}')
        job = _job(sf, mid)
        assert job.last_seq == 1
        assert job.recording_bytes == 6  # "AAA"+"BBB"; duplicate/gap not appended
        with open(os.path.join(audio_dir, f"{mid}.webm"), "rb") as fh:
            assert fh.read() == b"AAABBB"


# Feature: realtime-meeting-transcription, Property 39: Streaming-ASR failure degrades gracefully without ending the session.
# Validates: Requirements 5.2
def test_property_39_graceful_degradation_keeps_session_live_and_persists() -> None:
    from app.clients.streaming.base import StreamingAsrError

    class _FailingSession:
        async def push(self, audio_increment: bytes):  # noqa: ANN201
            raise StreamingAsrError("boom")

        async def aclose(self) -> None:
            return None

    class _FailingClient:
        def open_session(self, **kwargs):  # noqa: ANN003, ANN201
            return _FailingSession()

    with _harness() as (client, sf, audio_dir, owner_id, token, stranger_token, spy):
        mid = _seed_live(sf, audio_dir, owner_id)
        saved = live_mod.make_streaming_asr_client
        live_mod.make_streaming_asr_client = lambda settings: _FailingClient()
        try:
            with client.websocket_connect(_url(mid), subprotocols=_subs(token)) as ws:
                assert ws.receive_json()["next_seq"] == 0
                ws.send_bytes(_seq(0, b"AUDIO"))
                frame = ws.receive_json()
                assert frame["type"] == "asr_unavailable"
                # Session stays live; audio is still persisted on the fast path.
                assert _job(sf, mid).status == JobStatus.LIVE.value
                with open(os.path.join(audio_dir, f"{mid}.webm"), "rb") as fh:
                    assert fh.read() == b"AUDIO"
        finally:
            live_mod.make_streaming_asr_client = saved


def test_asr_recovers_and_sends_asr_available() -> None:
    """After a transient StreamingAsrError, a later success clears the flag."""
    from app.clients.streaming.base import StreamingAsrError

    class _FlakySession:
        def __init__(self) -> None:
            self.n = 0
            self.headers: list[bytes | None] = []

        def set_header_bytes(self, header_bytes: bytes | None) -> None:
            self.headers.append(header_bytes)

        async def push(self, audio_increment: bytes):  # noqa: ANN201
            self.n += 1
            if self.n == 1:
                raise StreamingAsrError("transient")
            return [
                {
                    "segments": [
                        {
                            "start": 0.0,
                            "end": 0.5,
                            "text": "ok",
                            "speaker": None,
                            "is_final": True,
                        }
                    ]
                }
            ]

        async def aclose(self) -> None:
            return None

    class _FlakyClient:
        def __init__(self) -> None:
            self.session = _FlakySession()

        def open_session(self, **kwargs):  # noqa: ANN003, ANN201
            return self.session

    with _harness() as (client, sf, audio_dir, owner_id, token, stranger_token, spy):
        mid = _seed_live(sf, audio_dir, owner_id)
        flaky = _FlakyClient()
        saved = live_mod.make_streaming_asr_client
        live_mod.make_streaming_asr_client = lambda settings: flaky
        try:
            with client.websocket_connect(_url(mid), subprotocols=_subs(token)) as ws:
                assert ws.receive_json()["next_seq"] == 0
                ws.send_bytes(_seq(0, b"HDR"))
                assert ws.receive_json()["type"] == "asr_unavailable"
                assert flaky.session.headers == [b"HDR"]
                ws.send_bytes(_seq(1, b"BODY"))
                assert ws.receive_json() == {"type": "asr_available"}
                frame = ws.receive_json()
                assert frame["type"] == "transcript"
                assert frame["result"]["segments"][0]["text"] == "ok"
        finally:
            live_mod.make_streaming_asr_client = saved


# Feature: realtime-meeting-transcription, Property 37: The live preview is never persisted or exposed as the final Transcript.
# Validates: Requirements 6.7
def test_property_37_live_path_persists_no_transcript() -> None:
    with _harness() as (client, sf, audio_dir, owner_id, token, stranger_token, spy):
        mid = _seed_live(sf, audio_dir, owner_id)
        with client.websocket_connect(_url(mid), subprotocols=_subs(token)) as ws:
            ws.receive_json()
            ws.send_bytes(_seq(0, b"AAA"))
            ws.receive_json()
            ws.send_text('{"type": "end"}')
        # No Transcript row exists yet -- only the (mock) batch pipeline would
        # create one, and it has not run here (enqueue is a spy).
        s = sf()
        try:
            assert (
                s.execute(select(func.count()).select_from(Transcript)).scalar_one()
                == 0
            )
        finally:
            s.close()


# Feature: realtime-meeting-transcription, Property 44: At most one actor writes a meeting's recording at any time (appends and finalization are mutually exclusive).
# Validates: Requirements 5.1, 6.1
def test_property_44_finalize_is_mutually_exclusive_with_append_lock() -> None:
    with _harness() as (client, sf, audio_dir, owner_id, token, stranger_token, spy):
        mid = _seed_live(sf, audio_dir, owner_id)
        # Write a recording so finalize would queue (not 0-byte).
        with open(os.path.join(audio_dir, f"{mid}.webm"), "wb") as fh:
            fh.write(b"\x00" * 16)
        s = sf()
        try:
            job = s.execute(
                select(ProcessingJob).where(ProcessingJob.meeting_id == mid)
            ).scalar_one()
            job.last_seq = 0
            job.recording_bytes = 16
            s.commit()
        finally:
            s.close()

        async def _scenario() -> None:
            coord = get_coordination()
            lock = coord.append_lock(mid)
            async with lock:
                # While the append lock is held, finalize must block (it takes
                # the same lock) -- appends and finalize are mutually exclusive.
                task = asyncio.create_task(
                    finalize_live_meeting(mid, session_factory=sf)
                )
                await asyncio.sleep(0.05)
                assert not task.done()  # blocked on the lock
            result = await task  # released -> finalize proceeds
            assert result is JobStatus.QUEUED

        asyncio.run(_scenario())
        assert _job(sf, mid).status == JobStatus.QUEUED.value
        assert spy.calls == [mid]


def test_ws_supersede_bumps_generation() -> None:
    with _harness() as (client, sf, audio_dir, owner_id, token, stranger_token, spy):
        mid = _seed_live(sf, audio_dir, owner_id)
        coord = get_coordination()
        with client.websocket_connect(_url(mid), subprotocols=_subs(token)) as ws1:
            ws1.receive_json()
            gen_after_first = coord._generation.get(mid)
            with client.websocket_connect(_url(mid), subprotocols=_subs(token)) as ws2:
                ws2.receive_json()
                gen_after_second = coord._generation.get(mid)
                # The second connection claimed a newer generation -> the first
                # is no longer current (superseded, single-writer).
                assert gen_after_second > gen_after_first
                assert not coord.is_current_connection(mid, gen_after_first)
                ws2.send_text('{"type": "end"}')
