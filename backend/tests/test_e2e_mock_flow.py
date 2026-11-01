"""End-to-end "happy path" with the mock pipeline -- task 15.2.

Exercises the COMPLETE user flow in-process against ``create_app()`` with the
deterministic mock ASR/LLM clients (``asr_impl=mock`` / ``llm_impl=mock``) and a
fake preprocessor (so no ffmpeg/ASR/LLM/Redis/Docker are needed):

    login -> upload -> (pipeline runs) -> poll status -> list -> view detail
    -> edit minutes -> export (Markdown) -> delete

This is the in-process complement to the live, through-Nginx compose bring-up
(task 14 / the Docker-guarded smoke test), which requires Docker. It verifies the
real HTTP request/response wiring across every Backend_API endpoint built in
tasks 3, 8, 10 and 11, with the worker pipeline (task 9) driven synchronously
via the upload enqueue seam.

How the async pipeline is made synchronous
------------------------------------------
The upload endpoint enqueues the Celery ``process_meeting`` task through the
module-level seam ``app.api.meetings._enqueue_processing_job``. Here that seam is
replaced with a function that runs :func:`app.worker.tasks.run_pipeline`
*inline* on the same in-memory engine, with the mock clients (selected by the
mock settings) and a fake ``preprocess_fn`` that writes a small standardized
file in place of ffmpeg. So by the time the upload response returns, the job has
already advanced ``queued -> preprocessing -> transcribing -> generating ->
completed`` and the Transcript + Meeting_Minutes are persisted.
"""

from __future__ import annotations

import os
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager

from fastapi.testclient import TestClient

import app.api.meetings as meetings_mod
from app.config import Settings, get_settings
from app.db import get_session, make_engine, make_session_factory
from app.enums import JobStatus
from app.models import (
    ActionItem,
    MeetingMinutes,
    MeetingRecord,
    ProcessingJob,
    Transcript,
    User,
    create_all,
)
from app.preprocess import standardized_wav_path
from app.security.passwords import hash_password
from app.worker.tasks import run_pipeline
from sqlalchemy import func, select

USERNAME = "e2e-user"
PASSWORD = "e2e-correct-horse-battery"


def _fake_preprocess(source_path: str, output_path: str, timeout_seconds=None) -> str:
    """Stand in for ffmpeg: write a small deterministic standardized file."""
    os.makedirs(os.path.dirname(output_path), exist_ok=True)
    with open(output_path, "wb") as fh:
        fh.write(b"FAKE-STANDARDIZED-MONO-16K-WAV\x00\x01\x02\x03")
    return output_path


@contextmanager
def _e2e_app() -> Iterator[tuple[TestClient, Settings, object]]:
    """Yield (client, settings, session_factory) wired for the mock E2E flow."""
    # A file-based SQLite (not :memory:) so the API request session and the
    # inline pipeline's own sessions share the same database via normal pooling,
    # without in-memory single-connection contention.
    work_dir = tempfile.mkdtemp(prefix="e2e_")
    db_path = os.path.join(work_dir, "app.db")
    audio_dir = os.path.join(work_dir, "audio")
    os.makedirs(audio_dir, exist_ok=True)
    database_url = f"sqlite:///{db_path}"

    engine = make_engine(database_url)
    create_all(engine)
    session_factory = make_session_factory(engine)

    # Seed the login account (real bcrypt hash; conftest lowers the cost factor).
    seed = session_factory()
    try:
        seed.add(User(username=USERNAME, password_hash=hash_password(PASSWORD)))
        seed.commit()
    finally:
        seed.close()

    test_settings = Settings(
        bootstrap_admin_username="admin",
        bootstrap_admin_password="bootstrap-pass",
        redis_url="redis://localhost:6379/0",
        database_url=database_url,
        asr_impl="mock",
        llm_impl="mock",
        audio_storage_dir=audio_dir,
    )  # type: ignore[call-arg]

    # Replace the upload's enqueue seam with an inline pipeline run on the same
    # engine, using the mock clients (via mock settings) and the fake ffmpeg.
    saved_enqueue = meetings_mod._enqueue_processing_job

    def _run_inline(meeting_id: str) -> None:
        run_pipeline(
            meeting_id,
            settings=test_settings,
            session_factory=session_factory,
            preprocess_fn=_fake_preprocess,
        )

    meetings_mod._enqueue_processing_job = _run_inline

    from app.main import create_app

    fastapi_app = create_app()

    def _override_get_session() -> Iterator:
        s = session_factory()
        try:
            yield s
        finally:
            s.close()

    fastapi_app.dependency_overrides[get_session] = _override_get_session
    fastapi_app.dependency_overrides[get_settings] = lambda: test_settings

    try:
        with TestClient(fastapi_app) as client:
            yield client, test_settings, session_factory
    finally:
        fastapi_app.dependency_overrides.clear()
        meetings_mod._enqueue_processing_job = saved_enqueue
        engine.dispose()


