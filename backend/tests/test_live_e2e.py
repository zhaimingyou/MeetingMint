"""End-to-end live -> batch flow with mocks (task 13).

Drives the COMPLETE live experience in-process against ``create_app()`` with the
deterministic mock streaming ASR + mock batch ASR/LLM and a fake preprocessor
(no ffmpeg / real models / Redis / Docker):

    login -> start live -> WS connect -> stream increments -> end
          -> (finalize: live->queued) -> inline batch pipeline -> completed
          -> status/detail expose the Transcript + Meeting_Minutes

The Nginx WebSocket-upgrade + full compose bring-up is the Docker-guarded smoke
(skipped without Docker); see tests/test_deploy_live.py for the static checks.

Switching to the REAL services needs NO code change -- set ``STREAMING_ASR_IMPL=http``
(+ ``STREAMING_ASR_ENDPOINT_URL``), ``ASR_IMPL=http`` / ``LLM_IMPL=http`` (+ their
endpoints/model) in the environment; the factories pick up the HTTP clients.
"""

from __future__ import annotations

import os
import struct
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager

from fastapi.testclient import TestClient

import app.api.live as live_mod
import app.api.meetings as meetings_mod
from app.config import Settings, get_settings
from app.db import get_session, make_engine, make_session_factory
from app.enums import JobStatus
from app.main import create_app
from app.models import User, create_all
from app.preprocess import standardized_wav_path  # noqa: F401 (documents the real path helper)
from app.security.passwords import hash_password
from app.security.sessions import issue_session
from app.worker.tasks import run_pipeline

USERNAME = "e2e-live"
PASSWORD = "correct-horse-battery-staple"


def _fake_preprocess(source_path: str, output_path: str, timeout_seconds=None) -> str:
    os.makedirs(os.path.dirname(output_path), exist_ok=True)
    with open(output_path, "wb") as fh:
        fh.write(b"FAKE-STD-WAV\x00\x01\x02")
    return output_path


def _seq(seq: int, payload: bytes) -> bytes:
    return struct.pack("<I", seq) + payload


@contextmanager
def _e2e() -> Iterator[tuple]:
    work = tempfile.mkdtemp(prefix="live_e2e_")
    audio_dir = os.path.join(work, "audio")
    os.makedirs(audio_dir, exist_ok=True)
    db_path = os.path.join(work, "app.db")
    engine = make_engine(f"sqlite:///{db_path}")
    create_all(engine)
    sf = make_session_factory(engine)

    s = sf()
    try:
        s.add(User(username=USERNAME, password_hash=hash_password(PASSWORD)))
        s.commit()
    finally:
        s.close()

    settings = Settings(
        bootstrap_admin_username="admin",
        bootstrap_admin_password="bootstrap-pass",
        redis_url="redis://localhost:6379/0",
        database_url=f"sqlite:///{db_path}",
        audio_storage_dir=audio_dir,
        streaming_asr_impl="mock",
        asr_impl="mock",
        llm_impl="mock",
    )  # type: ignore[call-arg]

    live_mod.reset_coordination()
    saved_factory = live_mod._get_session_factory
    live_mod._get_session_factory = lambda: sf
    saved_enqueue = meetings_mod._enqueue_processing_job

    # End-of-meeting finalize enqueues the pipeline; run it inline (mock clients
    # + fake ffmpeg) so the meeting reaches `completed` synchronously.
    def _run_inline(meeting_id: str) -> None:
        run_pipeline(
            meeting_id,
            settings=settings,
            session_factory=sf,
            preprocess_fn=_fake_preprocess,
        )

    meetings_mod._enqueue_processing_job = _run_inline

    app = create_app()

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
            yield client, sf, audio_dir
    finally:
        app.dependency_overrides.clear()
        live_mod._get_session_factory = saved_factory
        meetings_mod._enqueue_processing_job = saved_enqueue
        engine.dispose()


def test_full_live_to_batch_flow_with_mocks() -> None:
    with _e2e() as (client, sf, audio_dir):
        # 1. Login.
        login = client.post(
            "/api/auth/login", json={"username": USERNAME, "password": PASSWORD}
        )
        assert login.status_code == 200, login.text
        token = login.json()["token"]
        auth = {"Authorization": f"Bearer {token}"}

        # 2. Start a live meeting.
        start = client.post("/api/meetings/live", headers=auth)
        assert start.status_code == 201, start.text
        mid = start.json()["id"]

        # 3. Stream increments over the WebSocket, then end.
        with client.websocket_connect(
            f"/api/meetings/{mid}/live", subprotocols=["mm.live.v1", token]
        ) as ws:
            assert ws.receive_json() == {"type": "resume", "next_seq": 0}
            ws.send_bytes(_seq(0, b"WEBM-HEADER+CLUSTER0"))
            ws.receive_json()  # transcript (mock)
            ws.send_bytes(_seq(1, b"CLUSTER1"))
            ws.receive_json()
            ws.send_text('{"type": "end"}')

        # 4. End finalized the session and the inline pipeline ran to completion.
        status = client.get(f"/api/meetings/{mid}/status", headers=auth)
        assert status.status_code == 200
        assert status.json()["status"] == JobStatus.COMPLETED.value

        # 5. Detail exposes the final Transcript + Meeting_Minutes (from the
        #    batch pass -- NOT the live preview).
        detail = client.get(f"/api/meetings/{mid}", headers=auth).json()
        assert detail["status"] == JobStatus.COMPLETED.value
        assert detail["transcript"] is not None
        assert len(detail["transcript"]["segments"]) >= 1
        assert detail["minutes"] is not None
        assert detail["minutes"]["summary"]

        # 6. The recording on disk is the in-order concatenation of increments.
        with open(os.path.join(audio_dir, f"{mid}.webm"), "rb") as fh:
            assert fh.read() == b"WEBM-HEADER+CLUSTER0" + b"CLUSTER1"

        # 7. Export works for the live-origin meeting.
        export = client.get(f"/api/meetings/{mid}/export", headers=auth)
        assert export.status_code == 200
        assert export.headers["content-type"].startswith("text/markdown")
