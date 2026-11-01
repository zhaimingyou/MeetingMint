"""Deterministic, offline mock Streaming_ASR_Client (task 3.2).

Lets the entire live path run and be tested with **no** real Streaming_ASR_Service
and **no** ffmpeg (Req 4.3, 4.4). It satisfies the
:class:`~app.clients.streaming.base.StreamingAsrClient` /
:class:`~app.clients.streaming.base.StreamingAsrSession` Protocols, performs
**no** network I/O, requires **no** endpoint URL, and ignores ``header_bytes``.

Determinism rule (mirrors :mod:`app.clients.mock`)
--------------------------------------------------
All variability is derived from a *stable* :func:`hashlib.sha256` digest of each
Audio_Increment's bytes -- never Python's randomized :func:`hash`. Two fresh
sessions fed the *same sequence* of increments therefore produce **byte-for-byte
identical** sequences of results, across repeated calls and across separate
process runs (Property 40). Absolute timestamps advance monotonically from a
per-session cursor, which is itself a pure function of the prior increments, so
the whole session output is a deterministic function of its input sequence.

Result shape
------------
Each :meth:`push` returns exactly one
:class:`~app.clients.streaming.types.StreamingAsrResult` containing a single
Finalized_Segment for the increment, optionally followed by a trailing
Partial_Segment (provisional preview) -- Finalized first, then the partial, all
ascending by ``start`` -- structurally identical to what an HTTP implementation
would yield (Req 3.4, 3.5). A speaker label is attached deterministically to
roughly half of the segments so the relay's speaker-passthrough (Req 3.6) is
exercised.
"""

from __future__ import annotations

import hashlib

from app.clients.streaming.types import StreamingAsrResult, StreamingSegment

__all__ = ["MockStreamingAsrClient", "MockStreamingAsrSession"]


# Fixed Chinese placeholder vocabularies (resemble live meeting speech while
# staying well-formed and deterministic).
_PHRASES: tuple[str, ...] = (
    "大家好，我们开始今天的会议。",
    "先同步一下最新的进展。",
    "这个问题我们需要再确认一下。",
    "请相关同事尽快跟进。",
    "预算方面基本没有变化。",
    "测试结果整体符合预期。",
    "好的，这一点就先这样。",
    "还有什么需要补充的吗？",
)

_PARTIALS: tuple[str, ...] = (
    "接下来",
    "我觉得",
    "那么我们",
    "另外",
    "关于这个",
)

_SPEAKERS: tuple[str, ...] = ("发言人A", "发言人B", "发言人C")


def _int_from_digest(digest: bytes, offset: int, size: int = 4) -> int:
    """Read ``size`` bytes from ``digest`` (wrapping) as an unsigned int."""
    out = 0
    n = len(digest)
    for i in range(size):
        out = (out << 8) | digest[(offset + i) % n]
    return out


def _bounded(digest: bytes, offset: int, low: int, high: int) -> int:
    """Map digest bytes into the inclusive range ``[low, high]`` deterministically."""
    if high <= low:
        return low
    span = high - low + 1
    return low + (_int_from_digest(digest, offset) % span)


class MockStreamingAsrSession:
    """Offline, deterministic streaming session (Req 4.3, 4.4).

    Holds only a monotonic time ``_cursor`` (seconds from session start). No
    socket, no decode, no endpoint. ``header_bytes`` (if any) is ignored.
    """

    def __init__(self) -> None:
        self._cursor: float = 0.0

    async def push(self, audio_increment: bytes) -> list[StreamingAsrResult]:
        """Return one deterministic incremental result for ``audio_increment``."""
        digest = hashlib.sha256(audio_increment).digest()

        # Finalized segment for this increment: 1.0..6.0s long, contiguous.
        duration = _bounded(digest, 0, 10, 60) / 10.0
        start = round(self._cursor, 3)
        end = round(self._cursor + duration, 3)
        text = _PHRASES[_int_from_digest(digest, 8) % len(_PHRASES)]
        speaker: str | None = (
            _SPEAKERS[_int_from_digest(digest, 16) % len(_SPEAKERS)]
            if _int_from_digest(digest, 12) % 2 == 0
            else None
        )
        finalized: StreamingSegment = {
            "start": start,
            "end": end,
            "text": text,
            "speaker": speaker,
            "is_final": True,
        }
        self._cursor = end

        segments: list[StreamingSegment] = [finalized]

        # Optional trailing partial (provisional preview of the next utterance),
        # positioned after the finalized segment. Emitted ~2/3 of the time.
        if _int_from_digest(digest, 20) % 3 != 0:
            partial_duration = _bounded(digest, 24, 5, 30) / 10.0  # 0.5..3.0
            partial_text = _PARTIALS[_int_from_digest(digest, 28) % len(_PARTIALS)]
            partial: StreamingSegment = {
                "start": round(end, 3),
                "end": round(end + partial_duration, 3),
                "text": partial_text,
                "speaker": speaker,
                "is_final": False,
            }
            segments.append(partial)

        return [{"segments": segments}]

    async def aclose(self) -> None:
        """No resources to release for the offline mock."""
        return None


class MockStreamingAsrClient:
    """Offline, deterministic Streaming_ASR_Client (Req 4.3, 4.4).

    Takes no constructor arguments and ignores any endpoint configuration: it
    runs when ``streaming_asr_endpoint_url`` is absent. Each
    :meth:`open_session` returns a fresh :class:`MockStreamingAsrSession`.
    """

    def open_session(
        self,
        *,
        session_id: str,
        input_encoding: str,
        header_bytes: bytes | None = None,
    ) -> MockStreamingAsrSession:
        """Return a fresh deterministic session (no I/O; ``header_bytes`` ignored)."""
        return MockStreamingAsrSession()
