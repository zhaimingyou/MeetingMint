"""Property + example tests for the Celery worker pipeline -- task 9.4.

These cover design Properties 17 and 18 for ``run_pipeline`` (the injectable
core behind the ``process_meeting`` Celery task), plus example tests for the
concrete success/failure paths (Req 5.7, 6.7, 7.2, 7.3, 7.4).

Everything runs with **no** real ffmpeg / ASR / LLM / Redis:

- an in-memory SQLite database with the full schema (``app.models.create_all``)
  seeded with a user + meeting + ``queued`` Processing_Job per example;
- fake/mock ASR and LLM clients (and a fake ``preprocess_fn``) injected into
  ``run_pipeline``;
- a ``status_observer`` hook that records each ``Job_Status`` write in order,
  so Property 17's transition ordering is observed directly.

``hypothesis`` disallows function-scoped fixtures under ``@given`` (examples
would share state), so each example builds a fresh, isolated database via the
:func:`_pipeline_db` context manager and tears it down on exit.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager

from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st
from sqlalchemy import select

from app.clients.base import AsrError, LlmError, LlmSchemaError
from app.clients.factory import make_asr_client, make_llm_client
from app.config import Settings
from app.db import make_engine, make_session_factory
from app.enums import UNASSIGNED_OWNER, JobStatus
from app.models import (
    ActionItem,
    MeetingMinutes,
    MeetingRecord,
    ProcessingJob,
    Transcript,
    TranscriptSegment,
    User,
    create_all,
)
from app.preprocess import PreprocessError, PreprocessTimeout
from app.worker.tasks import run_pipeline


# --------------------------------------------------------------------------- #
# Helpers: isolated in-memory pipeline database + fakes
# --------------------------------------------------------------------------- #
def _settings(audio_dir: str = "/tmp/mms-pipeline-test") -> Settings:
    """A mock-impl Settings for the pipeline (no real infra needed)."""
    return Settings(
        bootstrap_admin_username="admin",
        bootstrap_admin_password="bootstrap-pass",
        redis_url="redis://localhost:6379/0",
        database_url="sqlite://",
        audio_storage_dir=audio_dir,
        asr_impl="mock",
        llm_impl="mock",
        preprocess_timeout_seconds=600,
    )  # type: ignore[call-arg]


@contextmanager
def _pipeline_db() -> Iterator[tuple[object, str]]:
    """Yield ``(session_factory, meeting_id)`` for a fresh seeded DB; tear down."""
    engine = make_engine("sqlite://")
    create_all(engine)
    session_factory = make_session_factory(engine)

    sess = session_factory()
    try:
        user = User(username="pipeline-owner", password_hash="x")
        sess.add(user)
        sess.commit()
        sess.refresh(user)
        meeting = MeetingRecord(
            owner_id=user.id,
            title="meeting",
            source_audio_path="/srv/audio/source.m4a",
            source_format="m4a",
        )
        sess.add(meeting)
        sess.commit()
        sess.refresh(meeting)
        sess.add(ProcessingJob(meeting_id=meeting.id, status=JobStatus.QUEUED.value))
        sess.commit()
        meeting_id = meeting.id
    finally:
        sess.close()

    try:
        yield session_factory, meeting_id
    finally:
        engine.dispose()


def _ok_preprocess(source_path: str, output_path: str, *, timeout_seconds: int) -> str:
    """Fake preprocessor: succeeds without touching ffmpeg, returns the path."""
    return output_path


def _failing_preprocess(exc: Exception):
    def _fn(source_path: str, output_path: str, *, timeout_seconds: int) -> str:
        raise exc

    return _fn


class _FakeAsr:
    """ASR client returning a fixed segment list."""

    def __init__(self, segments: list[dict]) -> None:
        self._segments = segments

    def transcribe(self, audio_path: str) -> list[dict]:
        return list(self._segments)


class _FailingAsr:
    def __init__(self, exc: Exception) -> None:
        self._exc = exc

    def transcribe(self, audio_path: str):
        raise self._exc


class _FakeLlm:
    """LLM client returning fixed minutes data."""

    def __init__(self, minutes: dict) -> None:
        self._minutes = minutes

    def generate_minutes(self, transcript) -> dict:
        return self._minutes


class _FailingLlm:
    def __init__(self, exc: Exception) -> None:
        self._exc = exc

    def generate_minutes(self, transcript):
        raise self._exc


def _read_job(session_factory, meeting_id: str) -> ProcessingJob:
    sess = session_factory()
    try:
        return sess.execute(
            select(ProcessingJob).where(ProcessingJob.meeting_id == meeting_id)
        ).scalar_one()
    finally:
        sess.close()


def _read_meeting(session_factory, meeting_id: str) -> MeetingRecord:
    sess = session_factory()
    try:
        return sess.get(MeetingRecord, meeting_id)
    finally:
        sess.close()


def _read_transcript(session_factory, meeting_id: str) -> Transcript | None:
    sess = session_factory()
    try:
        return sess.execute(
            select(Transcript).where(Transcript.meeting_id == meeting_id)
        ).scalar_one_or_none()
    finally:
        sess.close()


def _read_segments(session_factory, meeting_id: str) -> list[TranscriptSegment]:
    sess = session_factory()
    try:
        return list(
            sess.execute(
                select(TranscriptSegment)
                .join(Transcript, TranscriptSegment.transcript_id == Transcript.id)
                .where(Transcript.meeting_id == meeting_id)
                .order_by(TranscriptSegment.ordinal)
            ).scalars()
        )
    finally:
        sess.close()


def _read_minutes(session_factory, meeting_id: str) -> MeetingMinutes | None:
    sess = session_factory()
    try:
        return sess.execute(
            select(MeetingMinutes).where(MeetingMinutes.meeting_id == meeting_id)
        ).scalar_one_or_none()
    finally:
        sess.close()


def _read_action_items(session_factory, minutes_id: str) -> list[ActionItem]:
    sess = session_factory()
    try:
        return list(
            sess.execute(
                select(ActionItem)
                .where(ActionItem.minutes_id == minutes_id)
                .order_by(ActionItem.ordinal)
            ).scalars()
        )
    finally:
        sess.close()


# --------------------------------------------------------------------------- #
# Strategies
# --------------------------------------------------------------------------- #
@st.composite
def _segments_strategy(draw) -> list[dict]:
    """Generate an ascending-ordered, contiguous list of transcript segments."""
    n = draw(st.integers(min_value=0, max_value=6))
    cursor = draw(st.floats(min_value=0.0, max_value=5.0, allow_nan=False, allow_infinity=False))
    out: list[dict] = []
    for _ in range(n):
        dur = draw(st.floats(min_value=0.1, max_value=10.0, allow_nan=False, allow_infinity=False))
        start = round(cursor, 3)
        end = round(cursor + dur, 3)
        text = draw(st.text(max_size=30))
        speaker = draw(st.none() | st.text(min_size=1, max_size=8))
        out.append({"start": start, "end": end, "text": text, "speaker": speaker})
        cursor = end
    return out


_minutes_strategy = st.fixed_dictionaries(
    {
        "summary": st.text(max_size=60),
        "topics": st.lists(st.text(max_size=20), max_size=5),
        "decisions": st.lists(st.text(max_size=20), max_size=5),
        "action_items": st.lists(
            st.fixed_dictionaries(
                {
                    "description": st.text(max_size=20),
                    # Non-empty owner so the round-trip is exact (the pipeline
                    # only substitutes the unassigned indicator for falsy owners).
                    "owner": st.text(min_size=1, max_size=10),
                }
            ),
            max_size=5,
        ),
    }
)


# --------------------------------------------------------------------------- #
# Property 17
# --------------------------------------------------------------------------- #
# Feature: meeting-minutes-service, Property 17: Successful pipeline persists results and follows the stage ordering
# Validates: Requirements 5.7, 6.7, 7.1, 7.2, 7.3
@settings(max_examples=100, deadline=None, suppress_health_check=[HealthCheck.too_slow])
@given(segments=_segments_strategy(), minutes=_minutes_strategy)
def test_property_17_successful_pipeline_ordering_and_persistence(
    segments: list[dict], minutes: dict
) -> None:
    """For any meeting processed to success: the observed Job_Status transitions
    occur in the order queued -> preprocessing -> transcribing -> generating ->
    completed, and the assembled Transcript and generated Meeting_Minutes are
    persisted and retrievable from the Meeting_Record unchanged."""
    with _pipeline_db() as (session_factory, meeting_id):
        # The job starts queued (set at creation, Req 7.1).
        assert _read_job(session_factory, meeting_id).status == JobStatus.QUEUED.value

        observed: list[JobStatus] = []
        result = run_pipeline(
            meeting_id,
            settings=_settings(),
            session_factory=session_factory,
            asr_client=_FakeAsr(segments),
            llm_client=_FakeLlm(minutes),
            preprocess_fn=_ok_preprocess,
            status_observer=observed.append,
        )

        # Stage ordering: queued (initial) then the four observed writes.
        assert result is JobStatus.COMPLETED
        assert observed == [
            JobStatus.PREPROCESSING,
            JobStatus.TRANSCRIBING,
            JobStatus.GENERATING,
            JobStatus.COMPLETED,
        ]

        job = _read_job(session_factory, meeting_id)
        assert job.status == JobStatus.COMPLETED.value
        assert job.error_description is None

        # Transcript persisted unchanged, ordered by ordinal == input order.
        rows = _read_segments(session_factory, meeting_id)
        assert [r.ordinal for r in rows] == list(range(len(segments)))
        for row, seg in zip(rows, segments):
            assert row.start_seconds == seg["start"]
            assert row.end_seconds == seg["end"]
            assert row.text == seg["text"]
            assert row.speaker == seg["speaker"]

        # Meeting_Minutes persisted unchanged.
        persisted = _read_minutes(session_factory, meeting_id)
        assert persisted is not None
        assert persisted.summary == minutes["summary"]
        assert list(persisted.topics) == minutes["topics"]
        assert list(persisted.decisions) == minutes["decisions"]

        items = _read_action_items(session_factory, persisted.id)
        assert [i.ordinal for i in items] == list(range(len(minutes["action_items"])))
        for item, src in zip(items, minutes["action_items"]):
            assert item.description == src["description"]
            assert item.owner == src["owner"]
            assert item.owner != ""  # every owner is non-empty


# --------------------------------------------------------------------------- #
# Property 18
# --------------------------------------------------------------------------- #
# Feature: meeting-minutes-service, Property 18: Any stage failure produces a failed job with an error description
# Validates: Requirements 7.4
@settings(max_examples=100, deadline=None, suppress_health_check=[HealthCheck.too_slow])
@given(
    stage=st.sampled_from(["preprocess", "asr", "llm"]),
    msg=st.text(min_size=1, max_size=30).filter(lambda s: s.strip()),
    variant=st.booleans(),
)
def test_property_18_any_stage_failure_sets_failed_with_error(
    stage: str, msg: str, variant: bool
) -> None:
    """For any processing stage chosen to fail, the Job_Processor sets
    Job_Status to failed, records a non-empty error description, and persists no
    partial result for the failing stage."""
    ok_segments = [{"start": 0.0, "end": 1.0, "text": "hi", "speaker": None}]
    ok_minutes = {"summary": "s", "topics": [], "decisions": [], "action_items": []}

    preprocess_fn = _ok_preprocess
    asr_client = _FakeAsr(ok_segments)
    llm_client = _FakeLlm(ok_minutes)

    if stage == "preprocess":
        exc: Exception = (
            PreprocessTimeout(msg, timeout_seconds=5) if variant else PreprocessError(msg)
        )
        preprocess_fn = _failing_preprocess(exc)
    elif stage == "asr":
        asr_client = _FailingAsr(
            AsrError(msg, chunk_index=1) if variant else AsrError(msg)
        )
    else:  # llm
        llm_client = _FailingLlm(LlmSchemaError(msg) if variant else LlmError(msg))

    with _pipeline_db() as (session_factory, meeting_id):
        result = run_pipeline(
            meeting_id,
            settings=_settings(),
            session_factory=session_factory,
            asr_client=asr_client,
            llm_client=llm_client,
            preprocess_fn=preprocess_fn,
        )

        assert result is JobStatus.FAILED
        job = _read_job(session_factory, meeting_id)
        assert job.status == JobStatus.FAILED.value
        assert job.error_description is not None
        assert job.error_description.strip() != ""

        # No partial results persisted for the failing stage.
        if stage == "preprocess":
            assert _read_meeting(session_factory, meeting_id).standardized_audio_path is None
            assert _read_transcript(session_factory, meeting_id) is None
            assert _read_minutes(session_factory, meeting_id) is None
        elif stage == "asr":
            assert _read_transcript(session_factory, meeting_id) is None
            assert _read_minutes(session_factory, meeting_id) is None
        else:  # llm: transcript is a prior-stage success; minutes must be absent
            assert _read_transcript(session_factory, meeting_id) is not None
            assert _read_minutes(session_factory, meeting_id) is None


# --------------------------------------------------------------------------- #
# Example tests
# --------------------------------------------------------------------------- #
def test_successful_end_to_end_with_mock_clients() -> None:
    """End-to-end success using the real mock ASR/LLM clients (built from
    settings): segments persist in ascending ordinal order with correct
    ordinals, and every action item carries a non-empty owner."""
    with _pipeline_db() as (session_factory, meeting_id):
        st_ = _settings()
        result = run_pipeline(
            meeting_id,
            settings=st_,
            session_factory=session_factory,
            preprocess_fn=_ok_preprocess,  # avoid real ffmpeg; clients built from settings
        )
        assert result is JobStatus.COMPLETED
        assert _read_job(session_factory, meeting_id).status == JobStatus.COMPLETED.value

        # Sanity: the clients we built from settings are the offline mocks.
        assert type(make_asr_client(st_)).__name__ == "MockAsrClient"
        assert type(make_llm_client(st_)).__name__ == "MockLlmClient"

        rows = _read_segments(session_factory, meeting_id)
        assert rows, "mock ASR yields at least one segment"
        assert [r.ordinal for r in rows] == list(range(len(rows)))
        starts = [r.start_seconds for r in rows]
        assert starts == sorted(starts)  # ascending start (Req 5.3)

        minutes = _read_minutes(session_factory, meeting_id)
        assert minutes is not None
        for item in _read_action_items(session_factory, minutes.id):
            assert item.owner != ""  # Req 6.3: non-empty owner


def test_preprocess_timeout_fails_with_timeout_description() -> None:
    """A preprocessing timeout sets failed with a non-empty timeout description
    and persists no standardized path / transcript / minutes."""
    with _pipeline_db() as (session_factory, meeting_id):
        result = run_pipeline(
            meeting_id,
            settings=_settings(),
            session_factory=session_factory,
            asr_client=_FakeAsr([]),
            llm_client=_FakeLlm({"summary": "", "topics": [], "decisions": [], "action_items": []}),
            preprocess_fn=_failing_preprocess(
                PreprocessTimeout("ffmpeg transcode timed out after 5s", timeout_seconds=5)
            ),
        )
        assert result is JobStatus.FAILED
        job = _read_job(session_factory, meeting_id)
        assert job.status == JobStatus.FAILED.value
        assert "timed out" in (job.error_description or "")
        assert _read_meeting(session_factory, meeting_id).standardized_audio_path is None
        assert _read_transcript(session_factory, meeting_id) is None
        assert _read_minutes(session_factory, meeting_id) is None


def test_asr_failure_fails_with_no_transcript() -> None:
    """An ASR failure sets failed and persists no Transcript (Req 5.5/5.6)."""
    with _pipeline_db() as (session_factory, meeting_id):
        result = run_pipeline(
            meeting_id,
            settings=_settings(),
            session_factory=session_factory,
            asr_client=_FailingAsr(AsrError("ASR unreachable", chunk_index=2)),
            llm_client=_FakeLlm({"summary": "", "topics": [], "decisions": [], "action_items": []}),
            preprocess_fn=_ok_preprocess,
        )
        assert result is JobStatus.FAILED
        job = _read_job(session_factory, meeting_id)
        assert job.status == JobStatus.FAILED.value
        assert "chunk 2" in (job.error_description or "")
        assert _read_transcript(session_factory, meeting_id) is None
        assert _read_minutes(session_factory, meeting_id) is None


def test_llm_schema_error_fails_with_no_minutes() -> None:
    """An LLM schema error sets failed and persists no Meeting_Minutes, while
    the prior-stage Transcript remains (Req 6.6)."""
    with _pipeline_db() as (session_factory, meeting_id):
        result = run_pipeline(
            meeting_id,
            settings=_settings(),
            session_factory=session_factory,
            asr_client=_FakeAsr([{"start": 0.0, "end": 1.5, "text": "你好", "speaker": "发言人A"}]),
            llm_client=_FailingLlm(LlmSchemaError("minutes JSON did not conform to schema")),
            preprocess_fn=_ok_preprocess,
        )
        assert result is JobStatus.FAILED
        job = _read_job(session_factory, meeting_id)
        assert job.status == JobStatus.FAILED.value
        assert (job.error_description or "").strip() != ""
        # Transcript persisted by the earlier successful stage; no minutes.
        assert _read_transcript(session_factory, meeting_id) is not None
        assert _read_minutes(session_factory, meeting_id) is None
