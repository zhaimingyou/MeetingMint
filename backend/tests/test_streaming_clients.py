"""Unit tests for the Streaming_ASR_Client boundary + mock + factory (task 3).

Focused example/edge tests complementing the property tests in
``tests/test_streaming_properties.py``. The streaming client methods are async,
and the suite does not use pytest-asyncio, so coroutines are driven with
``asyncio.run`` (the offline mock holds no loop-bound state).
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from app.clients.streaming.base import StreamingAsrClient, StreamingAsrSession
from app.clients.streaming.factory import make_streaming_asr_client
from app.clients.streaming.mock import (
    MockStreamingAsrClient,
    MockStreamingAsrSession,
)


def _drive(increments: list[bytes]) -> list[list[dict]]:
    """Open a fresh mock session and push every increment, collecting results."""

    async def go() -> list[list[dict]]:
        client = MockStreamingAsrClient()
        session = client.open_session(session_id="m1", input_encoding="webm/opus")
        out: list[list[dict]] = []
        for inc in increments:
            out.append(await session.push(inc))
        await session.aclose()
        return out

    return asyncio.run(go())


def test_mock_conforms_to_protocols() -> None:
    client = MockStreamingAsrClient()
    assert isinstance(client, StreamingAsrClient)
    session = client.open_session(session_id="s", input_encoding="webm/opus")
    assert isinstance(session, (MockStreamingAsrSession, StreamingAsrSession))


def test_open_session_ignores_header_bytes_and_needs_no_endpoint() -> None:
    client = MockStreamingAsrClient()
    # header_bytes provided or not, both work and require no configuration.
    s1 = client.open_session(session_id="s", input_encoding="webm/opus")
    s2 = client.open_session(
        session_id="s", input_encoding="webm/opus", header_bytes=b"\x1aE\xdf\xa3"
    )
    assert isinstance(s1, MockStreamingAsrSession)
    assert isinstance(s2, MockStreamingAsrSession)


def test_push_produces_finalized_then_optional_partial_with_monotonic_time() -> None:
    results = _drive([b"chunk-a", b"chunk-b", b"chunk-c"])
    assert len(results) == 3
    prev_end = 0.0
    for per_push in results:
        assert len(per_push) == 1  # one incremental update per push
        segments = per_push[0]["segments"]
        finals = [s for s in segments if s["is_final"]]
        partials = [s for s in segments if not s["is_final"]]
        assert len(finals) == 1  # exactly one finalized per push
        assert len(partials) <= 1  # at most one trailing partial
        fin = finals[0]
        assert fin["start"] >= 0.0
        assert fin["end"] >= fin["start"]
        # Finalized timestamps advance monotonically across pushes (contiguous).
        assert fin["start"] >= prev_end - 1e-9
        prev_end = fin["end"]
        # Any partial sits after the finalized segment.
        for p in partials:
            assert p["start"] >= fin["end"] - 1e-9
            assert p["end"] >= p["start"]
        # speaker is None or a non-empty string.
        for s in segments:
            assert s["speaker"] is None or (
                isinstance(s["speaker"], str) and s["speaker"]
            )


def test_factory_returns_mock_for_mock_impl() -> None:
    settings = SimpleNamespace(streaming_asr_impl="mock", streaming_asr_endpoint_url=None)
    client = make_streaming_asr_client(settings)  # type: ignore[arg-type]
    assert isinstance(client, MockStreamingAsrClient)


def test_factory_rejects_unknown_impl() -> None:
    settings = SimpleNamespace(
        streaming_asr_impl="bogus", streaming_asr_endpoint_url=None
    )
    with pytest.raises(ValueError, match="bogus"):
        make_streaming_asr_client(settings)  # type: ignore[arg-type]
