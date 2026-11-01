"""Property-based tests for the upload endpoint -- task 8.5.

These ``hypothesis`` property tests cover design Properties 5, 6, and 7 for
``POST /api/meetings`` (Req 2.4 / 3.1 / 3.2 / 3.3 / 3.4 / 3.5 / 3.6). They are
distinct from the example/integration tests in
``tests/test_meetings_atomic_create.py`` (task 8.3) and
``tests/test_meetings_enqueue.py`` (task 8.4): here we assert *universal*
properties across many generated inputs (varied sizes, filenames, formats,
boundary conditions, and induced failures) rather than fixed examples.

Each property test runs >= 100 examples and is tagged with the exact design
property text plus its ``Validates`` requirement references.

Per-example isolation
---------------------
``hypothesis`` disallows function-scoped fixtures under ``@given`` (each example
would otherwise share fixture state, invalidating the "exactly one row" and
"no leftover file" assertions). Instead, every example builds a **fresh,
fully isolated harness** inside the test body via :func:`_upload_harness`:

- a brand-new in-memory SQLite engine + schema (so the meetings/jobs tables
  start empty for that example),
- a brand-new temporary ``audio_storage_dir`` on disk (so file assertions see
  only what this example wrote), and
- a freshly seeded user + valid bearer token.

The injectable seams on ``app.api.meetings`` (``detect_format``,
``_enqueue_processing_job``, ``_fsync_and_move``, ``_persist_meeting_record``)
are patched within the harness and restored on exit, so no real ffprobe binary
or Celery/Redis broker is needed and induced failures are deterministic. The
harness tears everything down (engine disposed, temp dir removed, dependency
overrides cleared, seams restored) after each example, guaranteeing no
cross-example state.
"""

from __future__ import annotations

import io
import os
import shutil
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager

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

USERNAME = "prop-uploader"
PASSWORD = "correct-horse-battery-staple"
SUPPORTED_FORMATS = ["mp3", "wav", "m4a", "webm"]


# --------------------------------------------------------------------------- #
# Harness: a fresh, isolated upload environment per generated example.
# --------------------------------------------------------------------------- #
@contextmanager
def _upload_harness(
    *,
    max_upload_bytes: int,
    detect_result: str | None = "wav",
    fail_move: bool = False,
    fail_persist: bool = False,
):
    """Yield ``(client, session_factory, audio_dir, token)`` for one example.

    Builds a fresh in-memory DB + temp audio dir + seeded user/token, mounts a
    ``TestClient`` over ``create_app()`` with the DB/settings dependencies
    overridden, and patches the ``app.api.meetings`` seams:

    - ``detect_format`` -> returns ``detect_result`` (a supported format string,
      or ``None`` to simulate an unsupported/unreadable content verdict);
    - ``_enqueue_processing_job`` -> a no-op spy (no real broker under test);
    - optionally ``_fsync_and_move`` / ``_persist_meeting_record`` -> raise, to
      induce the file-storage / record-creation failures of Property 7.

    Everything is torn down on exit (seams restored, overrides cleared, engine
    disposed, temp dir removed) so examples never share state.
    """
    from fastapi.testclient import TestClient

    audio_dir = tempfile.mkdtemp(prefix="upload-prop-audio-")
    engine = make_engine("sqlite://")
    create_all(engine)
    session_factory = make_session_factory(engine)

    # Seed a user and a valid 60-minute session token.
    sess = session_factory()
    try:
        user = User(username=USERNAME, password_hash=hash_password(PASSWORD))
        sess.add(user)
        sess.commit()
        sess.refresh(user)
        token = issue_session(sess, user.id, ttl_minutes=60).token
        sess.commit()
        user_id = user.id
    finally:
        sess.close()

    test_settings = Settings(
        bootstrap_admin_username="admin",
        bootstrap_admin_password="bootstrap-pass",
        redis_url="redis://localhost:6379/0",
        database_url="sqlite://",
        max_upload_bytes=max_upload_bytes,
        audio_storage_dir=audio_dir,
    )  # type: ignore[call-arg]

    # Patch the injectable seams; capture originals for restoration.
    saved = {
        "detect_format": meetings_mod.detect_format,
        "_enqueue_processing_job": meetings_mod._enqueue_processing_job,
        "_fsync_and_move": meetings_mod._fsync_and_move,
        "_persist_meeting_record": meetings_mod._persist_meeting_record,
    }
    meetings_mod.detect_format = lambda _p: detect_result
    meetings_mod._enqueue_processing_job = lambda _id: None
    if fail_move:
        def _boom_move(*_a, **_k):
            raise OSError("simulated file-storage failure")

        meetings_mod._fsync_and_move = _boom_move
    if fail_persist:
        def _boom_persist(*_a, **_k):
            raise RuntimeError("simulated record-creation failure")

        meetings_mod._persist_meeting_record = _boom_persist

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
        yield client, session_factory, audio_dir, token, user_id
    finally:
        app.dependency_overrides.clear()
        for name, original in saved.items():
            setattr(meetings_mod, name, original)
        engine.dispose()
        shutil.rmtree(audio_dir, ignore_errors=True)


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _rows(session_factory):
    sess = session_factory()
    try:
        meetings = list(sess.execute(select(MeetingRecord)).scalars())
        jobs = list(sess.execute(select(ProcessingJob)).scalars())
        return meetings, jobs
    finally:
        sess.close()


