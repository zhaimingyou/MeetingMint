"""Streaming ASR client boundary (realtime-meeting-transcription).

The pluggable, async, per-session boundary between the live WebSocket handler
and an external streaming speech-recognition service -- the streaming analogue
of the batch :mod:`app.clients` boundary. Like the batch ASR/LLM clients it has
interchangeable HTTP and deterministic-mock implementations selected by
configuration (``streaming_asr_impl``), so the whole live experience runs and is
tested with no real model present.
"""

from __future__ import annotations

from app.clients.streaming.base import (
    StreamingAsrClient,
    StreamingAsrError,
    StreamingAsrResult,
    StreamingAsrSession,
    StreamingSegment,
)
from app.clients.streaming.factory import make_streaming_asr_client
from app.clients.streaming.mock import (
    MockStreamingAsrClient,
    MockStreamingAsrSession,
)

__all__ = [
    "StreamingAsrClient",
    "StreamingAsrSession",
    "StreamingAsrError",
    "StreamingAsrResult",
    "StreamingSegment",
    "MockStreamingAsrClient",
    "MockStreamingAsrSession",
    "make_streaming_asr_client",
]
