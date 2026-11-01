"""Celery worker pipeline -- ``process_meeting`` and ``run_pipeline`` (task 9.2/9.3).

This module implements the asynchronous meeting-processing pipeline that
advances a ``Processing_Job`` through

    ``queued -> preprocessing -> transcribing -> generating -> completed``

(or to ``failed`` on any stage error), persisting the assembled ``Transcript``
on transcription success and the generated ``Meeting_Minutes`` on generation
success (design: "End-to-End Sequence" + "Failure and Resubmit Flow"; Req 5.7,
6.7, 7.2, 7.3, 7.4).

Two entry points
----------------
* :func:`run_pipeline` -- a plain, fully-injectable callable. This is the
  testable core: tests call it directly with an in-memory ``session_factory``,
  fake/mocked ASR/LLM clients, and a fake ``preprocess_fn``, so the whole
  pipeline runs with **no** real ffmpeg / ASR / LLM / Redis. It returns the
  terminal :class:`~app.enums.JobStatus` (``COMPLETED`` or ``FAILED``) and never
  raises for a business failure.
* :func:`process_meeting` -- the Celery task (registered under
  :data:`~app.worker.celery_app.PROCESS_MEETING_TASK_NAME`) that simply wraps
  :func:`run_pipeline` with production defaults. Because ``run_pipeline`` swallows
  business failures (recording them as ``failed``), the task ends normally and
  Celery never auto-retries a business failure (task 9.1 disables autoretry).

Short-transaction discipline (design: "SQLite write discipline")
----------------------------------------------------------------
Every ``Job_Status`` / result write is its **own** short transaction that
commits **before** the next long-running call (ffmpeg / ASR / LLM). A write
transaction is never held open across one of those calls, so a single in-flight
job cannot block the API or a second worker on the SQLite write lock. The long
calls run with no DB transaction held.

No-partial-results guarantee (Req 5.5/5.6, 6.5/6.6, 7.4)
--------------------------------------------------------
Each result is persisted in a transaction that runs only *after* the
corresponding long call has already returned successfully. Therefore a failure
in a stage means that stage's persistence transaction was never started: an ASR
failure leaves no ``Transcript``; an LLM failure leaves no ``Meeting_Minutes``.
On any failure the job is set to ``failed`` with a non-empty error description.

Injection seams (testability)
------------------------------
:func:`run_pipeline` accepts:

* ``session_factory`` -- a ``sessionmaker`` (defaults to the process-wide
  :func:`app.db.get_sessionmaker`). Each stage opens a fresh short-lived session
  from it.
* ``asr_client`` / ``llm_client`` -- pre-built clients (default: built from
  ``settings`` via :func:`app.clients.factory.make_asr_client` /
  :func:`~app.clients.factory.make_llm_client`, which select the mock impls when
  ``asr_impl``/``llm_impl`` are ``"mock"``).
* ``preprocess_fn`` -- the preprocessing callable (default
  :func:`app.preprocess.preprocess_meeting_audio`), invoked as
  ``preprocess_fn(source_path, output_path, timeout_seconds=...)``.
* ``status_observer`` -- an optional callable invoked with each
  :class:`~app.enums.JobStatus` immediately after that status write commits.
  Tests use it to capture the exact transition order for Property 17.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Iterator

from contextlib import contextmanager

from sqlalchemy import select
from sqlalchemy.orm import Session as OrmSession
from sqlalchemy.orm import sessionmaker

from app.clients.base import AsrClient, AsrError, LlmClient, LlmError
from app.clients.factory import make_asr_client, make_llm_client
from app.clients.types import TranscriptInput, TranscriptSegment as SegmentInput
from app.config import Settings
from app.config_effective import effective_settings
from app.db import get_sessionmaker
from app.enums import UNASSIGNED_OWNER, JobStatus
from app.models import (
    ActionItem,
    MeetingMinutes,
    MeetingRecord,
    ProcessingJob,
    Transcript,
    TranscriptSegment,
)
from app.preprocess import (
    PreprocessError,
    preprocess_failure_description,
    preprocess_meeting_audio,
    standardized_wav_path,
)
from app.worker.celery_app import PROCESS_MEETING_TASK_NAME, celery_app

logger = logging.getLogger(__name__)

__all__ = ["run_pipeline", "process_meeting"]

# Meetings are primarily Chinese (requirements Introduction); the language hint
# is advisory for the LLM client (Req 6.4) and ignored by clients that do their
# own detection.
DEFAULT_LANGUAGE_HINT = "zh"

# Type aliases for the injectable seams.
PreprocessFn = Callable[..., str]
StatusObserver = Callable[[JobStatus], None]


@contextmanager
def _short_txn(session_factory: sessionmaker[OrmSession]) -> Iterator[OrmSession]:
    """Open a fresh session, commit on clean exit, roll back on error, close always.

    Mirrors :func:`app.db.session_scope` but binds to the *injected*
    ``session_factory`` so the worker pipeline can run against a test's
    in-memory database. Each stage uses one of these so its write commits before
    the next long call (no transaction is held across ffmpeg/ASR/LLM).
    """
    session = session_factory()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def _write_status(
    session_factory: sessionmaker[OrmSession],
    meeting_id: str,
    status: JobStatus,
    *,
    error_description: str | None = None,
    observer: StatusObserver | None = None,
) -> None:
    """Set ``Processing_Job.status`` (and error) for ``meeting_id`` in one short txn.

    Commits immediately so the status is durable *before* the worker starts the
    next long call. ``error_description`` is set for ``FAILED`` and cleared
    (``None``) for the success transitions, so a re-run never carries a stale
    error. After the commit, ``observer`` (when provided) is notified with the
    written status -- this is the seam Property 17 uses to capture the exact
    transition order.
    """
    with _short_txn(session_factory) as session:
        job = session.execute(
            select(ProcessingJob).where(ProcessingJob.meeting_id == meeting_id)
        ).scalar_one()
        job.status = status.value
        job.error_description = error_description
        session.add(job)
    if observer is not None:
        observer(status)


def _persist_standardized_path(
    session_factory: sessionmaker[OrmSession], meeting_id: str, path: str
) -> None:
    """Persist the Standardized_Audio path onto the meeting (own short txn)."""
    with _short_txn(session_factory) as session:
        meeting = session.get(MeetingRecord, meeting_id)
        if meeting is None:  # pragma: no cover - defensive; loaded earlier
            raise RuntimeError(f"meeting {meeting_id!r} disappeared mid-pipeline")
        meeting.standardized_audio_path = path
        session.add(meeting)


def _persist_transcript(
    session_factory: sessionmaker[OrmSession],
    meeting_id: str,
    segments: list[SegmentInput],
) -> None:
    """Persist a Transcript and its ordered segments in one short txn.

    Replaces any existing transcript for the meeting so the operation is
    idempotent — safe to call multiple times (e.g. after ASR and again
    after phonetic correction).

    ``ordinal`` is assigned from each segment's position in the assembled list
    (the ASR client guarantees ascending ``start`` order, Req 5.3), so a later
    read ordered by ``ordinal`` reproduces the assembled order exactly.
    """
    with _short_txn(session_factory) as session:
        # Delete any previously persisted transcript for this meeting first.
        old = session.execute(
            select(Transcript).where(Transcript.meeting_id == meeting_id)
        ).scalar_one_or_none()
        if old is not None:
            # Cascade deletes segments via the ORM relationship.
            session.delete(old)
            session.flush()

        transcript = Transcript(meeting_id=meeting_id)
        session.add(transcript)
        session.flush()  # assign transcript.id for the segment FKs
        for ordinal, seg in enumerate(segments):
            session.add(
                TranscriptSegment(
                    transcript_id=transcript.id,
                    start_seconds=float(seg["start"]),
                    end_seconds=float(seg["end"]),
                    text=seg["text"],
                    speaker=seg.get("speaker"),
                    ordinal=ordinal,
                )
            )


def _load_transcript_input(
    session_factory: sessionmaker[OrmSession], meeting_id: str
) -> TranscriptInput:
    """Build the LLM ``TranscriptInput`` from the *persisted* segments.

    Reads the segments back ordered by ``ordinal`` (a read-only short txn) so the
    LLM receives exactly what was stored, then attaches the advisory language
    hint (Req 6.4).
    """
    with _short_txn(session_factory) as session:
        rows = (
            session.execute(
                select(TranscriptSegment)
                .join(Transcript, TranscriptSegment.transcript_id == Transcript.id)
                .where(Transcript.meeting_id == meeting_id)
                .order_by(TranscriptSegment.ordinal)
            )
            .scalars()
            .all()
        )
        segments: list[SegmentInput] = [
            {
                "start": row.start_seconds,
                "end": row.end_seconds,
                "text": row.text,
                "speaker": row.speaker,
            }
            for row in rows
        ]
    return {"segments": segments, "language": DEFAULT_LANGUAGE_HINT}


def _persist_minutes(
    session_factory: sessionmaker[OrmSession], meeting_id: str, minutes_data
) -> None:
    """Persist Meeting_Minutes and its ordered action items in one short txn.

    Each action item's ``owner`` is taken from the LLM result, falling back to
    the explicit unassigned indicator (:data:`app.enums.UNASSIGNED_OWNER`) if a
    client ever returned an empty/missing owner, so the NOT-NULL column always
    holds a non-empty value (Req 6.3).
    """
    with _short_txn(session_factory) as session:
        minutes = MeetingMinutes(
            meeting_id=meeting_id,
            summary=minutes_data["summary"],
            topics=list(minutes_data.get("topics", [])),
            decisions=list(minutes_data.get("decisions", [])),
        )
        session.add(minutes)
        session.flush()  # assign minutes.id for the action-item FKs
        for ordinal, item in enumerate(minutes_data.get("action_items", [])):
            owner = item.get("owner") or UNASSIGNED_OWNER
            session.add(
                ActionItem(
                    minutes_id=minutes.id,
                    description=item["description"],
                    owner=owner,
                    ordinal=ordinal,
                )
            )


def run_pipeline(
    meeting_id: str,
    *,
    settings: Settings | None = None,
    session_factory: sessionmaker[OrmSession] | None = None,
    asr_client: AsrClient | None = None,
    llm_client: LlmClient | None = None,
    preprocess_fn: PreprocessFn | None = None,
    status_observer: StatusObserver | None = None,
) -> JobStatus:
    """Run the full meeting-processing pipeline for ``meeting_id``.

    Advances the job ``preprocessing -> transcribing -> generating ->
    completed``, persisting the Transcript on transcription success and the
    Meeting_Minutes on generation success. On any stage failure the job is set
    to ``failed`` with a non-empty error description and no partial result is
    persisted for the failing stage.

    The function does not raise for business failures (preprocess / ASR / LLM):
    it records them as ``failed`` and returns :attr:`JobStatus.FAILED`. Truly
    unexpected errors are also caught and recorded as ``failed`` so the Celery
    task always ends normally (no queue-level retry, task 9.1). The only thing
    that can propagate is a failure to even *load* the meeting/job or to write
    the failed status itself (a genuine infrastructure fault).

    :returns: :attr:`JobStatus.COMPLETED` on success, :attr:`JobStatus.FAILED`
        on any stage failure, or :attr:`JobStatus.FAILED` likewise if the
        meeting/job cannot be found (after logging).
    """
    session_factory = session_factory if session_factory is not None else get_sessionmaker()
    preprocess_fn = preprocess_fn if preprocess_fn is not None else preprocess_meeting_audio

    def _stage_settings() -> Settings:
        # Injected settings pin the configuration (tests). Otherwise resolve the
        # current Effective_Configuration so each stage observes the latest
        # runtime-editable changes; an in-flight stage keeps the snapshot it
        # began with because each stage resolves once at its start (Req 5.2/5.3).
        if settings is not None:
            return settings
        return effective_settings(session_factory)

    # --- Load the meeting + job (read). Bail if either is missing. ----------
    with _short_txn(session_factory) as session:
        meeting = session.get(MeetingRecord, meeting_id)
        if meeting is None:
            logger.warning("process_meeting: meeting %s not found; nothing to do", meeting_id)
            return JobStatus.FAILED
        job = session.execute(
            select(ProcessingJob).where(ProcessingJob.meeting_id == meeting_id)
        ).scalar_one_or_none()
        if job is None:
            logger.warning(
                "process_meeting: no processing job for meeting %s; nothing to do",
                meeting_id,
            )
            return JobStatus.FAILED
        source_audio_path = meeting.source_audio_path

    def _fail(description: str) -> JobStatus:
        text = (description or "").strip() or "processing failed"
        _write_status(
            session_factory,
            meeting_id,
            JobStatus.FAILED,
            error_description=text,
            observer=status_observer,
        )
        return JobStatus.FAILED

    try:
        # --- Stage 1: preprocessing ----------------------------------------
        stage_settings = _stage_settings()
        _write_status(
            session_factory, meeting_id, JobStatus.PREPROCESSING, observer=status_observer
        )
        output_path = standardized_wav_path(stage_settings.audio_storage_dir, meeting_id)
        try:
            standardized_path = preprocess_fn(
                source_audio_path,
                output_path,
                timeout_seconds=stage_settings.preprocess_timeout_seconds,
            )
        except PreprocessError as exc:
            return _fail(preprocess_failure_description(exc))
        _persist_standardized_path(session_factory, meeting_id, standardized_path)

        # --- Stage 2: transcribing -----------------------------------------
        stage_settings = _stage_settings()
        asr = asr_client if asr_client is not None else make_asr_client(stage_settings)
        _write_status(
            session_factory, meeting_id, JobStatus.TRANSCRIBING, observer=status_observer
        )
        try:
            segments = asr.transcribe(standardized_path)
        except AsrError as exc:
            return _fail(str(exc) or "transcription failed")
        # No partial transcript on failure: this write only runs on success.
        _persist_transcript(session_factory, meeting_id, list(segments))

        # --- Stage 2.5: phonetic correction (optional, best-effort) -----------
        stage_settings = _stage_settings()
        if stage_settings.asr_correction_enabled:
            try:
                llm_corr = make_llm_client(stage_settings)
                orig = _load_transcript_input(session_factory, meeting_id)
                if orig and orig.get("segments"):
                    segs = orig["segments"]
                    # Batch-correct all segments in one windowed LLM call
                    # (group ~25 segments per window, overlap 2 for context).
                    batch_input = [
                        {
                            "index": i,
                            "text": s["text"],
                            "speaker": s.get("speaker"),
                        }
                        for i, s in enumerate(segs)
                    ]
                    results = llm_corr.correct_batch(batch_input)
                    result_map: dict[int, str] = {
                        int(r["index"]): str(r["text"]) for r in results
                    }
                    corrected = [
                        {
                            "start": s["start"],
                            "end": s["end"],
                            "text": result_map.get(i, s["text"]),
                            "speaker": s.get("speaker"),
                        }
                        for i, s in enumerate(segs)
                    ]
                    missing = len(segs) - len(result_map)
                    if missing > 0:
                        logger.warning(
                            "ASR batch correction: %d/%d segments corrected, "
                            "%d kept original for meeting %s",
                            len(result_map),
                            len(segs),
                            missing,
                            meeting_id,
                        )
                    _persist_transcript(session_factory, meeting_id, corrected)
            except LlmError:
                logger.warning(
                    "ASR correction failed for meeting %s; continuing with "
                    "uncorrected transcript",
                    meeting_id,
                )

        # --- Stage 3: generating -------------------------------------------
        stage_settings = _stage_settings()
        llm = llm_client if llm_client is not None else make_llm_client(stage_settings)
        _write_status(
            session_factory, meeting_id, JobStatus.GENERATING, observer=status_observer
        )
        transcript_input = _load_transcript_input(session_factory, meeting_id)
        try:
            minutes_data = llm.generate_minutes(transcript_input)
        except LlmError as exc:
            # Covers both transport (LlmError) and schema (LlmSchemaError) failures.
            return _fail(str(exc) or "minutes generation failed")
        # No partial minutes on failure: this write only runs on success.
        _persist_minutes(session_factory, meeting_id, minutes_data)

        # --- Stage 4: completed --------------------------------------------
        _write_status(
            session_factory, meeting_id, JobStatus.COMPLETED, observer=status_observer
        )
        return JobStatus.COMPLETED
    except Exception as exc:  # noqa: BLE001 - last-resort guard for unexpected faults
        # An unexpected error (not a known business exception) still must not
        # crash the task or trigger a Celery retry: record it as failed.
        logger.exception("process_meeting: unexpected failure for meeting %s", meeting_id)
        return _fail(str(exc) or "unexpected processing error")


@celery_app.task(name=PROCESS_MEETING_TASK_NAME)
def process_meeting(meeting_id: str) -> str:
    """Celery task entry point: run the pipeline with production defaults.

    Registered under :data:`~app.worker.celery_app.PROCESS_MEETING_TASK_NAME`
    (``"process_meeting"``) -- the same name the API enqueues by. It delegates to
    :func:`run_pipeline`, which builds the configured ASR/LLM clients, uses the
    process-wide session factory and the real preprocessor, and records any
    failure as ``Job_Status = failed`` rather than raising (so Celery does not
    auto-retry a business failure, task 9.1).

    :returns: the terminal job status string (for the Celery result backend).
    """
    return run_pipeline(meeting_id).value
