"""Meetings API router -- upload / recording-submit endpoint (task 8).

This module owns ``POST /api/meetings`` (the design's "Upload or submit
recording" endpoint). It is built incrementally across the task-8 sub-tasks;
this file currently implements **task 8.1**: receive the multipart body by
streaming it to a temporary file while counting the bytes actually read, and
enforce the size / non-empty rules before anything is persisted.

Task 8.1 scope (Req 3.3 / 3.4)
------------------------------
- **Stream to a temp file, counting bytes.** The uploaded part is read in
  fixed-size chunks and written to a temp file under the audio storage area.
  The size decision is based on the bytes *actually read*, never on the
  ``Content-Length`` header (which a client may understate or omit) -- see
  :func:`_stream_upload_to_path`.
- **413 the instant the limit is exceeded (Req 3.3).** As soon as the running
  byte count exceeds ``settings.max_upload_bytes`` the loop stops, the partial
  temp file is deleted, and the request is rejected with **413** carrying a
  message naming the maximum allowed size. We do not finish reading the body.
- **400 on a 0-byte upload (Req 3.4).** If the part contains no bytes the temp
  file is removed and the request is rejected with **400** and an "empty file"
  message.
- **No leftover temp files.** Every rejection path (and the interim success
  path) removes the temp file, so a rejected or acknowledged upload never leaves
  a partial file behind. This is also the foundation for the no-orphan guarantee
  hardened in task 8.3 (Req 3.5).

Seam for tasks 8.3 / 8.4
------------------------
Once 8.1's checks pass, the handler hands the validated temp file to
:func:`_finalize_upload`. Task **8.2** (implemented) detects the audio format by
**content inspection** (ffprobe) via the module-level :data:`detect_format`
seam and rejects unsupported content with **415** + a supported-formats message
(Req 3.2) before any record is created. On a supported format it passes the
detected canonical format to :func:`_create_meeting_record`, the remaining seam
that the later sub-tasks fill in:

- **8.3** -- ``fsync`` + atomic move of the temp file to its final location,
  then one DB transaction inserting the ``Meeting_Record`` (with a default
  title and the detected ``source_format``) and a ``Processing_Job(queued)``;
  compensating cleanup on failure (delete file on DB failure, no row on file
  failure) -> **500** with no orphan/partial; return **201** + the new id
  (Req 3.5 / 3.6 / 7.1).
- **8.4** -- enqueue the Celery ``process_meeting`` task after the commit.

Until those land, :func:`_create_meeting_record` performs **no database
write**: it removes the validated temp file and returns a provisional
acknowledgement (echoing the detected ``source_format``) so the endpoint is
reachable end to end and the existing suite stays green. The function
signatures are the stable contract the later tasks build on.

ASGI note: by the time this handler runs, the ASGI server / Starlette has
already parsed the multipart body into an :class:`~fastapi.UploadFile` (spooled
to a temp file). Re-reading it here in chunks lets us enforce the byte budget on
the bytes we actually receive rather than trusting the declared length; the
socket-level cap against truly oversized bodies is Nginx ``client_max_body_size``
(task 14.2).
"""

from __future__ import annotations

import logging
import os
import tempfile
from datetime import datetime
from typing import Annotated, Any
from uuid import uuid4

from fastapi import APIRouter, Depends, File, Response, UploadFile, status
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session as OrmSession

from app.api.deps import CurrentUser
from app.audio.format_detect import detect_audio_format
from app.clients.base import LlmError
from app.clients.factory import make_llm_client
from app.config import Settings, get_settings
from app.db import get_session
from app.enums import UNASSIGNED_OWNER, JobStatus
from app.models import (
    ActionItem,
    MeetingMinutes,
    MeetingRecord,
    ProcessingJob,
    TranscriptSegment,
    utcnow,
)
from app.worker.celery_app import PROCESS_MEETING_TASK_NAME, celery_app

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/meetings", tags=["meetings"])

# Injectable content-based format detector seam (task 8.2).
#
# ``_finalize_upload`` calls this module-level name rather than importing
# :func:`detect_audio_format` directly at the call site so tests can monkeypatch
# ``app.api.meetings.detect_format`` to simulate ffprobe's verdict (a supported
# format string, or ``None`` for unsupported) without needing a real ffprobe
# binary in the environment. Task 8.3 keeps using this same seam.
detect_format = detect_audio_format

# Bytes read per iteration while streaming the upload to disk. Large enough to
# keep syscall overhead low on real (hundreds-of-MB) uploads, small enough that
# the byte budget is enforced promptly. Read returns *up to* this many bytes, so
# small uploads simply come back in a single chunk.
UPLOAD_CHUNK_SIZE = 1024 * 1024  # 1 MiB


def _temp_dir(settings: Settings) -> str:
    """Return (creating if needed) the directory for in-progress upload temps.

    A ``tmp`` subdirectory of ``audio_storage_dir`` keeps partial uploads on the
    same filesystem/volume as the final audio files, which lets task 8.3 finish
    with an atomic same-filesystem ``rename`` rather than a cross-device copy.
    Tests point ``audio_storage_dir`` at a temp path so nothing is written to
    ``/data``.
    """
    tmp = os.path.join(settings.audio_storage_dir, "tmp")
    os.makedirs(tmp, exist_ok=True)
    return tmp


def _remove_quietly(path: str) -> None:
    """Best-effort delete of ``path``; ignore if it is already gone.

    Used on every rejection / acknowledgement path so a partial or consumed
    temp file never lingers. A failure to unlink is logged but not raised -- it
    must not mask the real response (e.g. the 413/400 the user should see).
    """
    try:
        os.remove(path)
    except FileNotFoundError:
        return
    except OSError:  # pragma: no cover - unexpected FS error, log and move on
        logger.warning("failed to remove temp upload file: %s", path, exc_info=True)


async def _stream_upload_to_path(
    upload: UploadFile, dest_path: str, max_bytes: int
) -> tuple[int, bool]:
    """Stream ``upload`` to ``dest_path`` in chunks, enforcing ``max_bytes``.

    Reads the part in :data:`UPLOAD_CHUNK_SIZE` chunks, writing each to
    ``dest_path`` and accumulating the count of bytes *actually read*. The
    moment that running total exceeds ``max_bytes`` the function stops reading
    and returns ``exceeded=True`` (the file on disk is left partial; the caller
    deletes it). The decision never consults ``Content-Length`` -- only the
    bytes observed on the wire (Req 3.3).

    Returns ``(bytes_written, exceeded)`` where ``bytes_written`` is the total
    number of bytes read from the part (which, when ``exceeded`` is True, is the
    count at the point we stopped, i.e. ``> max_bytes``).
    """
    total = 0
    with open(dest_path, "wb") as out:
        while True:
            chunk = await upload.read(UPLOAD_CHUNK_SIZE)
            if not chunk:
                break
            total += len(chunk)
            if total > max_bytes:
                # Exceeded the budget: stop immediately, do not read the rest of
                # the body, and signal the caller to reject with 413.
                return total, True
            out.write(chunk)
    return total, False


