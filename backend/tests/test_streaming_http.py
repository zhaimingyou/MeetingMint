"""Unit tests for HttpStreamingAsrClient construction + response parsing (task 4).

These cover the side-effect-free construction, the no-endpoint guard, and the
response coercion (``_parse_segments``) without ffmpeg or a real service. The
live ffmpeg decode + reconnect priming is exercised by the optional real-ffmpeg
integration test (task 4.2), skipped here.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.clients.streaming.base import StreamingAsrError
from app.clients.streaming.http import HttpStreamingAsrClient, _parse_segments


def _settings(url: str | None, timeout: float = 5.0) -> SimpleNamespace:
    return SimpleNamespace(
        streaming_asr_endpoint_url=url,
        streaming_asr_timeout_seconds=timeout,
    )


def test_construction_is_side_effect_free_and_stores_config() -> None:
    client = HttpStreamingAsrClient(_settings("http://asr.local:9000", timeout=7.0))  # type: ignore[arg-type]
    assert client.endpoint_url == "http://asr.local:9000"
    assert client.timeout_seconds == 7.0


def test_open_session_without_endpoint_raises() -> None:
    client = HttpStreamingAsrClient(_settings(None))  # type: ignore[arg-type]
    with pytest.raises(StreamingAsrError, match="endpoint URL is not configured"):
        client.open_session(session_id="s", input_encoding="webm/opus")


def test_parse_segments_object_form_defaults_speaker_and_final() -> None:
    segs = _parse_segments(
        {"segments": [{"start": 0.0, "end": 1.0, "text": "你好"}]}
    )
    assert len(segs) == 1
    assert segs[0]["speaker"] is None  # absent -> None (Req 3.6)
    assert segs[0]["is_final"] is True  # absent -> finalized


def test_parse_segments_bare_list_and_speaker_passthrough() -> None:
    segs = _parse_segments(
        [
            {"start": 0.0, "end": 1.0, "text": "a", "speaker": "发言人A", "is_final": False},
            {"start": 1.0, "end": 2.0, "text": "b"},
        ]
    )
    assert segs[0]["speaker"] == "发言人A"
    assert segs[0]["is_final"] is False
    assert segs[1]["speaker"] is None and segs[1]["is_final"] is True


def test_parse_segments_malformed_raises() -> None:
    with pytest.raises(StreamingAsrError):
        _parse_segments("not-a-list")
    with pytest.raises(StreamingAsrError):
        _parse_segments({"segments": ["not-an-object"]})
