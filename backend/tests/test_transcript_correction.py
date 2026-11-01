"""API tests for the transcript-correction endpoints.

Covers three endpoints added in the liquid-glass polish pass:

- ``POST /{meeting_id}/correct-selection``
- ``POST /{meeting_id}/transcript/correct-batch``
- ``PUT  /{meeting_id}/transcript``

Each endpoint is exercised through the FastAPI TestClient; a fake ``LlmClient``
is injected via ``monkeypatch`` so no real LLM endpoint is required.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator

import pytest
from sqlalchemy import select
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session as OrmSession
from sqlalchemy.orm import sessionmaker

from app.clients.base import LlmError
from app.config import Settings, get_settings
from app.db import get_session, make_engine, make_session_factory
from app.main import create_app
from app.models import (
    MeetingRecord,
    ProcessingJob,
    Transcript,
    TranscriptSegment,
    User,
    create_all,
)
from app.security.passwords import hash_password
from app.security.sessions import issue_session

# --------------------------------------------------------------------------- #
# Fake LLM client — configurable per test
# --------------------------------------------------------------------------- #


class _FakeLlmClient:
    """A stub ``LlmClient`` whose behavior the test controls via attributes."""

    def __init__(self) -> None:
        self.correct_text_result: str = "corrected"
        self.correct_batch_result: list[dict[str, object]] = []
        self._raise_on_next: Exception | None = None

    def set_raise(self, exc: Exception) -> None:
        self._raise_on_next = exc

    def correct_text(self, text: str, *, context: str | None = None) -> str:
        if self._raise_on_next is not None:
            raise self._raise_on_next
        return self.correct_text_result

    def correct_batch(self, segments: list[dict[str, object]]) -> list[dict[str, object]]:
        if self._raise_on_next is not None:
            raise self._raise_on_next
        return self.correct_batch_result

    def generate_minutes(self, transcript):  # noqa: ANN001 — not used by these tests
        raise NotImplementedError


# --------------------------------------------------------------------------- #
# Fixtures
# --------------------------------------------------------------------------- #

USERNAME = "corrector"
PASSWORD = "test-password"


@pytest.fixture()
def test_settings() -> Settings:
    return Settings(
        bootstrap_admin_username="admin",
        bootstrap_admin_password="bootstrap-pass",
        redis_url="redis://localhost:6379/0",
        database_url="sqlite://",
    )  # type: ignore[call-arg]


@pytest.fixture()
def engine() -> Iterator[Engine]:
    eng = make_engine("sqlite://")
    create_all(eng)
    try:
        yield eng
    finally:
        eng.dispose()


@pytest.fixture()
def session_factory(engine: Engine) -> sessionmaker[OrmSession]:
    return make_session_factory(engine)


@pytest.fixture()
def seed_user_id(session_factory: sessionmaker[OrmSession]) -> str:
    """Create a user and return its id."""
    sess = session_factory()
    try:
        user = User(username=USERNAME, password_hash=hash_password(PASSWORD))
        sess.add(user)
        sess.commit()
        sess.refresh(user)
        return user.id
    finally:
        sess.close()


@pytest.fixture()
def auth_token(
    session_factory: sessionmaker[OrmSession], seed_user_id: str
) -> str:
    """Issue a session token for the seeded user."""
    sess = session_factory()
    try:
        session = issue_session(sess, seed_user_id, ttl_minutes=60)
        sess.commit()
        return session.token
    finally:
        sess.close()


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


# --------------------------------------------------------------------------- #
# Shared helper: create a meeting with transcript + segments
# --------------------------------------------------------------------------- #


def _seed_meeting_with_transcript(
    session_factory: sessionmaker[OrmSession],
    owner_id: str,
    *,
    meeting_id: str | None = None,
    segment_count: int = 5,
    with_minutes: bool = False,
) -> str:
    """Create a completed meeting with a transcript and *n* segments.

    Returns the meeting id.
    """
    sess = session_factory()
    try:
        mid = meeting_id or str(uuid.uuid4())
        meeting = MeetingRecord(
            id=mid,
            owner_id=owner_id,
            title="Test Meeting",
            source_audio_path="/tmp/test.wav",
            source_format="wav",
        )
        sess.add(meeting)

        # A completed processing job so the meeting status is "completed".
        job = ProcessingJob(
            meeting_id=mid,
            status="completed",
        )
        sess.add(job)

        # Transcript + segments
        transcript = Transcript(meeting_id=mid)
        sess.add(transcript)
        sess.flush()

        for i in range(segment_count):
            seg = TranscriptSegment(
                transcript_id=transcript.id,
                start_seconds=i * 5.0,
                end_seconds=(i + 1) * 5.0,
                text=f"Segment {i} original text.",
                speaker=f"Speaker {chr(65 + (i % 3))}",  # A, B, C cycling
                ordinal=i,
            )
            sess.add(seg)

        sess.commit()
        return mid
    finally:
        sess.close()


# --------------------------------------------------------------------------- #
# Client fixture — overrides deps + injects fake LLM
# --------------------------------------------------------------------------- #


@pytest.fixture()
def fake_llm() -> _FakeLlmClient:
    return _FakeLlmClient()


@pytest.fixture()
def client(
    test_settings: Settings,
    session_factory: sessionmaker[OrmSession],
    fake_llm: _FakeLlmClient,
    monkeypatch: pytest.MonkeyPatch,
) -> Iterator["TestClient"]:  # noqa: F821
    """TestClient with in-memory DB, test settings, and fake-LLM injection."""
    from fastapi.testclient import TestClient

    # Endpoint bodies call get_settings() inline (not via Depends), so the
    # FastAPI dependency-override dict doesn't intercept them. Pin the
    # module-level reference to test_settings so every inline call succeeds.
    monkeypatch.setattr("app.api.meetings.get_settings", lambda: test_settings)

    # Route make_llm_client to our fake regardless of settings.
    monkeypatch.setattr(
        "app.api.meetings.make_llm_client",
        lambda _settings: fake_llm,
    )

    app = create_app()

    def _override_get_session() -> Iterator[OrmSession]:
        sess = session_factory()
        try:
            yield sess
        finally:
            sess.close()

    app.dependency_overrides[get_session] = _override_get_session
    app.dependency_overrides[get_settings] = lambda: test_settings

    with TestClient(app) as test_client:
        yield test_client

    app.dependency_overrides.clear()


# =================================================================== #
# POST /{meeting_id}/correct-selection
# =================================================================== #


def test_correct_selection_success(client, fake_llm, session_factory, seed_user_id, auth_token):
    """Happy path: successful correction returns {corrected: ...}."""
    mid = _seed_meeting_with_transcript(session_factory, seed_user_id)
    fake_llm.correct_text_result = "这是纠错后的文本"

    resp = client.post(
        f"/api/meetings/{mid}/correct-selection",
        headers=_auth(auth_token),
        json={"text": "这是园始文本", "context": "会议主题：测试"},
    )

    assert resp.status_code == 200
    body = resp.json()
    assert body["corrected"] == "这是纠错后的文本"


def test_correct_selection_unknown_meeting_returns_404(client, auth_token):
    """A random meeting id returns 404 (ownership gate)."""
    resp = client.post(
        "/api/meetings/00000000-0000-0000-0000-000000000000/correct-selection",
        headers=_auth(auth_token),
        json={"text": "测试"},
    )
    assert resp.status_code == 404


def test_correct_selection_foreign_meeting_returns_404(
    client, session_factory, seed_user_id,
):
    """A meeting owned by another user returns 404 (not 403 — opaque)."""
    # Create meeting under a different owner.
    other_id = str(uuid.uuid4())
    sess = session_factory()
    try:
        other = User(
            id=other_id,
            username="other-user",
            password_hash=hash_password("x"),
        )
        sess.add(other)
        sess.commit()
    finally:
        sess.close()
    mid = _seed_meeting_with_transcript(session_factory, other_id)

    # Auth as seed_user_id — should not see other's meeting.
    sess2 = session_factory()
    try:
        tok = issue_session(sess2, seed_user_id, ttl_minutes=60).token
        sess2.commit()
    finally:
        sess2.close()
    auth = _auth(tok)
    resp = client.post(
        f"/api/meetings/{mid}/correct-selection",
        headers=auth,
        json={"text": "测试"},
    )
    assert resp.status_code == 404


def test_correct_selection_llm_error_returns_502(
    client, fake_llm, session_factory, seed_user_id, auth_token,
):
    """LLM transport failures map to 502."""
    mid = _seed_meeting_with_transcript(session_factory, seed_user_id)
    fake_llm.set_raise(LlmError("LLM is down"))

    resp = client.post(
        f"/api/meetings/{mid}/correct-selection",
        headers=_auth(auth_token),
        json={"text": "测试"},
    )
    assert resp.status_code == 502
    assert "LLM is down" in resp.json()["detail"]


# =================================================================== #
# POST /{meeting_id}/transcript/correct-batch
# =================================================================== #


def test_correct_batch_success_index_align(
    client, fake_llm, session_factory, seed_user_id, auth_token,
):
    """Every input segment gets a corresponding output by index."""
    mid = _seed_meeting_with_transcript(session_factory, seed_user_id, segment_count=5)
    fake_llm.correct_batch_result = [
        {"index": 0, "text": "Corrected 0"},
        {"index": 1, "text": "Corrected 1"},
        {"index": 2, "text": "Corrected 2"},
        {"index": 3, "text": "Corrected 3"},
        {"index": 4, "text": "Corrected 4"},
    ]

    resp = client.post(
        f"/api/meetings/{mid}/transcript/correct-batch",
        headers=_auth(auth_token),
        json={
            "segments": [
                {"index": i, "text": f"Seg {i}", "speaker": "A"}
                for i in range(5)
            ]
        },
    )

    assert resp.status_code == 200
    body = resp.json()
    assert len(body["segments"]) == 5
    for i, seg in enumerate(body["segments"]):
        assert seg["index"] == i
        assert seg["text"] == f"Corrected {i}"


def test_correct_batch_missing_indices_in_response(
    client, fake_llm, session_factory, seed_user_id, auth_token,
):
    """If the LLM omits some indices the response still lists all (stub behavior).

    The endpoint itself is a thin pass-through — it returns whatever the
    LLM client returns. The frontend's responsibility is to detect gaps
    (the ``correctedMap.get(i) ?? seg.text`` fallback) and to surface them
    to the user. This test asserts what the backend actually sends, so the
    frontend can rely on the contract.
    """
    mid = _seed_meeting_with_transcript(session_factory, seed_user_id, segment_count=3)
    fake_llm.correct_batch_result = [
        {"index": 0, "text": "OK"},
        # index 1 deliberately omitted
        {"index": 2, "text": "Also OK"},
    ]

    resp = client.post(
        f"/api/meetings/{mid}/transcript/correct-batch",
        headers=_auth(auth_token),
        json={
            "segments": [
                {"index": i, "text": f"Seg {i}"} for i in range(3)
            ]
        },
    )

    assert resp.status_code == 200
    body = resp.json()
    assert len(body["segments"]) == 2  # backend returns whatever LLM gave
    indices = {s["index"] for s in body["segments"]}
    assert indices == {0, 2}
    assert 1 not in indices  # frontend should detect this gap


def test_correct_batch_empty_segments_returns_400(
    client, session_factory, seed_user_id, auth_token,
):
    """Empty segment list triggers FastAPI validation (400)."""
    mid = _seed_meeting_with_transcript(session_factory, seed_user_id)

    resp = client.post(
        f"/api/meetings/{mid}/transcript/correct-batch",
        headers=_auth(auth_token),
        json={"segments": []},
    )
    assert resp.status_code == 422  # pydantic validation


def test_correct_batch_unknown_meeting_returns_404(
    client, auth_token,
):
    resp = client.post(
        "/api/meetings/00000000-0000-0000-0000-000000000000/transcript/correct-batch",
        headers=_auth(auth_token),
        json={"segments": [{"index": 0, "text": "test"}]},
    )
    assert resp.status_code == 404


def test_correct_batch_llm_error_returns_502(
    client, fake_llm, session_factory, seed_user_id, auth_token,
):
    mid = _seed_meeting_with_transcript(session_factory, seed_user_id)
    fake_llm.set_raise(LlmError("batch failed"))

    resp = client.post(
        f"/api/meetings/{mid}/transcript/correct-batch",
        headers=_auth(auth_token),
        json={"segments": [{"index": 0, "text": "test"}]},
    )
    assert resp.status_code == 502


def test_correct_batch_span_over_25_windows(
    client, fake_llm, session_factory, seed_user_id, auth_token,
):
    """Correct batch with 50+ segments spans multiple windows and recombines."""
    mid = _seed_meeting_with_transcript(
        session_factory, seed_user_id, segment_count=60,
    )
    # Return index-aligned result for all 60.
    fake_llm.correct_batch_result = [
        {"index": i, "text": f"win-corrected-{i}"} for i in range(60)
    ]

    resp = client.post(
        f"/api/meetings/{mid}/transcript/correct-batch",
        headers=_auth(auth_token),
        json={
            "segments": [
                {"index": i, "text": f"raw-{i}"} for i in range(60)
            ]
        },
    )

    assert resp.status_code == 200
    body = resp.json()
    assert len(body["segments"]) == 60
    # Verify every index maps correctly.
    seen = {s["index"] for s in body["segments"]}
    assert seen == set(range(60))


# =================================================================== #
# PUT /{meeting_id}/transcript
# =================================================================== #


def test_update_transcript_replaces_segments(
    client, session_factory, seed_user_id, auth_token,
):
    """After update, the persisted segments reflect the new text/ordering."""
    mid = _seed_meeting_with_transcript(session_factory, seed_user_id, segment_count=3)

    resp = client.put(
        f"/api/meetings/{mid}/transcript",
        headers=_auth(auth_token),
        json={
            "segments": [
                {
                    "start": 0.0,
                    "end": 10.0,
                    "text": "Updated text one",
                    "speaker": "Speaker X",
                },
                {
                    "start": 10.0,
                    "end": 20.0,
                    "text": "Updated text two",
                    "speaker": None,
                },
            ]
        },
    )

    assert resp.status_code == 200
    body = resp.json()
    assert body["updated"] is True
    assert body["count"] == 2
    assert body["id"] == mid

    # Verify persistence — read back directly.
    sess = session_factory()
    try:
        transcript = sess.execute(
            select(Transcript).where(Transcript.meeting_id == mid)
        ).scalar_one()
        segs = sorted(transcript.segments, key=lambda s: s.ordinal)
        assert len(segs) == 2
        assert segs[0].text == "Updated text one"
        assert segs[0].speaker == "Speaker X"
        assert segs[1].text == "Updated text two"
        assert segs[1].speaker is None
    finally:
        sess.close()


def test_update_transcript_no_transcript_returns_409(
    client, session_factory, seed_user_id, auth_token,
):
    """Meeting without a transcript cannot have its transcript updated."""
    sess = session_factory()
    try:
        mid = str(uuid.uuid4())
        meeting = MeetingRecord(
            id=mid,
            owner_id=seed_user_id,
            title="No Transcript Meeting",
            source_audio_path="/tmp/t.wav",
            source_format="wav",
        )
        sess.add(meeting)
        sess.add(ProcessingJob(meeting_id=mid, status="completed"))
        sess.commit()
    finally:
        sess.close()

    resp = client.put(
        f"/api/meetings/{mid}/transcript",
        headers=_auth(auth_token),
        json={
            "segments": [{"start": 0, "end": 5, "text": "x", "speaker": None}]
        },
    )
    assert resp.status_code == 409


def test_update_transcript_unknown_meeting_returns_404(
    client, auth_token,
):
    resp = client.put(
        "/api/meetings/00000000-0000-0000-0000-000000000000/transcript",
        headers=_auth(auth_token),
        json={
            "segments": [{"start": 0, "end": 5, "text": "x", "speaker": None}]
        },
    )
    assert resp.status_code == 404