def _unsupported_format_response(settings: Settings) -> JSONResponse:
    """Build the 415 rejection naming the supported formats (Req 3.2).

    The message lists the configured ``settings.supported_formats`` (mp3, wav,
    m4a, webm by default) so the client knows what to provide instead.
    """
    supported = list(settings.supported_formats)
    pretty = ", ".join(supported)
    return JSONResponse(
        status_code=status.HTTP_415_UNSUPPORTED_MEDIA_TYPE,
        content={
            "detail": (
                "unsupported audio format; supported formats are: " f"{pretty}"
            ),
            "supported_formats": supported,
        },
    )


def _finalize_upload(
    *,
    db: OrmSession,
    settings: Settings,
    owner: CurrentUser,
    temp_path: str,
    size_bytes: int,
    original_filename: str | None,
) -> JSONResponse:
    """Detect the audio format (task 8.2) then hand off to the create seam.

    Task 8.2 scope (Req 3.2)
    ------------------------
    Inspect the validated temp file's **content** with ffprobe (via the
    module-level :data:`detect_format` seam) -- never its name/extension, since
    recorded blobs have no reliable filename. The detector returns one of the
    canonical Supported_Audio_Format strings or ``None``:

      - **Unsupported / unknown / non-audio / unreadable** (``None``, or a value
        not in ``settings.supported_formats``): delete the temp file and reject
        with **415** + a message naming mp3/wav/m4a/webm. No Meeting_Record is
        created.
      - **Supported**: pass the detected canonical format along to
        :func:`_create_meeting_record` (the task-8.3 seam) so it can be stored
        as ``Meeting_Record.source_format``.

    Create handoff (tasks 8.3 / 8.4)
    --------------------------------
    On a supported format, :func:`_create_meeting_record` performs the atomic
    create (task 8.3): ``fsync`` + atomic move of ``temp_path`` to its final
    location, one DB transaction inserting the ``Meeting_Record`` (+ default
    title) and a ``Processing_Job(queued)`` with compensating cleanup on
    failure -> 500 with no orphan/partial (Req 3.5 / 3.6 / 7.1), and a
    post-commit enqueue hook for Celery (task 8.4, currently a no-op).
    """
    detected_format = detect_format(temp_path)
    if detected_format is None or detected_format not in settings.supported_formats:
        # Req 3.2: content is not a Supported_Audio_Format -> 415, no record.
        _remove_quietly(temp_path)
        return _unsupported_format_response(settings)

    # Supported: continue to the create seam, carrying the detected format so
    # task 8.3 can persist it as Meeting_Record.source_format.
    return _create_meeting_record(
        db=db,
        settings=settings,
        owner=owner,
        temp_path=temp_path,
        size_bytes=size_bytes,
        original_filename=original_filename,
        source_format=detected_format,
    )


def _default_title(original_filename: str | None, created_at: datetime) -> str:
    """Derive the default ``Meeting_Record.title`` (design: POST /api/meetings).

    Rule (exact):
      - When an ``original_filename`` is present and non-blank (a real file
        upload), the title defaults to that filename verbatim (after trimming
        surrounding whitespace) -- e.g. ``"team-sync.m4a"``.
      - Otherwise (a recorded blob, which carries no reliable name), the title
        defaults to ``会议-<created_at>`` where ``<created_at>`` is the meeting's
        creation instant formatted to **minute** precision in ISO form, e.g.
        ``会议-2026-06-17T11:33``.

    The same ``created_at`` is used both here and as the persisted
    ``Meeting_Record.created_at`` so the title and the row agree. The title is
    user-editable later (task 11).
    """
    if original_filename is not None:
        name = original_filename.strip()
        if name:
            return name
    return f"会议-{created_at.strftime('%Y-%m-%dT%H:%M')}"


def _final_audio_path(settings: Settings, meeting_id: str, source_format: str) -> str:
    """Compute the final on-disk path for a meeting's source audio.

    The file is named by the pre-generated ``meeting_id`` so the filename and
    the ``Meeting_Record.id`` always match (1:1, collision-free via uuid4), and
    the extension is the detected canonical ``source_format``. The file lives
    directly under ``settings.audio_storage_dir`` -- the same volume as the
    ``tmp`` staging area, so the move below is a same-filesystem rename.
    """
    return os.path.join(settings.audio_storage_dir, f"{meeting_id}.{source_format}")


def _fsync_and_move(temp_path: str, final_path: str) -> None:
    """Durably move ``temp_path`` to ``final_path`` (fsync, then atomic rename).

    Ensures the uploaded bytes are flushed to stable storage (``fsync`` the file)
    before atomically moving it into place with :func:`os.replace` (a same-
    filesystem rename -- ``temp_path`` already lives under ``audio_storage_dir/tmp``).
    The containing directory is fsync'd afterwards so the rename itself survives a
    crash. Raises on any I/O failure; the caller runs compensating cleanup.
    """
    fd = os.open(temp_path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)

    os.replace(temp_path, final_path)

    # Persist the directory entry for the rename (best-effort: not all
    # platforms permit opening a directory for fsync).
    dir_path = os.path.dirname(final_path) or "."
    try:
        dir_fd = os.open(dir_path, os.O_RDONLY)
    except OSError:  # pragma: no cover - platform without dir fsync support
        return
    try:
        os.fsync(dir_fd)
    finally:
        os.close(dir_fd)


def _persist_meeting_record(
    db: OrmSession,
    *,
    meeting_id: str,
    owner_id: str,
    title: str,
    source_audio_path: str,
    source_format: str,
    created_at: datetime,
) -> None:
    """Insert the ``Meeting_Record`` + queued ``Processing_Job`` in one txn.

    This is the **single DB transaction** of the atomic create: it adds both the
    ``Meeting_Record`` (with the pre-generated ``meeting_id`` so the row id
    matches the audio filename) and its one-to-one ``Processing_Job`` seeded to
    ``queued`` (Req 7.1), then commits. Either both rows are committed or, on any
    error, neither is (the caller rolls back).

    Factored out as a named seam so a test can monkeypatch
    ``app.api.meetings._persist_meeting_record`` to force a DB failure and assert
    the compensating cleanup leaves no row and no file (Req 3.5).
    """
    record = MeetingRecord(
        id=meeting_id,
        owner_id=owner_id,
        title=title,
        source_audio_path=source_audio_path,
        source_format=source_format,
        created_at=created_at,
    )
    job = ProcessingJob(meeting_id=meeting_id, status=JobStatus.QUEUED.value)
    db.add(record)
    db.add(job)
    db.commit()


