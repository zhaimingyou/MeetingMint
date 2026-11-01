"""WebSocket implementation of the Streaming_ASR_Client.

Connects to the FunASR streaming WebSocket server (port 19101 on the DGX Spark)
which receives PCM int16 16kHz mono frames and returns JSON messages with
partial/final transcription results.

Protocol (server → client, JSON):
  {"type": "status",  "message": "listening"}
  {"type": "partial", "text": "...", "timestamp": 1.5, "speaker": "A"}
  {"type": "final",   "text": "...", "duration": 3.2, "speaker": "A"}
  {"type": "error",   "message": "..."}

Protocol (client → server):
  Binary audio frames (PCM int16, 16kHz, mono)
  Text: {"type": "reset"}

Like the HTTP client, this implementation uses a persistent ffmpeg subprocess
to decode the browser's WebM/Opus increment stream into raw PCM before sending
it over the WebSocket. The WebSocket receive path runs in a background task;
``push()`` drains any queued results after writing PCM.

Transport recovery
------------------
If the upstream WebSocket drops or ffmpeg exits mid-meeting, the next
``push()`` rebuilds the broken half (and retries once) so live preview can
recover without requiring the browser to reconnect the live session.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
from typing import TYPE_CHECKING, Any

from app.clients.streaming.base import StreamingAsrError
from app.clients.streaming.types import StreamingAsrResult, StreamingSegment

if TYPE_CHECKING:  # avoid a heavy/cyclic import at runtime
    from app.config import Settings

try:
    import websockets
    from websockets.asyncio.client import connect as ws_connect
except ImportError:  # pragma: no cover — dependency declared in pyproject.toml
    websockets = None  # type: ignore[assignment]
    ws_connect = None  # type: ignore[assignment]

__all__ = ["WebSocketStreamingAsrClient", "WebSocketStreamingAsrSession"]

logger = logging.getLogger(__name__)

# PCM read granularity for the background stdout drain.
_PCM_READ_SIZE = 16000 * 2  # ~0.5s of 16kHz mono s16le


def _to_result(
    msg: dict[str, Any],
    *,
    partial_cursor: float,
    final_cursor: float,
) -> StreamingAsrResult | None:
    """Convert a FunASR streaming server message into a StreamingAsrResult.

    ``partial`` messages produce a single Partial_Segment (is_final=False).
    ``final`` messages produce a single Finalized_Segment (is_final=True)
    with a start/end window derived from the session-level cursor so the
    relay can render it in order.

    ``status`` and ``error`` messages are consumed silently (errors are
    logged by the session); they produce no segments.
    """
    msg_type = msg.get("type")
    if msg_type == "partial":
        text = str(msg.get("text", ""))
        if not text:
            return None
        ts = float(msg.get("timestamp", partial_cursor))
        speaker = msg.get("speaker")
        segment: StreamingSegment = {
            "start": round(partial_cursor, 3),
            "end": round(partial_cursor + ts, 3),
            "text": text,
            "speaker": str(speaker) if speaker is not None else None,
            "is_final": False,
        }
        return {"segments": [segment]}

    if msg_type == "final":
        text = str(msg.get("text", ""))
        if not text:
            return None
        dur = float(msg.get("duration", 1.0))
        speaker = msg.get("speaker")
        segment = {
            "start": round(final_cursor, 3),
            "end": round(final_cursor + dur, 3),
            "text": text,
            "speaker": str(speaker) if speaker is not None else None,
            "is_final": True,
        }
        return {"segments": [segment]}

    return None


def _ws_is_open(ws: Any) -> bool:
    """Return True if *ws* looks like an open websockets client connection."""
    if ws is None:
        return False
    state = getattr(ws, "state", None)
    if state is None:
        # Older stubs / fakes without a state attribute are treated as open.
        return True
    # websockets.protocol.State.OPEN == 1; compare by name to avoid hard dep.
    name = getattr(state, "name", None)
    if name is not None:
        return name == "OPEN"
    try:
        return int(state) == 1
    except (TypeError, ValueError):
        return True


class WebSocketStreamingAsrSession:
    """A persistent-ffmpeg + WebSocket streaming session.

    On the first ``push`` it lazily:
    1. Starts ffmpeg to decode WebM/Opus → PCM s16le 16kHz mono.
    2. Opens a WebSocket to the FunASR streaming server.
    3. Launches a background task to read server messages into a queue.

    Mid-session, a dead WebSocket or exited ffmpeg is rebuilt so preview can
    recover after a transient upstream outage.
    """

    def __init__(
        self,
        *,
        ws_url: str,
        timeout_seconds: float,
        session_id: str,
        input_encoding: str,
        header_bytes: bytes | None,
    ) -> None:
        self._ws_url = ws_url
        self._timeout_seconds = float(timeout_seconds)
        self._session_id = session_id
        self._input_encoding = input_encoding
        self._header_bytes = header_bytes

        self._proc: asyncio.subprocess.Process | None = None
        self._reader: asyncio.Task[None] | None = None
        self._pcm: asyncio.Queue[bytes] = asyncio.Queue()

        self._ws: Any = None  # websockets.WebSocketClientProtocol
        self._recv_task: asyncio.Task[None] | None = None
        self._results: asyncio.Queue[StreamingAsrResult | Exception] = asyncio.Queue()

        self._partial_cursor: float = 0.0
        self._final_cursor: float = 0.0
        self._closed = False

    def set_header_bytes(self, header_bytes: bytes | None) -> None:
        """Cache the sequence-0 WebM header for ffmpeg rebuilds after reconnect."""
        self._header_bytes = header_bytes

    # -- ffmpeg decode (mirrors HttpStreamingAsrSession) -------------------

    async def _stop_ffmpeg(self) -> None:
        if self._reader is not None:
            self._reader.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._reader
            self._reader = None
        if self._proc is not None:
            with contextlib.suppress(Exception):
                if self._proc.stdin is not None:
                    self._proc.stdin.close()
            with contextlib.suppress(Exception):
                self._proc.terminate()
            with contextlib.suppress(Exception):
                await self._proc.wait()
            self._proc = None
        # Drop any PCM that belonged to the old decoder.
        self._pcm = asyncio.Queue()

    async def _ensure_ffmpeg(self) -> None:
        if self._proc is not None and self._proc.returncode is None:
            return
        if self._proc is not None:
            logger.warning(
                "streaming ASR session %s: rebuilding ffmpeg after exit code %s",
                self._session_id,
                self._proc.returncode,
            )
            await self._stop_ffmpeg()
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
        self._reader = asyncio.create_task(self._drain_stdout())
        # Header priming: on a rebuild after reconnect, feed the cached
        # sequence-0 header before any continuation bytes.
        if self._header_bytes:
            await self._write_stdin(self._header_bytes)

    async def _drain_stdout(self) -> None:
        assert self._proc is not None and self._proc.stdout is not None
        try:
            while True:
                chunk = await self._proc.stdout.read(_PCM_READ_SIZE)
                if not chunk:
                    break
                await self._pcm.put(chunk)
        except asyncio.CancelledError:
            raise
        except Exception:
            return

    async def _write_stdin(self, data: bytes) -> None:
        assert self._proc is not None and self._proc.stdin is not None
        try:
            self._proc.stdin.write(data)
            await self._proc.stdin.drain()
        except (BrokenPipeError, ConnectionResetError) as exc:
            raise StreamingAsrError("ffmpeg decode pipe closed") from exc

    def _collect_pcm(self) -> bytes:
        buf = bytearray()
        while True:
            try:
                buf.extend(self._pcm.get_nowait())
            except asyncio.QueueEmpty:
                break
        return bytes(buf)

    # -- WebSocket ----------------------------------------------------------

    async def _drop_ws(self) -> None:
        """Tear down the upstream WebSocket so the next ensure reconnects."""
        if self._recv_task is not None:
            self._recv_task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._recv_task
            self._recv_task = None
        if self._ws is not None:
            with contextlib.suppress(Exception):
                await self._ws.close()
            self._ws = None
        # Discard queued exceptions from the dead connection; keep any
        # transcript results already produced.
        kept: list[StreamingAsrResult] = []
        while True:
            try:
                item = self._results.get_nowait()
            except asyncio.QueueEmpty:
                break
            if not isinstance(item, Exception):
                kept.append(item)
        for item in kept:
            await self._results.put(item)

    async def _ensure_ws(self) -> None:
        if _ws_is_open(self._ws):
            return
        if self._ws is not None:
            logger.warning(
                "streaming ASR session %s: reconnecting WebSocket to %s",
                self._session_id,
                self._ws_url,
            )
            await self._drop_ws()
        if websockets is None or ws_connect is None:
            raise StreamingAsrError(
                "websockets library not installed; add 'websockets' to "
                "pyproject.toml dependencies"
            )
        try:
            self._ws = await asyncio.wait_for(
                ws_connect(self._ws_url, max_size=10 * 1024 * 1024),
                timeout=self._timeout_seconds,
            )
        except (TimeoutError, asyncio.TimeoutError) as exc:
            self._ws = None
            raise StreamingAsrError(
                f"WebSocket connect to {self._ws_url} timed out"
            ) from exc
        except Exception as exc:
            self._ws = None
            raise StreamingAsrError(
                f"WebSocket connect to {self._ws_url} failed: {exc}"
            ) from exc
        self._recv_task = asyncio.create_task(self._recv_loop())

    async def _recv_loop(self) -> None:
        """Background task: read JSON messages from the server, queue results."""
        assert self._ws is not None
        try:
            async for message in self._ws:
                if isinstance(message, bytes):
                    # Server only sends text; ignore binary.
                    continue
                try:
                    msg = json.loads(message)
                except json.JSONDecodeError:
                    continue
                result = _to_result(
                    msg,
                    partial_cursor=self._partial_cursor,
                    final_cursor=self._final_cursor,
                )
                if result is not None:
                    # Update cursors for time ordering.
                    for seg in result["segments"]:
                        if seg["is_final"]:
                            self._final_cursor = seg["end"]
                        else:
                            self._partial_cursor = max(
                                self._partial_cursor, seg["end"]
                            )
                    await self._results.put(result)
        except websockets.exceptions.ConnectionClosed:
            pass
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            await self._results.put(exc)

    async def _recover_transport(self) -> None:
        """Best-effort reset of broken WS / ffmpeg before a single retry."""
        await self._drop_ws()
        if self._proc is not None and self._proc.returncode is not None:
            await self._stop_ffmpeg()

    # -- push ---------------------------------------------------------------

    async def _push_once(self, audio_increment: bytes) -> list[StreamingAsrResult]:
        await self._ensure_ffmpeg()
        if self._proc is not None and self._proc.returncode is not None:
            raise StreamingAsrError(
                f"ffmpeg exited with code {self._proc.returncode}"
            )
        await self._ensure_ws()

        # Feed WebM bytes to ffmpeg, send decoded PCM to the WebSocket.
        await self._write_stdin(audio_increment)
        # Let the drain task surface freshly decoded PCM.
        await asyncio.sleep(0)
        pcm = self._collect_pcm()
        if pcm and self._ws is not None:
            try:
                await asyncio.wait_for(
                    self._ws.send(pcm),
                    timeout=self._timeout_seconds,
                )
            except (TimeoutError, asyncio.TimeoutError) as exc:
                await self._drop_ws()
                raise StreamingAsrError("WebSocket send timed out") from exc
            except websockets.exceptions.ConnectionClosed as exc:
                await self._drop_ws()
                raise StreamingAsrError(
                    f"WebSocket connection closed: {exc}"
                ) from exc

        # Drain any results the receive task has queued.
        results: list[StreamingAsrResult] = []
        while True:
            try:
                item = self._results.get_nowait()
            except asyncio.QueueEmpty:
                break
            if isinstance(item, Exception):
                await self._drop_ws()
                raise StreamingAsrError(str(item)) from item
            results.append(item)
        return results

    async def push(self, audio_increment: bytes) -> list[StreamingAsrResult]:
        if self._closed:
            raise StreamingAsrError("streaming session is closed")
        try:
            return await self._push_once(audio_increment)
        except StreamingAsrError as first:
            if self._closed:
                raise
            # One recovery attempt so a transient upstream blip does not pin
            # the live preview in "unavailable" for the rest of the meeting.
            logger.warning(
                "streaming ASR session %s push failed (%s); recovering once",
                self._session_id,
                first,
            )
            await self._recover_transport()
            try:
                return await self._push_once(audio_increment)
            except StreamingAsrError:
                raise first

    async def aclose(self) -> None:
        self._closed = True
        await self._stop_ffmpeg()
        await self._drop_ws()


class WebSocketStreamingAsrClient:
    """WebSocket implementation of the Streaming_ASR_Client.

    Stores the WebSocket endpoint URL from :class:`~app.config.Settings`;
    opens no connection at construction. Each :meth:`open_session` returns
    a :class:`WebSocketStreamingAsrSession` that lazily starts ffmpeg + a
    WebSocket connection on its first ``push``.
    """

    def __init__(self, settings: "Settings") -> None:
        ws_url = settings.streaming_asr_ws_url
        if not ws_url:
            raise StreamingAsrError(
                "streaming_asr_ws_url is not configured; cannot stream "
                "over WebSocket"
            )
        self.ws_url: str = ws_url
        self.timeout_seconds: float = float(settings.streaming_asr_timeout_seconds)

    def open_session(
        self,
        *,
        session_id: str,
        input_encoding: str,
        header_bytes: bytes | None = None,
    ) -> WebSocketStreamingAsrSession:
        return WebSocketStreamingAsrSession(
            ws_url=self.ws_url,
            timeout_seconds=self.timeout_seconds,
            session_id=session_id,
            input_encoding=input_encoding,
            header_bytes=header_bytes,
        )