def _temp_files(audio_dir: str) -> list[str]:
    tmp = os.path.join(audio_dir, "tmp")
    if not os.path.isdir(tmp):
        return []
    return [os.path.join(tmp, n) for n in os.listdir(tmp)]


def _final_files(audio_dir: str) -> list[str]:
    """Final audio files written directly under audio_dir (excludes tmp/)."""
    return [
        os.path.join(audio_dir, n)
        for n in os.listdir(audio_dir)
        if os.path.isfile(os.path.join(audio_dir, n))
    ]


# Varied filenames: unicode, spaces, and names without an extension.
# Content-based detection ignores the name, but these exercise the title-default
# and multipart-encoding paths. The name is non-empty: an empty multipart
# filename makes Starlette treat the part as a plain form field rather than a
# file upload (a request-shape concern, not upload validation), so we keep
# min_size=1; whitespace-only names still exercise the nameless-blob title path.
_filenames = st.text(
    alphabet=st.characters(
        min_codepoint=0x20, max_codepoint=0x4FFF, blacklist_characters="\r\n\\/"
    ),
    min_size=1,
    max_size=40,
)


# --------------------------------------------------------------------------- #
# Property 5
# --------------------------------------------------------------------------- #
# Feature: meeting-minutes-service, Property 5: Valid uploads create exactly one associated meeting
# Validates: Requirements 2.4, 3.1, 3.6
@settings(max_examples=100, deadline=None, suppress_health_check=[HealthCheck.too_slow])
@given(
    body=st.binary(min_size=1, max_size=4096),
    filename=_filenames,
    fmt=st.sampled_from(SUPPORTED_FORMATS),
)
def test_property_5_valid_upload_creates_exactly_one_meeting(
    body: bytes, filename: str, fmt: str
) -> None:
    """For any supported-format, >0-byte, within-limit upload: 201 + id, and
    exactly one Meeting_Record (owned by the authed user, file persisted with
    the uploaded bytes) plus exactly one queued Processing_Job -- no duplicates."""
    # max_upload_bytes >= body size so the body is within the limit (boundary
    # included: a body of exactly 4096 with max 4096 is accepted).
    with _upload_harness(max_upload_bytes=4096, detect_result=fmt) as (
        client,
        session_factory,
        audio_dir,
        token,
        user_id,
    ):
        resp = client.post(
            "/api/meetings",
            headers=_auth(token),
            files={"file": (filename, io.BytesIO(body), "application/octet-stream")},
        )

        assert resp.status_code == 201
        meeting_id = resp.json()["id"]
        assert meeting_id

        meetings, jobs = _rows(session_factory)
        # Exactly one Meeting_Record, owned by the authed user (no duplicates).
        assert len(meetings) == 1
        record = meetings[0]
        assert record.id == meeting_id
        assert record.owner_id == user_id
        assert record.source_format == fmt

        # The stored file exists under audio_storage_dir and holds the bytes.
        expected_path = os.path.join(audio_dir, f"{meeting_id}.{fmt}")
        assert record.source_audio_path == expected_path
        assert os.path.isfile(expected_path)
        with open(expected_path, "rb") as fh:
            assert fh.read() == body

        # Exactly one queued Processing_Job tied to the meeting (no duplicates).
        assert len(jobs) == 1
        assert jobs[0].meeting_id == meeting_id
        assert jobs[0].status == JobStatus.QUEUED.value

        # No temp left behind; exactly one final file on disk.
        assert _temp_files(audio_dir) == []
        assert len(_final_files(audio_dir)) == 1


# --------------------------------------------------------------------------- #
# Property 6
# --------------------------------------------------------------------------- #
# Feature: meeting-minutes-service, Property 6: Upload validation maps each invalid input to its status code
# Validates: Requirements 3.2, 3.3, 3.4
@settings(max_examples=100, deadline=None, suppress_health_check=[HealthCheck.too_slow])
@given(
    body=st.binary(min_size=1, max_size=2048),
    filename=_filenames,
)
def test_property_6_unsupported_format_rejected_with_415(
    body: bytes, filename: str
) -> None:
    """Unsupported content (detect_format -> None) on an in-range, non-empty
    body is rejected with 415 naming mp3/wav/m4a/webm, and no record is made."""
    with _upload_harness(max_upload_bytes=4096, detect_result=None) as (
        client,
        session_factory,
        audio_dir,
        token,
        _user_id,
    ):
        resp = client.post(
            "/api/meetings",
            headers=_auth(token),
            files={"file": (filename, io.BytesIO(body), "application/octet-stream")},
        )

        assert resp.status_code == 415
        payload = resp.json()
        # Message must name every supported format (Req 3.2).
        haystack = (payload.get("detail", "") + str(payload.get("supported_formats", ""))).lower()
        for fmt in SUPPORTED_FORMATS:
            assert fmt in haystack

        # No Meeting_Record / Processing_Job and no stored file on rejection.
        meetings, jobs = _rows(session_factory)
        assert meetings == []
        assert jobs == []
        assert _final_files(audio_dir) == []
        assert _temp_files(audio_dir) == []