def _enqueue_processing_job(meeting_id: str) -> None:
    """Post-commit enqueue of the Celery ``process_meeting`` task (task 8.4).

    Called by :func:`_create_meeting_record` **only after** the create
    transaction has committed, so the worker can never dequeue a meeting whose
    row does not yet exist. The job is enqueued **by name** via
    :data:`~app.worker.celery_app.PROCESS_MEETING_TASK_NAME` so the API does not
    need the (task-9) task body importable; task 9.1 registers the function
    under that same name and ``send_task`` continues to work unchanged.

    Enqueue-failure policy (decision, Req 7.1 / 7.6)
    ------------------------------------------------
    At this point the ``Meeting_Record`` and its ``Processing_Job(queued)`` are
    already committed (Req 7.1). If handing the message to the broker fails --
    Redis unreachable, a connection error, or even a configuration-load error --
    we **log the failure and return normally without failing the request**. The
    upload still returns **201** and the meeting exists with status ``queued``;
    the operator/user can re-drive it through the explicit resubmit path (Req
    7.6, task 10) or a later sweep, exactly as for any job that fails after
    enqueue. Failing the request here would be worse: it would imply nothing was
    created when in fact the durable record exists. (We deliberately do **not**
    roll back the committed row on an enqueue error -- losing the user's
    uploaded audio is the more harmful outcome.)

    This is the single seam the API uses to reach Celery, so tests can either
    monkeypatch this function directly or spy on ``celery_app.send_task``.
    """
    try:
        celery_app.send_task(PROCESS_MEETING_TASK_NAME, args=[meeting_id])
    except Exception:
        # Broker unreachable / connection / config error: the row is committed
        # and queued, so log and continue -- the request still succeeds (201).
        logger.exception(
            "failed to enqueue %s for meeting %s; the job remains queued and "
            "can be resubmitted",
            PROCESS_MEETING_TASK_NAME,
            meeting_id,
        )
    return None


def _internal_error_response(detail: str) -> JSONResponse:
    """Build the 500 used when the atomic create cannot complete (Req 3.5)."""
    return JSONResponse(
        status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
        content={"detail": detail},
    )


def _create_meeting_record(
    *,
    db: OrmSession,
    settings: Settings,
    owner: CurrentUser,
    temp_path: str,
    size_bytes: int,
    original_filename: str | None,
    source_format: str,
) -> JSONResponse:
    """Atomically persist a *validated, format-detected* upload (task 8.3).

    Ordering and atomicity (design "Atomicity at boundaries"; Req 3.5/3.6/7.1):

    1. **Pre-generate the meeting id** (uuid4) so the final audio filename and
       the ``Meeting_Record.id`` are identical -- file and row are consistent by
       construction, with no second pass to reconcile a model-generated id.
    2. **fsync + atomic move** the temp file to
       ``{audio_storage_dir}/{meeting_id}.{source_format}`` via
       :func:`_fsync_and_move` (same-filesystem rename; bytes are durable first).
    3. In **one DB transaction** (:func:`_persist_meeting_record`) insert the
       ``Meeting_Record`` (``owner_id``, default ``title``, ``source_audio_path``
       = the moved file, ``source_format``) and a ``Processing_Job(queued)``
       (Req 7.1), then commit. The request owns the commit (the ``get_session``
       dependency does not auto-commit).
    4. **Enqueue** hook for task 8.4 runs only after a successful commit.
    5. Return **201** with ``{"id": <meeting_id>}`` (Req 3.6), plus ``status`` and
       ``title`` for convenience.

    Compensating cleanup (no orphan / no partial, Req 3.5):
      - **File move fails** -> no row is created; the temp file and any partial
        final file are removed; respond **500**.
      - **DB insert/commit fails** -> the already-moved final file is deleted and
        the transaction rolled back, so no row remains and no stray file is left;
        respond **500**.

    The two failure points are isolated behind the :func:`_fsync_and_move` and
    :func:`_persist_meeting_record` seams so tests can force either failure and
    assert the no-orphan/no-partial guarantee.
    """
    meeting_id = str(uuid4())
    created_at = utcnow()
    final_path = _final_audio_path(settings, meeting_id, source_format)

    # Final audio files live directly under audio_storage_dir; ensure it exists
    # (the tmp staging subdir is created separately when streaming the upload).
    os.makedirs(settings.audio_storage_dir, exist_ok=True)

    # --- Step 1+2: durably move temp -> final (atomic rename). -----------
    try:
        _fsync_and_move(temp_path, final_path)
    except Exception:
        logger.exception("failed to store uploaded audio file")
        # No row was created. Clean any partial: the temp (if the rename did not
        # consume it) and the final (if a partial landed there).
        _remove_quietly(temp_path)
        _remove_quietly(final_path)
        return _internal_error_response("failed to store the uploaded audio file")

    # --- Step 3: one DB transaction (record + queued job). ---------------
    title = _default_title(original_filename, created_at)
    try:
        _persist_meeting_record(
            db,
            meeting_id=meeting_id,
            owner_id=owner.id,
            title=title,
            source_audio_path=final_path,
            source_format=source_format,
            created_at=created_at,
        )
    except Exception:
        logger.exception("failed to persist meeting record; rolling back")
        # Compensate: roll back so no row remains, and delete the moved file so
        # no orphan audio is left on disk (Req 3.5).
        try:
            db.rollback()
        except Exception:  # pragma: no cover - rollback itself failing
            logger.exception("rollback failed during compensating cleanup")
        _remove_quietly(final_path)
        return _internal_error_response("failed to create the meeting record")

    # --- Step 4: post-commit enqueue of the Celery process_meeting task. --
    _enqueue_processing_job(meeting_id)

    # --- Step 5: 201 + the new record id (Req 3.6). ----------------------
    return JSONResponse(
        status_code=status.HTTP_201_CREATED,
        content={
            "id": meeting_id,
            "status": JobStatus.QUEUED.value,
            "title": title,
        },
    )


@router.post(
    "",
    responses={
        status.HTTP_201_CREATED: {"description": "Upload accepted"},
        status.HTTP_400_BAD_REQUEST: {"description": "Empty (0-byte) upload"},
        status.HTTP_413_CONTENT_TOO_LARGE: {
            "description": "Upload exceeds the configured maximum size"
        },
    },
)
async def create_meeting(
    user: CurrentUser,
    db: Annotated[OrmSession, Depends(get_session)],
    settings: Annotated[Settings, Depends(get_settings)],
    file: Annotated[UploadFile, File(description="The meeting audio file")],
) -> Any:
    """Receive an audio upload, enforcing the size / non-empty rules (task 8.1).

    Streams the multipart ``file`` part to a temp file while counting bytes,
    then:
      - rejects with **413** (and the max-size message) the instant the byte
        count exceeds ``settings.max_upload_bytes`` -- without trusting
        ``Content-Length`` (Req 3.3);
      - rejects with **400** when the part is 0 bytes (Req 3.4);
      - otherwise hands the validated temp file to :func:`_finalize_upload`
        (the seam where tasks 8.2/8.3/8.4 add format detection, the atomic
        record create + 201, and Celery enqueue).

    The route declares :data:`CurrentUser`, so an absent/invalid token is
    rejected with a no-content 401 before any byte is read (Req 1.1).
    """
    max_bytes = settings.max_upload_bytes
    tmp_dir = _temp_dir(settings)

    # Create a uniquely-named temp file we fully control (so cleanup is precise).
    fd, temp_path = tempfile.mkstemp(dir=tmp_dir, prefix="upload-", suffix=".part")
    os.close(fd)  # we reopen via _stream_upload_to_path; just reserving the name

    try:
        size_bytes, exceeded = await _stream_upload_to_path(
            file, temp_path, max_bytes
        )

        if exceeded:
            # Req 3.3: too large -> 413 + a message naming the maximum size.
            _remove_quietly(temp_path)
            return JSONResponse(
                status_code=status.HTTP_413_CONTENT_TOO_LARGE,
                content={
                    "detail": (
                        "uploaded file exceeds the maximum allowed size of "
                        f"{max_bytes} bytes"
                    ),
                    "max_upload_bytes": max_bytes,
                },
            )

        if size_bytes == 0:
            # Req 3.4: empty file -> 400 + an "empty" message.
            _remove_quietly(temp_path)
            return JSONResponse(
                status_code=status.HTTP_400_BAD_REQUEST,
                content={"detail": "uploaded file is empty"},
            )

        # Validated: non-empty and within the size budget. Hand to the seam.
        return _finalize_upload(
            db=db,
            settings=settings,
            owner=user,
            temp_path=temp_path,
            size_bytes=size_bytes,
            original_filename=file.filename,
        )
    except Exception:
        # Any unexpected failure must not leave a partial temp file behind.
        _remove_quietly(temp_path)
        raise


