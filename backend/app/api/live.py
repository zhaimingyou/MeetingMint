"""Live meeting API + coordination (realtime-meeting-transcription).

This module owns the live-streaming path that layers on top of the existing
batch meeting pipeline. It is built across several tasks; the pieces here are:

* **Per-meeting coordination registry** (task 6.1) -- a process-local
  ``asyncio.Lock`` per ``meeting_id`` (serializing the append fast path and
  finalization), a "finalizing" flag (so the append path refuses once
  finalization has begun), and a connection generation counter (so a new
  Streaming_Connection supersedes the old one). These are **process-local**, so
  the live path requires the Backend_API to run as a single process / single
  replica (design FIX C); a multi-worker deployment would need a cross-process
  lock + shared supersede signal.
* **``finalize_live_meeting``** (task 6.2) -- the single, exactly-once finalizer
  shared by every trigger (WS ``end`` frame, in-connection idle timeout, REST
  ``/end`` fallback, inactivity sweep). It runs under the per-meeting append
  lock (mutually exclusive with appends, FIX A), truncates the recording to the
  durable ``recording_bytes`` and fsyncs it, then performs the ``live ->
  queued`` / ``live -> failed`` transition as a single atomic compare-and-set
  (``UPDATE ... WHERE status='live'``); only the trigger whose UPDATE affects one
  row enqueues the existing ``process_meeting`` pipeline (exactly once). This is
  essential because ``process_meeting`` is not idempotent (a double-enqueue would
  write duplicate Transcript/Meeting_Minutes rows).
* **``POST /api/meetings/live``** (task 5) -- start a Live_Meeting_Session,
  creating the ``Meeting_Record`` + ``Processing_Job(status=live)`` with the
  one-live-per-user guarantee enforced at the database (the partial unique index
  ``UNIQUE(owner_id) WHERE status='live'`` -> 409 on a concurrent second start).

Seams (testability): :func:`_get_session_factory` returns the process-wide
session factory by default and is monkeypatched in tests to the in-memory engine;
the Celery enqueue is reached via ``app.api.meetings._enqueue_processing_job``
(called as an attribute so tests' monkeypatch is honored).
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import time
from datetime import timedelta
from typing import Annotated, Any
from uuid import uuid4

from fastapi import (
    APIRouter,
    Depends,
    WebSocket,
    WebSocketDisconnect,
    status,
)
from fastapi.responses import JSONResponse
from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session as OrmSession
from sqlalchemy.orm import sessionmaker

import app.api.meetings as meetings_mod
from app.api.deps import CurrentUser
from app.clients.streaming.base import StreamingAsrError
from app.clients.streaming.factory import make_streaming_asr_client
from app.config import Settings, get_settings
from app.config_effective import effective_settings, effective_settings_for
from app.db import get_session, get_sessionmaker
from app.enums import JobStatus
from app.models import MeetingRecord, ProcessingJob, utcnow
from app.security.sessions import renew_session, validate_session

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/meetings", tags=["live"])

# No-audio error description recorded on a 0-byte live finalization (Req 6.5).
NO_AUDIO_ERROR = "未捕获到音频"

# WebSocket subprotocol carrying the live message version (the auth token is the
# second offered subprotocol; see the WS handler, task 7).
LIVE_SUBPROTOCOL = "mm.live.v1"


# --------------------------------------------------------------------------- #
# Session-factory seam (task 6 / 7 / 8)
# --------------------------------------------------------------------------- #
def _get_session_factory() -> sessionmaker[OrmSession]:
    """Return the session factory for live background DB work.

    Defaults to the process-wide :func:`app.db.get_sessionmaker`. Tests
    monkeypatch ``app.api.live._get_session_factory`` to bind the WS handler,
    finalizer, and inactivity sweep to the in-memory test engine (the WS route
    and background tasks do not flow through the ``get_session`` dependency
    override).
    """
    return get_sessionmaker()


# --------------------------------------------------------------------------- #
# Per-meeting coordination registry (task 6.1)
# --------------------------------------------------------------------------- #
class _LiveCoordination:
    """Process-local per-``meeting_id`` coordination primitives.

    * :meth:`append_lock` -- serializes the append fast path *and* finalization
      so at most one of {append a new increment, finalize} touches a meeting's
      recording at any instant (single-writer, FIX A/FIX 3).
    * :meth:`mark_finalizing` / :meth:`is_finalizing` -- once set, the append
      fast path refuses further appends for that meeting.
    * :meth:`claim_connection` / :meth:`is_current_connection` -- a monotonic
      generation per meeting; a newly accepted Streaming_Connection claims the
      latest generation and any older handler detects it has been superseded.
    """

    def __init__(self) -> None:
        self._locks: dict[str, asyncio.Lock] = {}
        self._finalizing: set[str] = set()
        self._generation: dict[str, int] = {}
        self._headers: dict[str, bytes] = {}

    def append_lock(self, meeting_id: str) -> asyncio.Lock:
        lock = self._locks.get(meeting_id)
        if lock is None:
            lock = asyncio.Lock()
            self._locks[meeting_id] = lock
        return lock

    def mark_finalizing(self, meeting_id: str) -> None:
        self._finalizing.add(meeting_id)

    def is_finalizing(self, meeting_id: str) -> bool:
        return meeting_id in self._finalizing

    def claim_connection(self, meeting_id: str) -> int:
        """Register a new connection for ``meeting_id``; return its generation."""
        gen = self._generation.get(meeting_id, 0) + 1
        self._generation[meeting_id] = gen
        return gen

    def is_current_connection(self, meeting_id: str, generation: int) -> bool:
        """True iff ``generation`` is still the latest connection for the meeting."""
        return self._generation.get(meeting_id) == generation

    def set_header(self, meeting_id: str, header_bytes: bytes) -> None:
        """Cache the sequence-0 container header (first writer wins).

        Used to re-prime the HTTP streaming decoder after a reconnect (FIX 5);
        ignored by the mock. Only the first (sequence 0) increment sets it.
        """
        self._headers.setdefault(meeting_id, header_bytes)

    def get_header(self, meeting_id: str) -> bytes | None:
        """Return the cached sequence-0 header for the meeting, if any."""
        return self._headers.get(meeting_id)

    def cleanup(self, meeting_id: str) -> None:
        """Drop all state for a meeting once it is no longer live (best-effort)."""
        self._locks.pop(meeting_id, None)
        self._finalizing.discard(meeting_id)
        self._generation.pop(meeting_id, None)
        self._headers.pop(meeting_id, None)


# Module-level singleton. Keyed by (globally unique) meeting id, so distinct
# meetings never share state; tests can call ``reset_coordination()``.
_coordination = _LiveCoordination()


def get_coordination() -> _LiveCoordination:
    """Return the process-wide live-coordination registry."""
    return _coordination


def reset_coordination() -> None:
    """Reset the coordination registry (test isolation)."""
    global _coordination
    _coordination = _LiveCoordination()


# --------------------------------------------------------------------------- #
# Recording finalization helpers
# --------------------------------------------------------------------------- #
def _finalize_recording_size(source_path: str, recording_bytes: int | None) -> int:
    """Truncate the recording to the durable cursor, fsync, and return its size.

    Reconciles a torn tail (FIX A/B): if the on-disk file is longer than the
    durable ``recording_bytes`` (bytes written past a never-committed cursor
    advance), it is truncated back down; the file is never extended. Returns the
    final on-disk byte count (0 when no recording exists -- the 0-byte case,
    Req 6.5). Best-effort fsync of the file and its directory so the size is
    durable before the atomic CAS.
    """
    if not os.path.isfile(source_path):
        return 0
    actual = os.path.getsize(source_path)
    target = actual if recording_bytes is None else min(int(recording_bytes), actual)
    try:
        fd = os.open(source_path, os.O_RDWR)
        try:
            if target < actual:
                os.ftruncate(fd, target)
            os.fsync(fd)
        finally:
            os.close(fd)
        dir_path = os.path.dirname(source_path) or "."
        try:
            dir_fd = os.open(dir_path, os.O_RDONLY)
            try:
                os.fsync(dir_fd)
            finally:
                os.close(dir_fd)
        except OSError:  # pragma: no cover - platform without dir fsync
            pass
    except OSError:  # pragma: no cover - defensive; fall back to on-disk size
        logger.warning("failed to truncate/fsync live recording %s", source_path)
        return min(target, os.path.getsize(source_path))
    return target


async def finalize_live_meeting(
    meeting_id: str,
    *,
    session_factory: sessionmaker[OrmSession] | None = None,
) -> JobStatus | None:
    """Exactly-once finalizer for a Live_Meeting_Session (Req 6.1, 6.2, 6.5, 6.8).

    Runs under the per-meeting append lock (so no append interleaves with or
    follows finalization, FIX A), truncates the recording to the durable
    ``recording_bytes`` and fsyncs it, then performs the ``live ->`` transition
    as a single atomic compare-and-set. Only the trigger whose UPDATE affects one
    row (``rowcount == 1``) acts: it enqueues ``process_meeting`` exactly once
    when the recording is > 0 bytes, or records the no-audio failure when 0 bytes.
    Concurrent triggers that lose the race are no-ops (return the already-final
    status). Idempotent and safe to call from any trigger.

    :returns: the resulting :class:`JobStatus` (the new terminal status when this
        call won the race, or the current status when another trigger already
        finalized); ``None`` if the meeting/job no longer exists.
    """
    factory = session_factory if session_factory is not None else _get_session_factory()
    coord = get_coordination()
    lock = coord.append_lock(meeting_id)
    async with lock:
        coord.mark_finalizing(meeting_id)
        session = factory()
        try:
            job = session.execute(
                select(ProcessingJob).where(ProcessingJob.meeting_id == meeting_id)
            ).scalar_one_or_none()
            if job is None:
                return None
            if job.status != JobStatus.LIVE.value:
                # Already finalized by another trigger; no-op.
                return JobStatus(job.status)
            meeting = session.get(MeetingRecord, meeting_id)
            source_path = meeting.source_audio_path if meeting is not None else ""
            recording_bytes = job.recording_bytes

            # Truncate-to-cursor + fsync BEFORE the CAS so the size is final.
            size_bytes = _finalize_recording_size(source_path, recording_bytes)

            target = JobStatus.QUEUED if size_bytes > 0 else JobStatus.FAILED
            error_description = None if size_bytes > 0 else NO_AUDIO_ERROR

            result = session.execute(
                update(ProcessingJob)
                .where(
                    ProcessingJob.meeting_id == meeting_id,
                    ProcessingJob.status == JobStatus.LIVE.value,
                )
                .values(status=target.value, error_description=error_description)
            )
            session.commit()
            won = (result.rowcount or 0) == 1
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

    if won and target is JobStatus.QUEUED:
        # Post-commit enqueue via the shared seam (honors test monkeypatch).
        meetings_mod._enqueue_processing_job(meeting_id)
    coord.cleanup(meeting_id)
    return target


# --------------------------------------------------------------------------- #
# Task 5: POST /api/meetings/live -- start a Live_Meeting_Session
# --------------------------------------------------------------------------- #
@router.post(
    "/live",
    responses={
        status.HTTP_201_CREATED: {"description": "Live meeting started"},
        status.HTTP_409_CONFLICT: {
            "description": "A live meeting is already in progress for this user"
        },
        status.HTTP_500_INTERNAL_SERVER_ERROR: {
            "description": "Could not create the live meeting"
        },
    },
)
def start_live_meeting(
    user: CurrentUser,
    db: Annotated[OrmSession, Depends(get_session)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> Any:
    """Start a Live_Meeting_Session (task 5; Req 2.2, 2.3, 2.8, 7.2).

    Creates exactly one ``Meeting_Record`` and its one-to-one
    ``Processing_Job(status=live, owner_id=...)`` in a single transaction. The
    audio file is created lazily on the first Audio_Increment (so an immediately
    ended session is the 0-byte case). The **one-live-per-user** rule is enforced
    at the database by the partial unique index ``UNIQUE(owner_id) WHERE
    status='live'``: a concurrent second start raises ``IntegrityError`` which is
    mapped to **409** with the existing session preserved (Req 7.2). Any other
    persistence failure rolls back leaving no partial row and responds **500**
    (Req 2.8). On success responds **201** ``{id, status, title, send_interval,
    reconnect_interval}`` (the intervals are server-configured, Req 3.1 / 5.5).
    """
    meeting_id = str(uuid4())
    created_at = utcnow()
    # Overlay any persisted runtime overrides onto the injected base settings so
    # the echoed intervals / live timing reflect the current Effective_Configuration
    # without a restart (admin-console Req 5.1).
    settings = effective_settings_for(settings, db)
    title = meetings_mod._default_title(None, created_at)
    source_path = meetings_mod._final_audio_path(settings, meeting_id, "webm")

    record = MeetingRecord(
        id=meeting_id,
        owner_id=user.id,
        title=title,
        source_audio_path=source_path,
        source_format="webm",
        created_at=created_at,
    )
    job = ProcessingJob(
        meeting_id=meeting_id,
        status=JobStatus.LIVE.value,
        owner_id=user.id,
        last_seq=None,
        recording_bytes=0,
        last_activity_at=created_at,
    )
    db.add(record)
    db.add(job)
    try:
        db.commit()
    except IntegrityError:
        # Partial unique index violation: this user already has a live session.
        db.rollback()
        return JSONResponse(
            status_code=status.HTTP_409_CONFLICT,
            content={
                "detail": "a live meeting is already in progress",
                "status": JobStatus.LIVE.value,
            },
        )
    except Exception:
        logger.exception("failed to create live meeting; rolling back")
        try:
            db.rollback()
        except Exception:  # pragma: no cover - rollback itself failing
            logger.exception("rollback failed creating live meeting")
        return meetings_mod._internal_error_response(
            "failed to start the live meeting"
        )

    return JSONResponse(
        status_code=status.HTTP_201_CREATED,
        content={
            "id": meeting_id,
            "status": JobStatus.LIVE.value,
            "title": title,
            "send_interval": settings.streaming_send_interval_seconds,
            "reconnect_interval": settings.streaming_reconnect_interval_seconds,
        },
    )


# --------------------------------------------------------------------------- #
# Task 9: end fallback + delete-of-live abort
# --------------------------------------------------------------------------- #
def abort_live_session(meeting_id: str) -> None:
    """Signal an active live handler to stop writing (delete-of-live, task 9.2).

    Marks the session finalizing and supersedes the current connection so the WS
    handler's append fast path refuses further writes and the handler exits. This
    is a synchronous, best-effort signal (no async lock) usable from the sync
    ``DELETE`` endpoint before it removes the row + audio, so the delete does not
    race a still-writing handler.
    """
    coord = get_coordination()
    coord.mark_finalizing(meeting_id)
    coord.claim_connection(meeting_id)


@router.post(
    "/{meeting_id}/end",
    responses={
        status.HTTP_200_OK: {"description": "Live meeting finalized"},
        status.HTTP_404_NOT_FOUND: {"description": "Unknown or not-owned meeting"},
    },
)
async def end_live_meeting(
    meeting_id: str,
    user: CurrentUser,
    db: Annotated[OrmSession, Depends(get_session)],
) -> Any:
    """End a Live_Meeting_Session (REST fallback for the WS ``end`` frame; task 9.2).

    Owner-scoped (Req 8.5 owner-scoping via the shared meetings helper): an
    unknown or foreign meeting is **404**. Otherwise it invokes the shared,
    exactly-once :func:`finalize_live_meeting` (atomic CAS) and responds **200**
    with the resulting status. Calling ``/end`` on an already-ended meeting is a
    harmless no-op that returns the current status.
    """
    job = meetings_mod._load_owned_job(db, owner_id=user.id, meeting_id=meeting_id)
    if job is None:
        return meetings_mod._not_found_response()
    result = await finalize_live_meeting(meeting_id)
    return JSONResponse(
        status_code=status.HTTP_200_OK,
        content={
            "id": meeting_id,
            "status": result.value if result is not None else job.status,
        },
    )


# --------------------------------------------------------------------------- #
# Task 7: WS /api/meetings/{id}/live -- the Streaming_Connection handler
# --------------------------------------------------------------------------- #
# Upstream Audio_Increment binary frame = a 4-byte little-endian uint32 sequence
# prefix followed by the WebM/Opus fragment bytes.
SEQ_PREFIX_BYTES = 4


def _read_subprotocol_token(websocket: WebSocket) -> str | None:
    """Extract the session token from the offered WebSocket subprotocols.

    The client connects with subprotocols ``[LIVE_SUBPROTOCOL, <token>]`` so the
    opaque token never appears in the URL or access logs (design WS-auth). Returns
    the token, or ``None`` when the protocol/token is absent or malformed.
    """
    offered = list(websocket.scope.get("subprotocols") or [])
    if len(offered) >= 2 and offered[0] == LIVE_SUBPROTOCOL:
        return offered[1] or None
    return None


def _authorize_ws(
    token: str | None, meeting_id: str, factory: sessionmaker[OrmSession]
) -> bool:
    """Return True iff ``token`` is valid and owns ``meeting_id`` in ``live`` state.

    Enforces Req 2.6: absent/invalid/expired/invalidated token, a foreign
    meeting, or a non-live meeting all deny the connection (the caller closes the
    handshake before ``accept`` -- no stream is established).
    """
    if not token:
        return False
    session = factory()
    try:
        user = validate_session(session, token)
        if user is None:
            return False
        meeting = session.get(MeetingRecord, meeting_id)
        if meeting is None or meeting.owner_id != user.id:
            return False
        job = session.execute(
            select(ProcessingJob).where(ProcessingJob.meeting_id == meeting_id)
        ).scalar_one_or_none()
        if job is None or job.status != JobStatus.LIVE.value:
            return False
        return True
    finally:
        session.close()


def _reconcile_cursor(factory: sessionmaker[OrmSession], meeting_id: str) -> int:
    """Truncate the recording to the durable cursor and return the next sequence.

    On (re)connect the recording is truncated to ``min(recording_bytes, actual
    file size)`` -- reconciling a torn tail in either direction (FIX B) -- and the
    next expected sequence is ``last_seq + 1`` (or 0 when nothing is persisted).
    """
    session = factory()
    try:
        job = session.execute(
            select(ProcessingJob).where(ProcessingJob.meeting_id == meeting_id)
        ).scalar_one_or_none()
        if job is None:
            return 0
        meeting = session.get(MeetingRecord, meeting_id)
        if meeting is not None and os.path.isfile(meeting.source_audio_path):
            actual = os.path.getsize(meeting.source_audio_path)
            rb = job.recording_bytes
            target = actual if rb is None else min(int(rb), actual)
            if target < actual:
                with contextlib.suppress(OSError):
                    fd = os.open(meeting.source_audio_path, os.O_RDWR)
                    try:
                        os.ftruncate(fd, target)
                        os.fsync(fd)
                    finally:
                        os.close(fd)
        return 0 if job.last_seq is None else int(job.last_seq) + 1
    finally:
        session.close()


def _append_increment(
    factory: sessionmaker[OrmSession],
    meeting_id: str,
    seq: int,
    payload: bytes,
    token: str | None,
    settings: Settings,
) -> tuple[str, int]:
    """Idempotently append one sequence-numbered increment (fast path; FIX A/B).

    Returns ``(outcome, next_expected_seq)`` where outcome is one of:
      * ``"stop"`` -- the job is gone or no longer live;
      * ``"ignored"`` -- ``seq`` is already persisted (``seq < expected``);
      * ``"gap"`` -- ``seq > expected`` (caller re-requests resume);
      * ``"appended"`` -- the bytes were appended.

    On append it writes the bytes, **fsyncs the recording**, and only **then**
    commits the durable cursor advance (``last_seq`` / ``recording_bytes`` /
    ``last_activity_at``) plus a sliding session renewal -- data-before-metadata
    (FIX B), so the durable cursor is never ahead of the durable bytes.
    """
    session = factory()
    try:
        job = session.execute(
            select(ProcessingJob).where(ProcessingJob.meeting_id == meeting_id)
        ).scalar_one_or_none()
        if job is None or job.status != JobStatus.LIVE.value:
            return ("stop", 0)
        expected = 0 if job.last_seq is None else int(job.last_seq) + 1
        if seq < expected:
            return ("ignored", expected)
        if seq > expected:
            return ("gap", expected)

        meeting = session.get(MeetingRecord, meeting_id)
        if meeting is None:
            return ("stop", 0)
        path = meeting.source_audio_path
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        with open(path, "ab") as fh:
            fh.write(payload)
            fh.flush()
            os.fsync(fh.fileno())  # bytes durable BEFORE the cursor commit

        job.last_seq = seq
        job.recording_bytes = int(job.recording_bytes or 0) + len(payload)
        job.last_activity_at = utcnow()
        if token:
            renew_session(
                session,
                token,
                ttl_minutes=settings.session_ttl_minutes,
                max_lifetime_seconds=settings.live_session_max_lifetime_seconds,
            )
        session.commit()
        return ("appended", seq + 1)
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def _touch_activity(
    factory: sessionmaker[OrmSession],
    meeting_id: str,
    token: str | None,
    settings: Settings,
) -> None:
    """Record a keep-alive: bump ``last_activity_at`` and slide the session."""
    session = factory()
    try:
        job = session.execute(
            select(ProcessingJob).where(ProcessingJob.meeting_id == meeting_id)
        ).scalar_one_or_none()
        if job is None or job.status != JobStatus.LIVE.value:
            return
        job.last_activity_at = utcnow()
        if token:
            renew_session(
                session,
                token,
                ttl_minutes=settings.session_ttl_minutes,
                max_lifetime_seconds=settings.live_session_max_lifetime_seconds,
            )
        session.commit()
    finally:
        session.close()


def _revalidate(
    factory: sessionmaker[OrmSession], token: str | None, settings: Settings
) -> bool:
    """Re-validate the session mid-connection (Req 8.4/8.5); True iff still valid."""
    if not token:
        return False
    session = factory()
    try:
        user = validate_session(session, token)
        if user is None:
            return False
        renew_session(
            session,
            token,
            ttl_minutes=settings.session_ttl_minutes,
            max_lifetime_seconds=settings.live_session_max_lifetime_seconds,
        )
        session.commit()
        return True
    finally:
        session.close()


async def _safe_ws_send_json(websocket: WebSocket, payload: dict[str, Any]) -> bool:
    """Send a JSON control/transcript frame; never raise if the socket is closed.

    Ending a live meeting races the browser close with late ASR results. A hard
    ``RuntimeError: Cannot call "send" once a close message has been sent`` used
    to crash the ASGI task (seen on 192.0.2.10). Treat a closed peer as a
    soft stop so finalization and teardown still run cleanly.
    """
    try:
        await websocket.send_json(payload)
        return True
    except (RuntimeError, WebSocketDisconnect) as exc:
        logger.debug("live ws send skipped (socket closed): %s", exc)
        return False
    except Exception:
        logger.exception("live ws send failed")
        return False


@router.websocket("/{meeting_id}/live")
async def live_ws(
    websocket: WebSocket,
    meeting_id: str,
    settings: Annotated[Settings, Depends(get_settings)],
) -> None:
    """The Streaming_Connection handler for a Live_Meeting_Session (task 7).

    Authenticates the handshake via the subprotocol token (Req 2.6, rejecting
    before ``accept``), supersedes any prior connection, reconciles + announces
    the resume cursor, then runs the receive loop: sequence-numbered, idempotent,
    fsync-before-commit audio append on the fast path (FIX A/B); best-effort
    streaming ASR relay with graceful degradation (Req 5.2/5.3); keep-alive +
    sliding renewal; ``end`` / inactivity-timeout finalization via the shared
    exactly-once finalizer; and periodic session re-validation (Req 8.4/8.5).
    """
    factory = _get_session_factory()
    token = _read_subprotocol_token(websocket)
    if not _authorize_ws(token, meeting_id, factory):
        # Reject BEFORE accept -> no stream established, nothing created (Req 2.6).
        await websocket.close(code=status.WS_1008_POLICY_VIOLATION)
        return

    await websocket.accept(subprotocol=LIVE_SUBPROTOCOL)
    coord = get_coordination()
    generation = coord.claim_connection(meeting_id)

    # Overlay persisted runtime overrides onto the injected base settings so the
    # streaming client selection and live timing reflect the current
    # Effective_Configuration for this connection (admin-console Req 5.1). Read
    # once at connect; a change mid-connection applies on the next connection.
    _eff_session = factory()
    try:
        settings = effective_settings_for(settings, _eff_session)
    finally:
        _eff_session.close()

    asr_client = make_streaming_asr_client(settings)
    asr_session = asr_client.open_session(
        session_id=meeting_id,
        input_encoding="webm/opus",
        header_bytes=coord.get_header(meeting_id),
    )

    next_seq = _reconcile_cursor(factory, meeting_id)
    if not await _safe_ws_send_json(
        websocket, {"type": "resume", "next_seq": next_seq}
    ):
        with contextlib.suppress(Exception):
            await asr_session.aclose()
        return

    last_reval = time.monotonic()
    asr_unavailable = False
    try:
        while True:
            # Superseded by a newer connection, or finalization started -> stop.
            if not coord.is_current_connection(meeting_id, generation):
                break
            if coord.is_finalizing(meeting_id):
                break

            if (
                time.monotonic() - last_reval
                >= settings.live_session_revalidation_interval_seconds
            ):
                if not _revalidate(factory, token, settings):
                    with contextlib.suppress(Exception):
                        await websocket.close(code=status.WS_1008_POLICY_VIOLATION)
                    break
                last_reval = time.monotonic()

            try:
                event = await asyncio.wait_for(
                    websocket.receive(),
                    timeout=float(settings.live_inactivity_timeout_seconds),
                )
            except (asyncio.TimeoutError, TimeoutError):
                # Inactivity: finalize as if ended (Req 7.3).
                await finalize_live_meeting(meeting_id, session_factory=factory)
                with contextlib.suppress(Exception):
                    await websocket.close()
                break

            if event.get("type") == "websocket.disconnect":
                # Transport drop is NOT an end: the client reconnects, or the
                # inactivity sweep finalizes later (Req 5.5).
                break

            text = event.get("text")
            if text is not None:
                try:
                    msg = json.loads(text)
                except (ValueError, TypeError):
                    continue
                mtype = msg.get("type") if isinstance(msg, dict) else None
                if mtype == "keepalive":
                    _touch_activity(factory, meeting_id, token, settings)
                    continue
                if mtype == "end":
                    await finalize_live_meeting(meeting_id, session_factory=factory)
                    with contextlib.suppress(Exception):
                        await websocket.close()
                    break
                continue

            data = event.get("bytes")
            if not data or len(data) < SEQ_PREFIX_BYTES:
                continue
            seq = int.from_bytes(data[:SEQ_PREFIX_BYTES], "little")
            payload = data[SEQ_PREFIX_BYTES:]
            if seq == 0 and payload:
                coord.set_header(meeting_id, payload)
                # Keep the ASR session's rebuild header in sync (ffmpeg
                # recovery after a mid-meeting decoder crash).
                setter = getattr(asr_session, "set_header_bytes", None)
                if callable(setter):
                    setter(payload)

            lock = coord.append_lock(meeting_id)
            async with lock:
                if coord.is_finalizing(meeting_id):
                    break
                outcome, expected = _append_increment(
                    factory, meeting_id, seq, payload, token, settings
                )

            if outcome == "stop":
                break
            if outcome == "gap":
                if not await _safe_ws_send_json(
                    websocket, {"type": "resume", "next_seq": expected}
                ):
                    break
                continue
            if outcome == "ignored":
                continue

            # outcome == "appended": persistence is durable; ASR is best-effort.
            try:
                results = await asr_session.push(payload)
                if asr_unavailable:
                    asr_unavailable = False
                    if not await _safe_ws_send_json(
                        websocket, {"type": "asr_available"}
                    ):
                        break
                for result in results:
                    if not await _safe_ws_send_json(
                        websocket, {"type": "transcript", "result": result}
                    ):
                        # Peer gone; stop the loop (recording already durable).
                        break
                else:
                    continue
                break
            except StreamingAsrError as exc:
                logger.warning(
                    "streaming ASR unavailable for meeting %s: %s",
                    meeting_id,
                    exc,
                )
                if not asr_unavailable:
                    asr_unavailable = True
                    if not await _safe_ws_send_json(
                        websocket, {"type": "asr_unavailable"}
                    ):
                        break
    except WebSocketDisconnect:
        pass
    finally:
        with contextlib.suppress(Exception):
            await asr_session.aclose()


# --------------------------------------------------------------------------- #
# Task 8: inactivity sweep (the in-connection idle timeout lives in the WS loop)
# --------------------------------------------------------------------------- #
async def sweep_inactive_live_sessions(
    *,
    session_factory: sessionmaker[OrmSession] | None = None,
    settings: Settings | None = None,
    now: Any = None,
) -> list[str]:
    """Finalize live sessions idle beyond the inactivity timeout (Req 7.3).

    Out-of-band counterpart to the WS receive loop's in-connection idle timeout:
    it covers a session whose socket dropped and was never reconnected (no live
    coroutine to time out). Selects ``Processing_Job`` rows with ``status ==
    'live'`` and ``last_activity_at < now - live_inactivity_timeout_seconds`` and
    runs the shared exactly-once :func:`finalize_live_meeting` for each (so it
    cannot double-enqueue even if it races an explicit end). Returns the list of
    finalized meeting ids.
    """
    factory = session_factory if session_factory is not None else _get_session_factory()
    settings = settings if settings is not None else effective_settings(factory)
    now = now if now is not None else utcnow()
    cutoff = now - timedelta(seconds=settings.live_inactivity_timeout_seconds)

    session = factory()
    try:
        meeting_ids = list(
            session.execute(
                select(ProcessingJob.meeting_id).where(
                    ProcessingJob.status == JobStatus.LIVE.value,
                    ProcessingJob.last_activity_at.is_not(None),
                    ProcessingJob.last_activity_at < cutoff,
                )
            ).scalars()
        )
    finally:
        session.close()

    finalized: list[str] = []
    for meeting_id in meeting_ids:
        try:
            await finalize_live_meeting(meeting_id, session_factory=factory)
            finalized.append(meeting_id)
        except Exception:  # pragma: no cover - defensive; one bad row must not stop the sweep
            logger.exception("inactivity sweep failed to finalize %s", meeting_id)
    return finalized