# Feature: meeting-minutes-service, Property 6: Upload validation maps each invalid input to its status code
# Validates: Requirements 3.2, 3.3, 3.4
@settings(max_examples=100, deadline=None, suppress_health_check=[HealthCheck.too_slow])
@given(
    max_bytes=st.integers(min_value=1, max_value=1024),
    overflow=st.integers(min_value=1, max_value=1024),
    filename=_filenames,
)
def test_property_6_oversize_rejected_with_413(
    max_bytes: int, overflow: int, filename: str
) -> None:
    """A body strictly larger than max_upload_bytes is rejected with 413 and a
    message stating the size limit; no record is made. The size check fires
    before format detection, so detect_format would have returned a supported
    format -- the rejection is purely on size (Req 3.3)."""
    body = b"\x00" * (max_bytes + overflow)  # strictly over the limit
    with _upload_harness(max_upload_bytes=max_bytes, detect_result="wav") as (
        client,
        session_factory,
        audio_dir,
        token,
        _user_id,
    ):
        resp = client.post(
            "/api/meetings",
            headers=_auth(token),
            files={"file": (filename, io.BytesIO(body), "application/octet-stream")},
        )

        assert resp.status_code == 413
        payload = resp.json()
        # The limit is surfaced in the rejection (Req 3.3).
        assert payload.get("max_upload_bytes") == max_bytes
        assert str(max_bytes) in payload.get("detail", "")

        meetings, jobs = _rows(session_factory)
        assert meetings == []
        assert jobs == []
        assert _final_files(audio_dir) == []
        assert _temp_files(audio_dir) == []


# Feature: meeting-minutes-service, Property 6: Upload validation maps each invalid input to its status code
# Validates: Requirements 3.2, 3.3, 3.4
@settings(max_examples=100, deadline=None, suppress_health_check=[HealthCheck.too_slow])
@given(
    filename=_filenames,
    max_bytes=st.integers(min_value=1, max_value=4096),
)
def test_property_6_empty_file_rejected_with_400(
    filename: str, max_bytes: int
) -> None:
    """A 0-byte upload is rejected with 400 and an 'empty' message; no record is
    made. The empty check fires before format detection (Req 3.4)."""
    with _upload_harness(max_upload_bytes=max_bytes, detect_result="wav") as (
        client,
        session_factory,
        audio_dir,
        token,
        _user_id,
    ):
        resp = client.post(
            "/api/meetings",
            headers=_auth(token),
            files={"file": (filename, io.BytesIO(b""), "application/octet-stream")},
        )

        assert resp.status_code == 400
        assert "empty" in resp.json().get("detail", "").lower()

        meetings, jobs = _rows(session_factory)
        assert meetings == []
        assert jobs == []
        assert _final_files(audio_dir) == []
        assert _temp_files(audio_dir) == []


# --------------------------------------------------------------------------- #
# Property 7
# --------------------------------------------------------------------------- #
# Feature: meeting-minutes-service, Property 7: Upload failures leave no partial file and no orphan record
# Validates: Requirements 3.5
@settings(max_examples=100, deadline=None, suppress_health_check=[HealthCheck.too_slow])
@given(
    body=st.binary(min_size=1, max_size=4096),
    filename=_filenames,
    fmt=st.sampled_from(SUPPORTED_FORMATS),
    fail_at_persist=st.booleans(),
)
def test_property_7_failure_leaves_no_partial_or_orphan(
    body: bytes, filename: str, fmt: str, fail_at_persist: bool
) -> None:
    """For any induced failure of file storage OR record creation on an
    otherwise-valid upload: 500, no Meeting_Record/Processing_Job row, and no
    leftover final file and no leftover temp file."""
    with _upload_harness(
        max_upload_bytes=4096,
        detect_result=fmt,
        fail_move=not fail_at_persist,
        fail_persist=fail_at_persist,
    ) as (client, session_factory, audio_dir, token, _user_id):
        resp = client.post(
            "/api/meetings",
            headers=_auth(token),
            files={"file": (filename, io.BytesIO(body), "application/octet-stream")},
        )

        assert resp.status_code == 500

        # No orphan rows of either kind (Req 3.5).
        meetings, jobs = _rows(session_factory)
        assert meetings == []
        assert jobs == []

        # No partial file: neither a final audio file nor a leftover temp.
        assert _final_files(audio_dir) == []
        assert _temp_files(audio_dir) == []