# --------------------------------------------------------------------------- #
# Task 10: job status + resubmit endpoints (Req 7.5, 7.6, 7.7, 7.8)
# --------------------------------------------------------------------------- #
#
# Both endpoints below are guarded by :data:`CurrentUser`, so an absent/invalid
# token is rejected with a no-content 401 before any work happens (Req 1.1).
# They are owner-scoped: a meeting that does not exist *or* is not owned by the
# requesting user is treated identically as **not-found (404)**, so the API
# never discloses the existence of another user's meeting (consistent with the
# design's 404 for inaccessible records, Req 8.5).


def _not_found_response() -> JSONResponse:
    """Build the 404 used when a meeting/job is unknown or not owned (Req 7.7).

    Owner-scoping collapses "no such meeting" and "someone else's meeting" into
    the same not-found result so ownership is never disclosed.
    """
    return JSONResponse(
        status_code=status.HTTP_404_NOT_FOUND,
        content={"detail": "meeting not found"},
    )


def _load_owned_job(
    db: OrmSession, *, owner_id: str, meeting_id: str
) -> ProcessingJob | None:
    """Return the ``Processing_Job`` for an *owned* meeting, else ``None``.

    Resolves to ``None`` when the ``Meeting_Record`` does not exist, is owned by
    a different user, or (defensively) has no associated ``Processing_Job``. In
    every such case the caller responds 404 -- unknown and foreign meetings are
    indistinguishable to the client (Req 7.7 / 8.5). The owner filter is part of
    the meeting lookup so a foreign id can never reach the job at all.
    """
    record = db.execute(
        select(MeetingRecord).where(
            MeetingRecord.id == meeting_id,
            MeetingRecord.owner_id == owner_id,
        )
    ).scalar_one_or_none()
    if record is None:
        return None
    return db.execute(
        select(ProcessingJob).where(ProcessingJob.meeting_id == meeting_id)
    ).scalar_one_or_none()


@router.get(
    "/{meeting_id}/status",
    responses={
        status.HTTP_200_OK: {"description": "Current job status"},
        status.HTTP_404_NOT_FOUND: {"description": "Unknown or not-owned meeting"},
    },
)
def get_meeting_status(
    meeting_id: str,
    user: CurrentUser,
    db: Annotated[OrmSession, Depends(get_session)],
) -> Any:
    """Return the current ``Job_Status`` for an owned meeting (task 10.1).

    Behavior (Req 7.5 / 7.7):
      - **200** with ``{"status": <Job_Status>}`` for an existing owned
        meeting's job. The recorded ``error_description`` is included **only**
        when the status is ``failed`` (it is omitted entirely for every other
        status, even if a stale description lingered on the row).
      - **404** when the meeting/job does not exist or is not owned by the
        requesting user -- unknown and foreign meetings are indistinguishable
        (Req 7.7 / 8.5).
    """
    job = _load_owned_job(db, owner_id=user.id, meeting_id=meeting_id)
    if job is None:
        return _not_found_response()

    content: dict[str, Any] = {"status": job.status}
    # Surface the error description exactly when the job has failed (Req 7.5).
    if job.status == JobStatus.FAILED.value:
        content["error_description"] = job.error_description
    return JSONResponse(status_code=status.HTTP_200_OK, content=content)


@router.post(
    "/{meeting_id}/resubmit",
    responses={
        status.HTTP_200_OK: {"description": "Failed job reset to queued and re-enqueued"},
        status.HTTP_404_NOT_FOUND: {"description": "Unknown or not-owned meeting"},
        status.HTTP_409_CONFLICT: {
            "description": "Job is not in the failed state; resubmission rejected"
        },
    },
)
def resubmit_meeting(
    meeting_id: str,
    user: CurrentUser,
    db: Annotated[OrmSession, Depends(get_session)],
) -> Any:
    """Resubmit an owned meeting's **failed** job (task 10.2).

    Behavior (Req 7.6 / 7.7 / 7.8):
      - **404** when the meeting/job does not exist or is not owned (Req 7.7 /
        8.5) -- checked first, before any state inspection.
      - When the job's status is ``failed`` (Req 7.6): reset it to ``queued``
        and clear the stale ``error_description`` in a single short transaction,
        commit, then re-enqueue the Celery ``process_meeting`` task **after the
        commit** via the same :func:`_enqueue_processing_job` seam used by
        upload (so the worker can never dequeue a meeting whose reset is not yet
        durable). Respond **200** with the new ``queued`` status.
      - When the job's status is anything other than ``failed`` (Req 7.8):
        reject with **409** and **preserve** the existing status -- no write, no
        enqueue. The current status is echoed so the client can see why.
    """
    job = _load_owned_job(db, owner_id=user.id, meeting_id=meeting_id)
    if job is None:
        return _not_found_response()

    # Req 7.8: only a failed job may be resubmitted; otherwise reject and
    # preserve the existing status (no state change, no re-enqueue).
    if job.status != JobStatus.FAILED.value:
        return JSONResponse(
            status_code=status.HTTP_409_CONFLICT,
            content={
                "detail": (
                    "only a failed job may be resubmitted; current status is "
                    f"'{job.status}'"
                ),
                "status": job.status,
            },
        )

    # Req 7.6: reset failed -> queued in a single short transaction. The stale
    # error description belongs to the failed attempt, so clear it now that the
    # job is freshly queued.
    job.status = JobStatus.QUEUED.value
    job.error_description = None
    db.commit()

    # Re-enqueue only after the reset is committed, via the SAME seam as upload.
    _enqueue_processing_job(meeting_id)

    return JSONResponse(
        status_code=status.HTTP_200_OK,
        content={"id": meeting_id, "status": JobStatus.QUEUED.value},
    )


