"""Streaming_ASR_Client interface, typed shapes, and exception (task 3.1).

This is the *pluggable boundary* between the live WebSocket handler and the
external streaming speech-recognition service (design: "Streaming_ASR_Client
interface"). It mirrors :mod:`app.clients.base` (the batch ASR/LLM boundary) but
is **stateful per session** -- a streaming recognizer maintains rolling context
across :class:`StreamingAsrSession.push` calls -- and **async**, because it runs
on the API event loop alongside the WebSocket.

Shapes
------
* :class:`StreamingSegment` / :class:`StreamingAsrResult` -- the incremental
  result shapes, deliberately :class:`typing.TypedDict` (zero runtime weight,
  built straight from parsed JSON), matching the batch ``TranscriptSegment``
  style. ``is_final`` distinguishes a Finalized_Segment (stable) from a
  Partial_Segment (provisional, may be revised), and ``speaker`` is populated
  only when the service supplies diarization data (Req 3.6).

Interfaces (Protocols, ``@runtime_checkable``)
----------------------------------------------
* :class:`StreamingAsrSession` -- ``async push(audio_increment) -> results`` and
  ``async aclose()``. One session per Live_Meeting_Session connection.
* :class:`StreamingAsrClient` -- ``open_session(...)`` opens a session with **no**
  upstream I/O (side-effect-free construction; mock-friendly). ``header_bytes``
  carries the cached sequence-0 WebM/Opus container header so an HTTP impl can
  re-prime a fresh decoder after a reconnect (design FIX 5); the mock ignores it.

Exception
---------
* :class:`StreamingAsrError` -- a transport/timeout/service failure of the
  streaming client. Unlike the batch :class:`~app.clients.base.AsrError` (which
  fails the job), this is caught on the WS best-effort path and degrades the
  *preview* gracefully without ending the meeting (Req 5.2): the authoritative
  Transcript is still produced later by the batch pass (decision D1).
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from app.clients.streaming.types import StreamingAsrResult, StreamingSegment

__all__ = [
    "StreamingSegment",
    "StreamingAsrResult",
    "StreamingAsrSession",
    "StreamingAsrClient",
    "StreamingAsrError",
]


class StreamingAsrError(Exception):
    """A streaming ASR transport/timeout/service failure (Req 5.2).

    Raised by a :class:`StreamingAsrSession` when the Streaming_ASR_Service is
    unreachable, errors, or does not return a result within the configured
    streaming-ASR timeout. The live WS handler catches it on the best-effort
    side path, marks live transcription unavailable, and keeps the session live
    and the audio persisting (graceful degradation) -- it does **not** fail the
    meeting.
    """


@runtime_checkable
class StreamingAsrSession(Protocol):
    """A single live streaming-recognition session (one per connection)."""

    async def push(self, audio_increment: bytes) -> list[StreamingAsrResult]:
        """Feed one Audio_Increment; return zero or more incremental results.

        :param audio_increment: the raw captured fragment bytes (WebM/Opus for
            the browser MediaRecorder source).
        :returns: zero or more :class:`StreamingAsrResult` updates produced from
            this and prior increments. Within each result, Finalized_Segments
            precede an optional trailing Partial_Segment, ordered by ascending
            ``start`` (Req 3.4, 3.5).
        :raises StreamingAsrError: on a transport/timeout/service failure
            (Req 5.2); the caller degrades the preview gracefully.
        """
        ...

    async def aclose(self) -> None:
        """Release the session (close any upstream connection / decode pipe)."""
        ...


@runtime_checkable
class StreamingAsrClient(Protocol):
    """Streaming speech-to-text boundary (design: Streaming_ASR_Client).

    Implementations: ``HttpStreamingAsrClient`` (task 4) and
    :class:`~app.clients.streaming.mock.MockStreamingAsrClient` (task 3.2),
    selected at startup by
    :func:`~app.clients.streaming.factory.make_streaming_asr_client`.
    """

    def open_session(
        self,
        *,
        session_id: str,
        input_encoding: str,
        header_bytes: bytes | None = None,
    ) -> StreamingAsrSession:
        """Begin a streaming session. Opens no upstream connection until the
        first :meth:`StreamingAsrSession.push` (side-effect-free construction,
        so the mock needs no network and no endpoint URL).

        :param session_id: stable id for the Live_Meeting_Session (logging /
            upstream correlation).
        :param input_encoding: the increment container/codec hint
            (e.g. ``"webm/opus"``).
        :param header_bytes: the cached sequence-0 container header. On a
            **rebuild after reconnect** an HTTP impl primes a fresh decoder with
            it before feeding header-less continuation bytes (design FIX 5);
            when ``None`` and a header is required the impl reports the preview
            as unavailable rather than emitting garbage. The mock ignores it.
        """
        ...
