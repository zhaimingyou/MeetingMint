"""HTTP implementation of the Streaming_ASR_Client (task 4).

``HttpStreamingAsrClient`` is the integration seam to a real, locally-deployed
streaming-capable ASR service. Like the batch :class:`app.clients.http.HttpAsrClient`
it stores configuration at construction and opens **no** connection there; all
network/subprocess work happens inside a session's :meth:`push`.

Decode reality (design "Server-side audio decode" + FIX 5/D)
------------------------------------------------------------
The browser MediaRecorder emits a single WebM/Opus byte stream in which only the
**sequence-0 fragment carries the container header**; later fragments are bare
cluster continuations that cannot be decoded on their own. So a session owns a
**persistent ffmpeg** process (``ffmpeg -i pipe:0 -f s16le -ar 16000 -ac 1
pipe:1``) fed the continuous increment byte stream, with a background task
draining decoded PCM. On a **rebuild after a reconnect** the WS handler passes
the cached sequence-0 ``header_bytes`` to :meth:`StreamingAsrClient.open_session`;
the session writes that header to the fresh ffmpeg **before** the resumed
continuation bytes so decoding survives the reconnect. If no header is available
when one is required, the session reports the preview **unavailable** (raises
:class:`StreamingAsrError`) rather than emitting garbage -- acceptable because,
per decision D1, the live preview is not the Transcript of record.

> Caveat (FIX D): the cached sequence-0 fragment is the WebM init segment
> *followed by* the first audio cluster(s), not a clean init segment, so
> re-priming replays the opening audio into the rebuilt preview -- a
> preview-only artifact under D1.

Failure policy (Req 5.2)
------------------------
Any transport/timeout/decode failure raises :class:`StreamingAsrError`. The live
WS handler catches it on the best-effort side path, marks live transcription
unavailable, and keeps the session live and the audio persisting (graceful
degradation). The authoritative Transcript is produced later by the batch pass.

Service contract (adaptable, like the batch client)
----------------------------------------------------
On each push the newly-decoded PCM (s16le, 16 kHz, mono) is POSTed as
``application/octet-stream`` to ``streaming_asr_endpoint_url`` and the JSON
response ``{"segments": [{"start","end","text","speaker"?,"is_final"?}, ...]}``
(or a bare list) is parsed into incremental results. The exact wire shape is
isolated here so adapting to a concrete service (FunASR streaming, etc.) is a
change confined to this module, mirroring how the batch HTTP client is adapted.
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import Mapping, Sequence
from typing import TYPE_CHECKING, Any

import httpx

from app.clients.streaming.base import StreamingAsrError
from app.clients.streaming.types import StreamingAsrResult, StreamingSegment

if TYPE_CHECKING:  # avoid a heavy/cyclic import at runtime
    from app.config import Settings

__all__ = ["HttpStreamingAsrClient", "HttpStreamingAsrSession"]

# PCM read granularity for the background stdout drain.
_PCM_READ_SIZE = 16000 * 2  # ~0.5s of 16kHz mono s16le


def _parse_segments(payload: Any) -> list[StreamingSegment]:
    """Coerce a streaming-ASR response into a list of :class:`StreamingSegment`.

    Accepts ``{"segments": [...]}`` or a bare list of segment objects. ``speaker``
    defaults to ``None`` (Req 3.6 -- carried only when supplied) and ``is_final``
    defaults to ``True`` (a result without the flag is treated as finalized).

    :raises StreamingAsrError: if the body is not a list / ``{"segments": [...]}``
        or a segment is not an object -- a malformed body is a streaming failure
        that degrades the preview gracefully.
    """
    segments = payload.get("segments") if isinstance(payload, Mapping) else payload
    if not isinstance(segments, Sequence) or isinstance(segments, (str, bytes)):
        raise StreamingAsrError(
            "streaming ASR returned a malformed response: expected a list of "
            "segments or {'segments': [...]}"
        )
    out: list[StreamingSegment] = []
    for seg in segments:
        if not isinstance(seg, Mapping):
            raise StreamingAsrError(
                "streaming ASR returned a malformed segment "
                f"(expected an object, got {type(seg).__name__})"
            )
        speaker = seg.get("speaker")
        out.append(
            {
                "start": float(seg.get("start", 0.0)),
                "end": float(seg.get("end", 0.0)),
                "text": str(seg.get("text", "")),
                "speaker": speaker if speaker is not None else None,
                "is_final": bool(seg.get("is_final", True)),
            }
        )
    return out


class HttpStreamingAsrSession:
    """A persistent-ffmpeg streaming session relaying PCM to the ASR service."""

    def __init__(
        self,
        *,
        endpoint_url: str,
        timeout_seconds: float,
        session_id: str,
        input_encoding: str,
        header_bytes: bytes | None,
    ) -> None:
        self._endpoint_url = endpoint_url
        self._timeout_seconds = float(timeout_seconds)
        self._session_id = session_id
        self._input_encoding = input_encoding
        self._header_bytes = header_bytes
        self._proc: asyncio.subprocess.Process | None = None
        self._reader: asyncio.Task[None] | None = None
        self._pcm: asyncio.Queue[bytes] = asyncio.Queue()
        self._client: httpx.AsyncClient | None = None
        self._closed = False

    def set_header_bytes(self, header_bytes: bytes | None) -> None:
        """Cache the sequence-0 container header for ffmpeg rebuilds."""
        self._header_bytes = header_bytes

    async def _ensure_started(self) -> None:
        if self._proc is not None:
            return
        try:
            self._proc = await asyncio.create_subprocess_exec(
                "ffmpeg",
                "-nostdin",
                "-hide_banner",
                "-loglevel",
                "error",
                "-i",
                "pipe:0",
                "-f",
                "s16le",
                "-ar",
                "16000",
                "-ac",
                "1",
                "pipe:1",
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
            )
        except FileNotFoundError as exc:
            raise StreamingAsrError("ffmpeg executable not found") from exc
        self._client = httpx.AsyncClient(timeout=httpx.Timeout(self._timeout_seconds))
        self._reader = asyncio.create_task(self._drain_stdout())
        # Header priming (FIX 5): on a rebuild after reconnect, feed the cached
        # sequence-0 header before any continuation bytes so the decoder syncs.
        if self._header_bytes:
            await self._write_stdin(self._header_bytes)

    async def _drain_stdout(self) -> None:
        """Continuously read decoded PCM into the queue until ffmpeg EOFs."""
        assert self._proc is not None and self._proc.stdout is not None
        try:
            while True:
                chunk = await self._proc.stdout.read(_PCM_READ_SIZE)
                if not chunk:
                    break
                await self._pcm.put(chunk)
        except asyncio.CancelledError:  # pragma: no cover - shutdown path
            raise
        except Exception:  # pragma: no cover - defensive; surfaced on next push
            return

    async def _write_stdin(self, data: bytes) -> None:
        assert self._proc is not None and self._proc.stdin is not None
        try:
            self._proc.stdin.write(data)
            await self._proc.stdin.drain()
        except (BrokenPipeError, ConnectionResetError) as exc:
            raise StreamingAsrError("ffmpeg decode pipe closed") from exc

    def _collect_pcm(self) -> bytes:
        """Drain whatever PCM is currently available without blocking."""
        buf = bytearray()
        while True:
            try:
                buf.extend(self._pcm.get_nowait())
            except asyncio.QueueEmpty:
                break
        return bytes(buf)

    async def push(self, audio_increment: bytes) -> list[StreamingAsrResult]:
        if self._closed:
            raise StreamingAsrError("streaming session is closed")
        await self._ensure_started()
        if self._proc is not None and self._proc.returncode is not None:
            raise StreamingAsrError(
                f"ffmpeg exited with code {self._proc.returncode}"
            )
        await self._write_stdin(audio_increment)
        # Give the drain task a moment to surface freshly decoded PCM, bounded
        # by the configured per-result timeout so a stall degrades gracefully.
        await asyncio.sleep(0)
        pcm = self._collect_pcm()
        if not pcm:
            return []
        assert self._client is not None
        try:
            response = await asyncio.wait_for(
                self._client.post(
                    self._endpoint_url,
                    content=pcm,
                    headers={
                        "Content-Type": "application/octet-stream",
                        "X-Session-Id": self._session_id,
                        "X-Sample-Rate": "16000",
                    },
                ),
                timeout=self._timeout_seconds,
            )
            response.raise_for_status()
            segments = _parse_segments(response.json())
        except (TimeoutError, asyncio.TimeoutError) as exc:
            raise StreamingAsrError("streaming ASR timed out") from exc
        except httpx.HTTPError as exc:
            raise StreamingAsrError(f"streaming ASR transport error: {exc}") from exc
        if not segments:
            return []
        return [{"segments": segments}]

    async def aclose(self) -> None:
        self._closed = True
        if self._reader is not None:
            self._reader.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._reader
        if self._proc is not None:
            with contextlib.suppress(Exception):
                if self._proc.stdin is not None:
                    self._proc.stdin.close()
            with contextlib.suppress(Exception):
                self._proc.terminate()
            with contextlib.suppress(Exception):
                await self._proc.wait()
        if self._client is not None:
            with contextlib.suppress(Exception):
                await self._client.aclose()


class HttpStreamingAsrClient:
    """HTTP implementation of :class:`~app.clients.streaming.base.StreamingAsrClient`.

    Stores the streaming transport configuration from
    :class:`~app.config.Settings`; opens no connection at construction. Each
    :meth:`open_session` returns a :class:`HttpStreamingAsrSession` that lazily
    starts ffmpeg + an httpx client on its first push.
    """

    def __init__(self, settings: "Settings") -> None:
        self.endpoint_url: str | None = (
            str(settings.streaming_asr_endpoint_url)
            if settings.streaming_asr_endpoint_url is not None
            else None
        )
        self.timeout_seconds: float = float(settings.streaming_asr_timeout_seconds)

    def open_session(
        self,
        *,
        session_id: str,
        input_encoding: str,
        header_bytes: bytes | None = None,
    ) -> HttpStreamingAsrSession:
        if self.endpoint_url is None:
            # http mode without a URL is prevented by startup validation
            # (app.startup); guard defensively so a misconfiguration is a clear
            # error rather than an httpx crash mid-meeting.
            raise StreamingAsrError(
                "streaming ASR endpoint URL is not configured; cannot stream "
                "over HTTP"
            )
        return HttpStreamingAsrSession(
            endpoint_url=self.endpoint_url,
            timeout_seconds=self.timeout_seconds,
            session_id=session_id,
            input_encoding=input_encoding,
            header_bytes=header_bytes,
        )