# --------------------------------------------------------------------------- #
# Task 11: meeting listing, detail, edit, export, delete endpoints
# (Req 8.1, 8.2, 8.4, 8.5, 9.2, 9.3, 9.4, 9.6, 9.7)
# --------------------------------------------------------------------------- #
#
# Every endpoint below is guarded by :data:`CurrentUser` (absent/invalid token
# -> no-content 401, Req 1.1) and is **owner-scoped**: a meeting that does not
# exist *or* is owned by another user is treated identically as **404** via
# :func:`_not_found_response`, so the API never discloses another user's data
# (Req 8.5, Property 23). Record-level access goes through
# :func:`_load_owned_meeting`, the record analogue of :func:`_load_owned_job`.
#
# Export / edit "no minutes" status-code choice (Req 9.7)
# -------------------------------------------------------
# When an export (or edit) is requested for a meeting that exists and is owned
# by the caller but has no persisted Meeting_Minutes yet, we reject with **409
# Conflict**, not 404. Rationale: 404 is reserved exclusively for owner-scoping
# (unknown/foreign meeting) so it never leaks whether a meeting exists; a 404
# here would be indistinguishable from "no such meeting". The meeting *does*
# exist -- the request simply conflicts with its current state (no minutes
# generated yet), which is the same 409 semantics already used by the resubmit
# endpoint for a wrong-state job. See :func:`_no_minutes_response`.


def _load_owned_meeting(
    db: OrmSession, *, owner_id: str, meeting_id: str
) -> MeetingRecord | None:
    """Return the ``Meeting_Record`` for an *owned* meeting, else ``None``.

    The record analogue of :func:`_load_owned_job`: resolves to ``None`` when no
    meeting with ``meeting_id`` exists *or* it is owned by a different user, so
    every caller can collapse "unknown" and "foreign" into a single 404 and
    never disclose another user's meeting (Req 8.5, Property 23). The owner
    filter is part of the query, so a foreign id never loads a row.
    """
    return db.execute(
        select(MeetingRecord).where(
            MeetingRecord.id == meeting_id,
            MeetingRecord.owner_id == owner_id,
        )
    ).scalar_one_or_none()


def _no_minutes_response(action: str) -> JSONResponse:
    """Build the 409 used when a meeting has no minutes to export/edit (Req 9.7).

    See the module section comment for why this is 409 (state conflict) rather
    than 404 (which is reserved for owner-scoping and must not leak existence).
    """
    return JSONResponse(
        status_code=status.HTTP_409_CONFLICT,
        content={"detail": f"no meeting minutes exist to {action}"},
    )


def _meeting_status(record: MeetingRecord) -> str | None:
    """Return the meeting's Job_Status, or ``None`` if (defensively) no job."""
    return record.job.status if record.job is not None else None


def _serialize_transcript(record: MeetingRecord) -> dict[str, Any] | None:
    """Serialize the persisted Transcript (ordered segments) or ``None``.

    Returns ``None`` exactly when no Transcript has been persisted for the
    meeting (Req 8.2 / Property 22). Segments are ordered by ``ordinal`` -- the
    position the ASR client assembled them in (ascending start, Req 5.3) -- so
    the read reproduces the stored order.
    """
    transcript = record.transcript
    if transcript is None:
        return None
    segments = sorted(transcript.segments, key=lambda seg: seg.ordinal)
    return {
        "segments": [
            {
                "start": seg.start_seconds,
                "end": seg.end_seconds,
                "text": seg.text,
                "speaker": seg.speaker,
            }
            for seg in segments
        ]
    }


def _serialize_minutes(record: MeetingRecord) -> dict[str, Any] | None:
    """Serialize the persisted Meeting_Minutes or ``None``.

    Returns ``None`` exactly when no Meeting_Minutes have been persisted
    (Req 8.2 / Property 22). Action items are ordered by ``ordinal`` and each
    carries its owner (Req 9.1 -- the unassigned indicator was substituted at
    write time when no owner was identified, Req 6.3).
    """
    minutes = record.minutes
    if minutes is None:
        return None
    items = sorted(minutes.action_items, key=lambda item: item.ordinal)
    return {
        "summary": minutes.summary,
        "topics": list(minutes.topics),
        "decisions": list(minutes.decisions),
        "action_items": [
            {"description": item.description, "owner": item.owner} for item in items
        ],
    }


class SegmentUpdate(BaseModel):
    """A single transcript segment for update."""

    start: float
    end: float
    text: str
    speaker: str | None = None


class TranscriptUpdateRequest(BaseModel):
    """Request body for replacing transcript segments."""

    segments: list[SegmentUpdate] = Field(..., min_length=1)


class CorrectionRequest(BaseModel):
    """Request body for post-ASR phonetic correction."""

    text: str = Field(..., min_length=1, max_length=50000)
    context: str | None = Field(default=None, max_length=100000)


class BatchCorrectionSegment(BaseModel):
    """One segment in a batch-correction request."""

    index: int = Field(..., ge=0)
    text: str = Field(..., min_length=1)
    speaker: str | None = None


class CorrectBatchRequest(BaseModel):
    """Request body for batch post-ASR phonetic correction."""

    segments: list[BatchCorrectionSegment] = Field(..., min_length=1, max_length=500)


class ActionItemEdit(BaseModel):
    """One action item in a minutes-edit request (Req 9.2)."""

    description: str = ""
    owner: str = ""


class MinutesEdit(BaseModel):
    """The body of ``PUT /api/meetings/{id}/minutes`` (Req 9.2).

    A full replacement of the editable minutes content: ``summary`` plus the
    topics / decisions lists and the action-item set. Fields default to empty so
    a partial body clears the corresponding section rather than 422-ing.
    """

    summary: str = ""
    topics: list[str] = Field(default_factory=list)
    decisions: list[str] = Field(default_factory=list)
    action_items: list[ActionItemEdit] = Field(default_factory=list)


def _normalize_owner(owner: str | None) -> str:
    """Return ``owner`` verbatim when non-blank, else the unassigned indicator.

    Req 6.3 / task 11.3: every action item owner must be non-empty. A blank
    (empty or whitespace-only) owner defaults to
    :data:`app.enums.UNASSIGNED_OWNER`; a non-blank owner is kept exactly as
    given (not trimmed) so a real name round-trips unchanged.
    """
    if owner is not None and owner.strip():
        return owner
    return UNASSIGNED_OWNER


def _persist_minutes_edit(
    db: OrmSession,
    minutes: MeetingMinutes,
    *,
    summary: str,
    topics: list[str],
    decisions: list[str],
    action_items: list[tuple[str, str]],
) -> None:
    """Replace a meeting's minutes content in **one** transaction (Req 9.2/9.6).

    Overwrites ``summary``/``topics``/``decisions`` and replaces the entire
    action-item set (the old rows are removed by the ``delete-orphan`` cascade
    when the collection is reassigned), assigning a fresh ``ordinal`` from list
    position so a later read reproduces the submitted order. Commits once; the
    whole edit is therefore atomic -- either every change lands or, on any error,
    the caller rolls back and the previously stored minutes are left unchanged
    (Req 9.6).

    Isolated as a named seam (like :func:`_persist_meeting_record`) so a test can
    monkeypatch ``app.api.meetings._persist_minutes_edit`` to force a failure and
    assert the prior minutes survive (Property 24).
    """
    minutes.summary = summary
    minutes.topics = list(topics)
    minutes.decisions = list(decisions)
    minutes.action_items = [
        ActionItem(description=description, owner=owner, ordinal=ordinal)
        for ordinal, (description, owner) in enumerate(action_items)
    ]
    db.commit()