def _login(client: TestClient) -> str:
    resp = client.post(
        "/api/auth/login", json={"username": USERNAME, "password": PASSWORD}
    )
    assert resp.status_code == 200, resp.text
    return resp.json()["token"]


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def test_full_flow_login_upload_process_view_edit_export_delete() -> None:
    """The whole mock pipeline flow works end to end through the real HTTP API."""
    with _e2e_app() as (client, settings, session_factory):
        # --- 1. Login (Req 1.2) ------------------------------------------
        token = _login(client)
        auth = _auth(token)

        # --- 2. Upload an audio file (Req 3.1 / 3.6 / 7.1) ---------------
        # ffprobe is not installed; stand in for content-based detection.
        meetings_mod.detect_format = lambda path: "wav"
        upload = client.post(
            "/api/meetings",
            files={"file": ("meeting.wav", b"RIFF....fake-audio-bytes....", "audio/wav")},
            headers=auth,
        )
        assert upload.status_code == 201, upload.text
        meeting_id = upload.json()["id"]
        source_path = os.path.join(settings.audio_storage_dir, f"{meeting_id}.wav")
        assert os.path.isfile(source_path)  # source stored on disk

        # --- 3. The inline pipeline already advanced the job to completed -
        status = client.get(f"/api/meetings/{meeting_id}/status", headers=auth)
        assert status.status_code == 200
        assert status.json()["status"] == JobStatus.COMPLETED.value

        # --- 4. List shows the meeting (owner-scoped) (Req 8.1) ----------
        listing = client.get("/api/meetings", headers=auth)
        assert listing.status_code == 200
        ids = [m["id"] for m in listing.json()]
        assert meeting_id in ids

        # --- 5. Detail includes the persisted transcript + minutes (Req 8.2/9.1)
        detail = client.get(f"/api/meetings/{meeting_id}", headers=auth).json()
        assert detail["status"] == JobStatus.COMPLETED.value
        assert detail["transcript"] is not None
        assert len(detail["transcript"]["segments"]) >= 1
        assert detail["minutes"] is not None
        assert isinstance(detail["minutes"]["summary"], str) and detail["minutes"]["summary"]

        # --- 6. Edit the minutes; the edit round-trips (Req 9.2 / 9.6) ---
        edited = {
            "summary": "修订后的会议摘要。",
            "topics": ["议题一", "议题二"],
            "decisions": ["决议一"],
            "action_items": [
                {"description": "完成对接", "owner": "张三"},
                {"description": "整理纪要", "owner": ""},  # blank -> 未指派
            ],
        }
        put = client.put(
            f"/api/meetings/{meeting_id}/minutes", json=edited, headers=auth
        )
        assert put.status_code == 200, put.text
        after = client.get(f"/api/meetings/{meeting_id}", headers=auth).json()["minutes"]
        assert after["summary"] == "修订后的会议摘要。"
        assert after["topics"] == ["议题一", "议题二"]
        assert after["decisions"] == ["决议一"]
        owners = {item["owner"] for item in after["action_items"]}
        assert "张三" in owners
        assert "未指派" in owners  # blank owner normalized

        # --- 7. Export as Markdown with every section (Req 9.3) ----------
        export = client.get(f"/api/meetings/{meeting_id}/export", headers=auth)
        assert export.status_code == 200
        assert export.headers["content-type"].startswith("text/markdown")
        assert ".md" in export.headers.get("content-disposition", "")
        body = export.text
        assert "修订后的会议摘要。" in body
        assert "议题一" in body and "议题二" in body
        assert "决议一" in body
        assert "完成对接" in body and "张三" in body
        assert "整理纪要" in body and "未指派" in body

        # --- 8. Delete removes the record + audio + transcript + minutes (Req 9.4)
        delete = client.delete(f"/api/meetings/{meeting_id}", headers=auth)
        assert delete.status_code == 200
        assert delete.json()["deleted"] is True
        # Subsequent fetch is a 404 (gone, not disclosed).
        assert client.get(f"/api/meetings/{meeting_id}", headers=auth).status_code == 404
        # Audio file removed from disk.
        assert not os.path.exists(source_path)
        # Every owned table row is gone (cascade).
        check = session_factory()
        try:
            assert check.execute(select(func.count()).select_from(MeetingRecord)).scalar_one() == 0
            assert check.execute(select(func.count()).select_from(ProcessingJob)).scalar_one() == 0
            assert check.execute(select(func.count()).select_from(Transcript)).scalar_one() == 0
            assert check.execute(select(func.count()).select_from(MeetingMinutes)).scalar_one() == 0
            assert check.execute(select(func.count()).select_from(ActionItem)).scalar_one() == 0
        finally:
            check.close()


def test_failed_job_can_be_resubmitted_then_completes() -> None:
    """A failed stage surfaces an error, and resubmit re-runs to completion (Req 7.6)."""
    with _e2e_app() as (client, settings, session_factory):
        token = _login(client)
        auth = _auth(token)
        meetings_mod.detect_format = lambda path: "wav"

        # First run: force the preprocessor to fail so the job ends 'failed'.
        from app.preprocess import PreprocessError

        def _boom(source_path: str, output_path: str, timeout_seconds=None) -> str:
            raise PreprocessError("decode failure: unreadable source")

        def _run_failing(meeting_id: str) -> None:
            run_pipeline(
                meeting_id,
                settings=settings,
                session_factory=session_factory,
                preprocess_fn=_boom,
            )

        saved = meetings_mod._enqueue_processing_job
        meetings_mod._enqueue_processing_job = _run_failing
        try:
            up = client.post(
                "/api/meetings",
                files={"file": ("m.wav", b"bytes-here-xyz", "audio/wav")},
                headers=auth,
            )
        finally:
            meetings_mod._enqueue_processing_job = saved
        meeting_id = up.json()["id"]

        status = client.get(f"/api/meetings/{meeting_id}/status", headers=auth).json()
        assert status["status"] == JobStatus.FAILED.value
        assert status.get("error_description")  # error surfaced (Req 7.5)

        # Resubmit re-enqueues; here the seam runs the (now-succeeding) pipeline.
        resub = client.post(f"/api/meetings/{meeting_id}/resubmit", headers=auth)
        assert resub.status_code == 200
        final = client.get(f"/api/meetings/{meeting_id}/status", headers=auth).json()
        assert final["status"] == JobStatus.COMPLETED.value
