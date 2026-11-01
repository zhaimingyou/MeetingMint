"""Unit tests for the WebSocket streaming-ASR adapter (including reconnect)."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.clients.streaming.base import StreamingAsrError
from app.clients.streaming.websocket import (
    WebSocketStreamingAsrSession,
    _to_result,
    _ws_is_open,
)


def test_to_result_preserves_speaker_on_final_message() -> None:
    result = _to_result(
        {"type": "final", "text": "你好", "duration": 1.2, "speaker": "A"},
        partial_cursor=0.0,
        final_cursor=0.0,
    )

    assert result is not None
    assert result["segments"][0]["speaker"] == "A"
    assert result["segments"][0]["is_final"] is True


def test_to_result_preserves_speaker_on_partial_message() -> None:
    result = _to_result(
        {"type": "partial", "text": "你好", "timestamp": 0.8, "speaker": "B"},
        partial_cursor=0.0,
        final_cursor=0.0,
    )

    assert result is not None
    assert result["segments"][0]["speaker"] == "B"
    assert result["segments"][0]["is_final"] is False


def test_ws_is_open_handles_state_enum_and_none() -> None:
    assert _ws_is_open(None) is False
    assert _ws_is_open(SimpleNamespace(state=SimpleNamespace(name="OPEN"))) is True
    assert _ws_is_open(SimpleNamespace(state=SimpleNamespace(name="CLOSED"))) is False
    assert _ws_is_open(SimpleNamespace()) is True  # no state -> assume open


class _FakeWs:
    """Minimal websockets-like client for unit tests."""

    def __init__(self, *, fail_sends: int = 0) -> None:
        self.state = SimpleNamespace(name="OPEN")
        self.fail_sends = fail_sends
        self.sent: list[bytes] = []
        self.closed = False

    async def send(self, data: bytes) -> None:
        if self.fail_sends > 0:
            self.fail_sends -= 1
            self.state = SimpleNamespace(name="CLOSED")
            # Match the real exception type used by the production path.
            import websockets.exceptions

            raise websockets.exceptions.ConnectionClosedError(None, None)
        self.sent.append(data)

    async def close(self) -> None:
        self.closed = True
        self.state = SimpleNamespace(name="CLOSED")

    def __aiter__(self):  # noqa: ANN204
        return self

    async def __anext__(self) -> str:
        await asyncio.sleep(3600)
        raise StopAsyncIteration


def _session(**kwargs: Any) -> WebSocketStreamingAsrSession:
    defaults = dict(
        ws_url="ws://example.test/stream",
        timeout_seconds=2.0,
        session_id="sess-1",
        input_encoding="webm/opus",
        header_bytes=b"HEADER",
    )
    defaults.update(kwargs)
    return WebSocketStreamingAsrSession(**defaults)


def test_push_reconnects_websocket_after_connection_closed() -> None:
    """After an upstream WS drop, the next push rebuilds and succeeds."""

    async def _run() -> None:
        sess = _session()
        first = _FakeWs(fail_sends=1)
        second = _FakeWs(fail_sends=0)
        connects = [first, second]

        async def _connect(*_a: Any, **_k: Any) -> _FakeWs:
            return connects.pop(0)

        fake_proc = MagicMock()
        fake_proc.returncode = None
        fake_proc.stdin = MagicMock()
        fake_proc.stdin.write = MagicMock()
        fake_proc.stdin.drain = AsyncMock()
        fake_proc.stdin.close = MagicMock()
        fake_proc.stdout = MagicMock()
        fake_proc.stdout.read = AsyncMock(return_value=b"")
        fake_proc.terminate = MagicMock()
        fake_proc.wait = AsyncMock()

        with (
            patch(
                "app.clients.streaming.websocket.ws_connect",
                new=AsyncMock(side_effect=_connect),
            ),
            patch(
                "app.clients.streaming.websocket.asyncio.create_subprocess_exec",
                new=AsyncMock(return_value=fake_proc),
            ),
            patch.object(
                WebSocketStreamingAsrSession,
                "_collect_pcm",
                return_value=b"\x00\x01" * 8,
            ),
        ):
            # First push: send fails, recover, second connect succeeds.
            results = await sess.push(b"cluster-1")
            assert results == []
            assert second.sent  # PCM went out on the reconnected socket
            assert first.closed or first.state.name == "CLOSED"

        await sess.aclose()

    asyncio.run(_run())


def test_push_raises_when_reconnect_also_fails() -> None:
    async def _run() -> None:
        sess = _session()

        async def _always_fail(*_a: Any, **_k: Any) -> None:
            raise OSError("refused")

        fake_proc = MagicMock()
        fake_proc.returncode = None
        fake_proc.stdin = MagicMock()
        fake_proc.stdin.write = MagicMock()
        fake_proc.stdin.drain = AsyncMock()
        fake_proc.stdout = MagicMock()
        fake_proc.stdout.read = AsyncMock(return_value=b"")

        with (
            patch(
                "app.clients.streaming.websocket.ws_connect",
                new=AsyncMock(side_effect=_always_fail),
            ),
            patch(
                "app.clients.streaming.websocket.asyncio.create_subprocess_exec",
                new=AsyncMock(return_value=fake_proc),
            ),
        ):
            with pytest.raises(StreamingAsrError, match="failed"):
                await sess.push(b"cluster-1")

        await sess.aclose()

    asyncio.run(_run())


def test_set_header_bytes_updates_rebuild_cache() -> None:
    sess = _session(header_bytes=None)
    assert sess._header_bytes is None
    sess.set_header_bytes(b"EBML")
    assert sess._header_bytes == b"EBML"