def _render_minutes_markdown(record: MeetingRecord, minutes: MeetingMinutes) -> str:
    """Render the minutes as a single Markdown document (Req 9.3 / Property 25).

    The document contains the summary, every topic, every decision, and every
    action item together with its owner -- each value written verbatim so the
    export is a faithful, shareable record. Action items are emitted in
    ``ordinal`` order.
    """
    items = sorted(minutes.action_items, key=lambda item: item.ordinal)
    lines: list[str] = [
        f"# {record.title}",
        "",
        "## 摘要",
        minutes.summary,
        "",
        "## 议题",
    ]
    lines.extend(f"- {topic}" for topic in minutes.topics)
    lines.extend(["", "## 决议"])
    lines.extend(f"- {decision}" for decision in minutes.decisions)
    lines.extend(["", "## 行动项"])
    lines.extend(f"- {item.description}（负责人：{item.owner}）" for item in items)
    lines.append("")
    return "\n".join(lines)


def _render_minutes_docx(record: MeetingRecord, minutes: MeetingMinutes) -> bytes:
    """Render the minutes as a well-formatted Word (.docx) document.

    Produces a Chinese-friendly document with proper font settings,
    black headings, and a clean layout suitable for sharing and printing.
    """
    from io import BytesIO

    from docx import Document
    from docx.shared import Pt, Inches, Cm, RGBColor
    from docx.enum.text import WD_ALIGN_PARAGRAPH
    from docx.oxml.ns import qn

    doc = Document()

    # -- Global defaults: Chinese-friendly font, comfortable margins --
    style = doc.styles["Normal"]
    font = style.font
    font.name = "微软雅黑"
    font.size = Pt(11)
    style.paragraph_format.space_after = Pt(6)
    style.paragraph_format.line_spacing = 1.35

    def _set_east_asian(run: Any, font_name: str = "微软雅黑") -> None:
        """Safely set the East-Asian font on a run, creating rPr if needed."""
        rPr = run._element.get_or_add_rPr()
        rFonts = rPr.find(qn("w:rFonts"))
        if rFonts is None:
            from lxml import etree
            rFonts = etree.SubElement(rPr, qn("w:rFonts"))
        rFonts.set(qn("w:eastAsia"), font_name)

    # Thin page margins for more content per page.
    for section in doc.sections:
        section.top_margin = Cm(2.0)
        section.bottom_margin = Cm(2.0)
        section.left_margin = Cm(2.5)
        section.right_margin = Cm(2.5)

    # -- Title line --
    title = doc.add_paragraph()
    title.alignment = WD_ALIGN_PARAGRAPH.CENTER
    run = title.add_run(record.title)
    run.bold = True
    run.font.size = Pt(18)
    run.font.color.rgb = RGBColor(0x1A, 0x1A, 0x1A)
    _set_east_asian(run)

    # Date subtitle
    created = record.created_at.strftime("%Y年%m月%d日 %H:%M") if record.created_at else ""
    if created:
        date_para = doc.add_paragraph()
        date_para.alignment = WD_ALIGN_PARAGRAPH.CENTER
        date_run = date_para.add_run(created)
        date_run.font.size = Pt(9)
        date_run.font.color.rgb = RGBColor(0x88, 0x88, 0x88)
        _set_east_asian(date_run)
        date_para.paragraph_format.space_after = Pt(18)

    def _add_section_heading(text: str) -> None:
        """Add a clean black section heading with Chinese font."""
        h = doc.add_paragraph()
        run = h.add_run(text)
        run.bold = True
        run.font.size = Pt(13)
        run.font.color.rgb = RGBColor(0x33, 0x33, 0x33)
        _set_east_asian(run)
        h.paragraph_format.space_before = Pt(16)
        h.paragraph_format.space_after = Pt(8)

    def _add_body(text: str) -> None:
        """Add a normal body paragraph."""
        p = doc.add_paragraph(text)
        for run in p.runs:
            _set_east_asian(run)

    def _add_bullet(text: str) -> None:
        """Add a bullet item with Chinese-friendly rendering."""
        p = doc.add_paragraph(style="List Bullet")
        # Clear default run and add our own.
        p.clear()
        run = p.add_run(text)
        run.font.size = Pt(11)
        _set_east_asian(run)
        p.paragraph_format.space_after = Pt(3)

    # -- Summary --
    _add_section_heading("摘要")
    summary_para = doc.add_paragraph()
    summary_run = summary_para.add_run(minutes.summary)
    summary_run.font.size = Pt(11)
    _set_east_asian(summary_run)
    summary_para.paragraph_format.first_line_indent = Cm(0.7)
    summary_para.paragraph_format.line_spacing = 1.5

    # -- Topics --
    _add_section_heading("议题")
    if minutes.topics:
        for topic in minutes.topics:
            _add_bullet(topic)
    else:
        _add_body("（无）")

    # -- Decisions --
    _add_section_heading("决议")
    if minutes.decisions:
        for decision in minutes.decisions:
            _add_bullet(decision)
    else:
        _add_body("（无）")

    # -- Action Items (as a simple table for clarity) --
    _add_section_heading("行动项")
    items = sorted(minutes.action_items, key=lambda item: item.ordinal)
    if items:
        table = doc.add_table(rows=1, cols=2)
        table.style = "Light Grid Accent 1"
        # Header row
        hdr = table.rows[0].cells
        hdr[0].text = "事项"
        hdr[1].text = "负责人"
        for cell in hdr:
            for p in cell.paragraphs:
                p.runs[0].bold = True
                p.runs[0].font.size = Pt(10)
                _set_east_asian(p.runs[0])
        # Data rows
        for item in items:
            row = table.add_row()
            row.cells[0].text = item.description
            row.cells[1].text = item.owner
            for cell in row.cells:
                for p in cell.paragraphs:
                    for run in p.runs:
                        run.font.size = Pt(10)
                        _set_east_asian(run)
        # Set column widths
        for row_obj in table.rows:
            row_obj.cells[0].width = Cm(11)
            row_obj.cells[1].width = Cm(4)
    else:
        _add_body("（无）")

    buf = BytesIO()
    doc.save(buf)
    return buf.getvalue()


