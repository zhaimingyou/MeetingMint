"""Live-origin batch handoff verification (task 12).

A Meeting_Record that reaches ``queued`` via ``finalize_live_meeting`` (a
complete ``.webm`` recording) must be **indistinguishable** from an
upload-origin meeting once queued: the unchanged ``process_meeting`` pipeline
and the read endpoints behave identically. This exercises the **existing**
meeting-minutes-service properties against a live-origin fixture (no new
pipeline code):

- **Property 17** -- stage ordering ``queued -> preprocessing -> transcribing ->
  generating -> completed`` and persisted Transcript/Minutes.
- **Property 21** -- listing includes the meeting with title/status/created_at.
- **Property 22 / 23** -- detail exposes Transcript/Minutes once persisted and
  is owner-scoped (foreign -> 404).
"""

from __future__ import annotations

import asyncio
import os
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from uuid import uuid4

from fastapi.testclient import TestClient

import app.api.live as live_mod
import app.api.meetings as meetings_mod
from app.api.live import finalize_live_meeting, reset_coordination
from app.config import Settings, get_settings
from app.db import get_session, make_engine, make_session_factory
from app.enums import JobStatus
from app.models import MeetingRecord, ProcessingJob, User, create_all
from app.security.passwords import hash_password
from app.security.sessions import issue_session
from app.worker.tasks import run_pipeline

PASSWORD = "correct-horse-battery-staple"


def _fake_preprocess(source_path: str, output_path: str, timeout_seconds=None) -> str:
    os.makedirs(os.path.dirname(output_path), exist_ok=True)
    with open(output_path, "wb") as fh:
        fh.write(b"FAKE-STD-WAV\x00\x01")
    return output_path


@contextmanager
def _harness() -> Iterator[tuple]:
    work = tempfile.mkdtemp(prefix="live_handoff_")
    audio_dir = os.path.join(work, "audio")
    os.makedirs(audio_dir, exist_ok=True)
    db_path = os.path.join(work, "app.db")
    engine = make_engine(f"sqlite:///{db_path}")
    create_all(engine)
    sf = make_session_factory(engine)

    s = sf()
    try:
        owner = User(username="handoff-owner", password_hash=hash_password(PASSWORD))
        stranger = User(username="handoff-stranger", password_hash=hash_password(PASSWORD))
        s.add_all([owner, stranger])
        s.commit()
        owner_id = owner.id
        token = issue_session(s, owner_id, ttl_minutes=60).token
        stranger_token = issue_session(s, stranger.id, ttl_minutes=60).token
        s.commit()
    finally:
        s.close()

    settings = Settings(
        bootstrap_admin_username="admin",
        bootstrap_admin_password="bootstrap-pass",
        redis_url="redis://localhost:6379/0",
        database_url=f"sqlite:///{db_path}",
        audio_storage_dir=audio_dir,
        asr_impl="mock",
        llm_impl="mock",
    )  # type: ignore[call-arg]

    reset_coordination()
    saved_factory = live_mod._get_session_factory
    live_mod._get_session_factory = lambda: sf
    saved_enqueue = meetings_mod._enqueue_processing_job
    # The enqueue seam runs the (mock) pipeline inline so finalize -> completed.
    captured: dict = {}

    def _run_inline(meeting_id: str) -> None:
        captured["transitions"] = []
        run_pipeline(
            meeting_id,
            settings=settings,
            session_factory=sf,
            preprocess_fn=_fake_preprocess,
            status_observer=lambda st: captured["transitions"].append(st),
        )

    meetings_mod._enqueue_processing_job = _run_inline

    app = create_app_local()

    def _override_get_session() -> Iterator:
        sess = sf()
        try:
            yield sess
        finally:
            sess.close()

    app.dependency_overrides[get_session] = _override_get_session
    app.dependency_overrides[get_settings] = lambda: settings
    try:
        with TestClient(app) as client:
            yield client, sf, audio_dir, owner_id, token, stranger_token, captured
    finally:
        app.dependency_overrides.clear()
        live_mod._get_session_factory = saved_factory
        meetings_mod._enqueue_processing_job = saved_enqueue
        engine.dispose()


def create_app_local():
    from app.main import create_app

    return create_app()


def _seed_live_with_recording(sf, audio_dir, owner_id) -> str:
    mid = str(uuid4())
    path = os.path.join(audio_dir, f"{mid}.webm")
    with open(path, "wb") as fh:
        fh.write(b"\x00" * 4096)
    s = sf()
    try:
        s.add(
            MeetingRecord(
                id=mid, owner_id=owner_id, title="会议-handoff",
                source_audio_path=path, source_format="webm",
            )
        )
        s.add(
            ProcessingJob(
                meeting_id=mid, status=JobStatus.LIVE.value, owner_id=owner_id,
                last_seq=0, recording_bytes=4096,
            )
        )
        s.commit()
        return mid
    finally:
        s.close()


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def test_live_origin_meeting_completes_via_unchanged_pipeline_and_reads() -> None:
    """Finalize a live meeting, then the existing pipeline + endpoints behave as
    they do for an upload (reuses Properties 17/21/22/23)."""
    with _harness() as (client, sf, audio_dir, owner_id, token, stranger_token, captured):
        mid = _seed_live_with_recording(sf, audio_dir, owner_id)

        # End the live session -> queued + inline (mock) pipeline runs to completed.
        result = asyncio.run(finalize_live_meeting(mid, session_factory=sf))
        assert result is JobStatus.QUEUED

        # Property 17: the pipeline advanced through the standard batch stages.
        assert captured["transitions"] == [
            JobStatus.PREPROCESSING,
            JobStatus.TRANSCRIBING,
            JobStatus.GENERATING,
            JobStatus.COMPLETED,
        ]

        # Status endpoint reports completed.
        status_resp = client.get(f"/api/meetings/{mid}/status", headers=_auth(token))
        assert status_resp.status_code == 200
        assert status_resp.json()["status"] == JobStatus.COMPLETED.value

        # Property 21: listing includes the live-origin meeting.
        listing = client.get("/api/meetings", headers=_auth(token)).json()
        assert any(m["id"] == mid for m in listing)

        # Property 22: detail exposes the persisted Transcript + Minutes.
        detail = client.get(f"/api/meetings/{mid}", headers=_auth(token)).json()
        assert detail["status"] == JobStatus.COMPLETED.value
        assert detail["transcript"] is not None
        assert len(detail["transcript"]["segments"]) >= 1
        assert detail["minutes"] is not None
        assert detail["minutes"]["summary"]

        # Property 23: a foreign user cannot see it (owner-scoped 404).
        assert (
            client.get(f"/api/meetings/{mid}", headers=_auth(stranger_token)).status_code
            == 404
        )

        # Export works too (a live-origin meeting is indistinguishable here).
        export = client.get(f"/api/meetings/{mid}/export", headers=_auth(token))
        assert export.status_code == 200
        assert export.headers["content-type"].startswith("text/markdown")