@router.get(
    "",
    responses={
        status.HTTP_200_OK: {"description": "The current user's meetings"},
    },
)
def list_meetings(
    user: CurrentUser,
    db: Annotated[OrmSession, Depends(get_session)],
) -> Any:
    """List the current user's meetings, newest first (task 11.1).

    Returns only the requesting user's Meeting_Records (owner-scoped, Req 8.1),
    each as ``{id, title, status, created_at}``, ordered by ``created_at``
    descending with ties broken by ``id`` descending (Req 8.3). When the user has
    no meetings the response is **200** with an empty list (Req 8.4).
    """
    records = (
        db.execute(
            select(MeetingRecord)
            .where(MeetingRecord.owner_id == user.id)
            .order_by(MeetingRecord.created_at.desc(), MeetingRecord.id.desc())
        )
        .scalars()
        .all()
    )
    items = [
        {
            "id": record.id,
            "title": record.title,
            "status": _meeting_status(record),
            "created_at": record.created_at.isoformat(),
        }
        for record in records
    ]
    return JSONResponse(status_code=status.HTTP_200_OK, content=items)


@router.get(
    "/{meeting_id}",
    responses={
        status.HTTP_200_OK: {"description": "Meeting metadata + transcript + minutes"},
        status.HTTP_404_NOT_FOUND: {"description": "Unknown or not-owned meeting"},
    },
)
def get_meeting_detail(
    meeting_id: str,
    user: CurrentUser,
    db: Annotated[OrmSession, Depends(get_session)],
) -> Any:
    """Return a meeting's metadata plus its transcript/minutes (task 11.2).

    For an owned meeting (Req 8.2): returns ``id``/``title``/``status``/
    ``created_at`` and includes the Transcript and the Meeting_Minutes **exactly
    when each has been persisted** -- the ``transcript`` / ``minutes`` fields are
    ``null`` until their stage completes (Property 22). For an unknown or foreign
    meeting: **404** with no data disclosed (Req 8.5, Property 23).
    """
    record = _load_owned_meeting(db, owner_id=user.id, meeting_id=meeting_id)
    if record is None:
        return _not_found_response()
    content = {
        "id": record.id,
        "title": record.title,
        "status": _meeting_status(record),
        "created_at": record.created_at.isoformat(),
        "transcript": _serialize_transcript(record),
        "minutes": _serialize_minutes(record),
    }
    return JSONResponse(status_code=status.HTTP_200_OK, content=content)


@router.put(
    "/{meeting_id}/minutes",
    responses={
        status.HTTP_200_OK: {"description": "Edited minutes persisted"},
        status.HTTP_404_NOT_FOUND: {"description": "Unknown or not-owned meeting"},
        status.HTTP_409_CONFLICT: {"description": "Meeting has no minutes to edit"},
        status.HTTP_500_INTERNAL_SERVER_ERROR: {
            "description": "Persisting the edit failed; prior minutes retained"
        },
    },
)
def update_meeting_minutes(
    meeting_id: str,
    payload: MinutesEdit,
    user: CurrentUser,
    db: Annotated[OrmSession, Depends(get_session)],
) -> Any:
    """Atomically replace a meeting's minutes (task 11.3, Req 9.2 / 9.6).

    - **404** when the meeting is unknown or not owned (Req 8.5).
    - **409** when the meeting exists but has no persisted minutes to edit (see
      the module section comment on the 409-vs-404 choice).
    - Otherwise replace ``summary``/``topics``/``decisions`` and the action-item
      set in a single transaction (:func:`_persist_minutes_edit`); each owner is
      normalized so it is never blank (Req 6.3 via :func:`_normalize_owner`). On
      success respond **200** confirming the save (Req 9.2). If persistence
      fails, roll back so the previously stored minutes are unchanged and respond
      **500** (Req 9.6).
    """
    record = _load_owned_meeting(db, owner_id=user.id, meeting_id=meeting_id)
    if record is None:
        return _not_found_response()
    minutes = record.minutes
    if minutes is None:
        return _no_minutes_response("edit")

    normalized_items = [
        (item.description, _normalize_owner(item.owner))
        for item in payload.action_items
    ]
    try:
        _persist_minutes_edit(
            db,
            minutes,
            summary=payload.summary,
            topics=payload.topics,
            decisions=payload.decisions,
            action_items=normalized_items,
        )
    except Exception:
        logger.exception("failed to persist edited minutes; rolling back")
        try:
            db.rollback()
        except Exception:  # pragma: no cover - rollback itself failing
            logger.exception("rollback failed during minutes-edit cleanup")
        return _internal_error_response("failed to save the edited minutes")

    return JSONResponse(
        status_code=status.HTTP_200_OK,
        content={"id": meeting_id, "saved": True},
    )


@router.get(
    "/{meeting_id}/export",
    responses={
        status.HTTP_200_OK: {"description": "Minutes document"},
        status.HTTP_400_BAD_REQUEST: {"description": "Unknown format"},
        status.HTTP_404_NOT_FOUND: {"description": "Unknown or not-owned meeting"},
        status.HTTP_409_CONFLICT: {"description": "Meeting has no minutes to export"},
    },
)
def export_meeting_minutes(
    meeting_id: str,
    user: CurrentUser,
    db: Annotated[OrmSession, Depends(get_session)],
    format: str = "md",
) -> Any:
    """Export a meeting's minutes as a downloadable file.

    Supports two formats via the ``format`` query parameter:

    * ``md`` (default) — Markdown (``text/markdown``, ``.md``).
    * ``docx`` — Microsoft Word (``application/vnd.openxmlformats-officedocument.wordprocessingml.document``, ``.docx``).

    **404** for an unknown/foreign meeting (Req 8.5); **409** when the meeting
    exists but has no minutes yet; **400** when ``format`` is unrecognised.
    """
    if format not in ("md", "docx"):
        return JSONResponse(
            status_code=status.HTTP_400_BAD_REQUEST,
            content={"detail": f"Unknown export format: {format!r}"},
        )

    record = _load_owned_meeting(db, owner_id=user.id, meeting_id=meeting_id)
    if record is None:
        return _not_found_response()
    minutes = record.minutes
    if minutes is None:
        return _no_minutes_response("export")

    if format == "docx":
        document = _render_minutes_docx(record, minutes)
        filename = f"minutes-{record.id}.docx"
        return Response(
            content=document,
            media_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            headers={"Content-Disposition": f'attachment; filename="{filename}"'},
        )

    # default: Markdown
    document = _render_minutes_markdown(record, minutes)
    filename = f"minutes-{record.id}.md"
    return Response(
        content=document,
        media_type="text/markdown; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@router.post(
    "/{meeting_id}/correct-selection",
    responses={
        status.HTTP_200_OK: {"description": "Corrected text"},
        status.HTTP_400_BAD_REQUEST: {"description": "Invalid request body"},
        status.HTTP_404_NOT_FOUND: {"description": "Unknown or not-owned meeting"},
        status.HTTP_502_BAD_GATEWAY: {"description": "LLM correction failed"},
    },
)
def correct_selection(
    meeting_id: str,
    body: CorrectionRequest,
    user: CurrentUser,
    db: Annotated[OrmSession, Depends(get_session)],
) -> Any:
    """Post-ASR phonetic correction for a user-selected text span.

    Accepts a text selection from the meeting transcript and returns a
    corrected version after running it through the configured LLM with a
    dedicated ASR-error-correction prompt. The original transcript is
    **not** modified — the caller (frontend) decides whether to apply
    the returned correction.

    - **400** when ``text`` is empty or exceeds 2000 characters.
    - **404** when the meeting is unknown or not owned by the current user
      (Req 8.5).
    - **502** on any LLM transport/timeout failure.
    """
    # Ownership gate: reject unknown / foreign meetings.
    meeting = db.execute(
        select(MeetingRecord).where(
            MeetingRecord.id == meeting_id,
            MeetingRecord.owner_id == user.id,
        )
    ).scalar_one_or_none()
    if meeting is None:
        return _not_found_response()

    try:
        llm = make_llm_client(get_settings())
        corrected = llm.correct_text(body.text, context=body.context)
    except LlmError as exc:
        return JSONResponse(
            status_code=status.HTTP_502_BAD_GATEWAY,
            content={"detail": str(exc) or "LLM correction failed"},
        )

    return JSONResponse(
        status_code=status.HTTP_200_OK,
        content={"corrected": corrected},
    )


@router.post(
    "/{meeting_id}/transcript/correct-batch",
    responses={
        status.HTTP_200_OK: {"description": "Batch-corrected segments"},
        status.HTTP_400_BAD_REQUEST: {"description": "Invalid request body"},
        status.HTTP_404_NOT_FOUND: {"description": "Unknown or not-owned meeting"},
        status.HTTP_502_BAD_GATEWAY: {"description": "LLM correction failed"},
    },
)
def correct_batch(
    meeting_id: str,
    body: CorrectBatchRequest,
    user: CurrentUser,
    db: Annotated[OrmSession, Depends(get_session)],
) -> Any:
    """Batch post-ASR correction for an ordered list of transcript segments.

    Accepts an ordered list of ``{index, text, speaker?}`` segments and
    returns ``{index, text}`` results aligned by the original indices.
    Segments are corrected in windowed prompts (group ~25 segments per
    LLM call with context overlap) so the model sees cross-segment
    context for consistent terminology correction.

    - **400** when segments list is empty or exceeds 500 entries.
    - **404** when the meeting is unknown or not owned by the current user.
    - **502** on any LLM transport/timeout failure.
    """
    meeting = db.execute(
        select(MeetingRecord).where(
            MeetingRecord.id == meeting_id,
            MeetingRecord.owner_id == user.id,
        )
    ).scalar_one_or_none()
    if meeting is None:
        return _not_found_response()

    segments: list[dict[str, object]] = [
        {
            "index": seg.index,
            "text": seg.text,
            "speaker": seg.speaker,
        }
        for seg in body.segments
    ]

    try:
        llm = make_llm_client(get_settings())
        corrected = llm.correct_batch(segments)
    except LlmError as exc:
        return JSONResponse(
            status_code=status.HTTP_502_BAD_GATEWAY,
            content={"detail": str(exc) or "LLM batch correction failed"},
        )

    return JSONResponse(
        status_code=status.HTTP_200_OK,
        content={"segments": corrected},
    )


@router.put(
    "/{meeting_id}/transcript",
    responses={
        status.HTTP_200_OK: {"description": "Transcript updated"},
        status.HTTP_404_NOT_FOUND: {"description": "Unknown or not-owned meeting"},
        status.HTTP_409_CONFLICT: {"description": "Meeting has no transcript"},
    },
)
def update_transcript(
    meeting_id: str,
    body: TranscriptUpdateRequest,
    user: CurrentUser,
    db: Annotated[OrmSession, Depends(get_session)],
) -> Any:
    """Replace the persisted transcript segments for a completed meeting.

    The existing Transcript and all its segments are deleted and replaced
    with the provided segment list. Timestamps and speaker labels are
    preserved as-provided.
    """
    meeting = db.execute(
        select(MeetingRecord).where(
            MeetingRecord.id == meeting_id,
            MeetingRecord.owner_id == user.id,
        )
    ).scalar_one_or_none()
    if meeting is None:
        return _not_found_response()

    transcript = meeting.transcript
    if transcript is None:
        return JSONResponse(
            status_code=status.HTTP_409_CONFLICT,
            content={"detail": "该会议没有转写文本可更新"},
        )

    # Delete existing segments, then insert the updated ones.
    for seg in transcript.segments:
        db.delete(seg)
    db.flush()

    for ordinal, seg in enumerate(body.segments):
        db.add(
            TranscriptSegment(
                transcript_id=transcript.id,
                start_seconds=seg.start,
                end_seconds=seg.end,
                text=seg.text,
                speaker=seg.speaker,
                ordinal=ordinal,
            )
        )
    db.commit()

    return JSONResponse(
        status_code=status.HTTP_200_OK,
        content={"id": meeting_id, "updated": True, "count": len(body.segments)},
    )


@router.delete(
    "/{meeting_id}",
    responses={
        status.HTTP_200_OK: {"description": "Meeting and its data deleted"},
        status.HTTP_404_NOT_FOUND: {"description": "Unknown or not-owned meeting"},
    },
)
def delete_meeting(
    meeting_id: str,
    user: CurrentUser,
    db: Annotated[OrmSession, Depends(get_session)],
) -> Any:
    """Permanently delete an owned meeting and all its data (task 11.5, Req 9.4).

    - **404** when the meeting is unknown or not owned (Req 8.5).
    - Otherwise delete the ``Meeting_Record``; the ORM/DB cascade removes its
      ``Processing_Job``, ``Transcript`` (+ segments), and ``Meeting_Minutes``
      (+ action items). After the delete commits, the source and standardized
      audio files are removed best-effort via :func:`_remove_quietly` (Req 9.4).
      Respond **200** confirming the deletion.

    The file removal runs *after* the commit (and only for paths that are set),
    so a meeting is never left with its row deleted but referenced by a missing
    audio file mid-transaction; a residual file (should unlink fail) is harmless
    and logged.
    """
    record = _load_owned_meeting(db, owner_id=user.id, meeting_id=meeting_id)
    if record is None:
        return _not_found_response()

    # If the meeting is a Live_Meeting_Session in progress, signal the active WS
    # handler to stop and release its append handle BEFORE removing the row +
    # audio, so the delete never races a still-writing handler (Req 9.4
    # extension for realtime-meeting-transcription). Lazy import avoids a
    # module import cycle (live imports meetings helpers).
    if record.job is not None and record.job.status == JobStatus.LIVE.value:
        from app.api.live import abort_live_session

        abort_live_session(meeting_id)

    # Capture the on-disk audio paths before the row (and its columns) are gone.
    audio_paths = [
        path
        for path in (record.source_audio_path, record.standardized_audio_path)
        if path
    ]

    db.delete(record)
    db.commit()

    # Best-effort filesystem cleanup after the DB delete is durable (Req 9.4).
    for path in audio_paths:
        _remove_quietly(path)

    return JSONResponse(
        status_code=status.HTTP_200_OK,
        content={"id": meeting_id, "deleted": True},
    )
