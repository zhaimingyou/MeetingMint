"""Streaming_ASR_Client factory selecting the implementation by config (task 3.3).

Mirrors :func:`app.clients.factory.make_asr_client`: returns the implementation
named by ``streaming_asr_impl`` in :class:`~app.config.Settings` -- the
deterministic mock (:mod:`app.clients.streaming.mock`) when ``"mock"``, the
HTTP implementation (:mod:`app.clients.streaming.http`, task 4) when ``"http"``,
or the WebSocket implementation (:mod:`app.clients.streaming.websocket`) when
``"websocket"`` -- with no code change required to switch (Req 4.1, 4.2).

The HTTP/WebSocket implementations are imported **lazily** inside their branches
so this factory (and the whole mock path) is importable and usable without the
``httpx``/``websockets``/ffmpeg-touching modules, keeping the mock-first
sequencing clean. The selector is constrained to
``Literal["http", "mock", "websocket"]`` by ``Settings`` so an unexpected value
still raises a clear ``ValueError`` rather than returning ``None``.
"""

from __future__ import annotations

from app.clients.streaming.base import StreamingAsrClient
from app.config import Settings, get_settings

__all__ = ["make_streaming_asr_client"]


def make_streaming_asr_client(settings: Settings | None = None) -> StreamingAsrClient:
    """Return the Streaming_ASR_Client implementation designated by config.

    :param settings: configuration to read ``streaming_asr_impl`` (and, for
        HTTP, the endpoint/timeout fields) from; defaults to
        :func:`app.config.get_settings` when ``None``.
    :returns: a :class:`~app.clients.streaming.mock.MockStreamingAsrClient` when
        ``streaming_asr_impl == "mock"``, otherwise an ``HttpStreamingAsrClient``
        configured from ``settings``.
    :raises ValueError: if ``streaming_asr_impl`` is an unrecognized value.
    """
    settings = settings if settings is not None else get_settings()
    if settings.streaming_asr_impl == "mock":
        from app.clients.streaming.mock import MockStreamingAsrClient

        return MockStreamingAsrClient()
    if settings.streaming_asr_impl == "http":
        # Lazy import: keeps the mock path free of the HTTP/ffmpeg module.
        from app.clients.streaming.http import HttpStreamingAsrClient

        return HttpStreamingAsrClient(settings)
    if settings.streaming_asr_impl == "websocket":
        # Lazy import: keeps the mock path free of the websockets/ffmpeg module.
        from app.clients.streaming.websocket import WebSocketStreamingAsrClient

        return WebSocketStreamingAsrClient(settings)
    raise ValueError(
        f"unknown streaming_asr_impl: {settings.streaming_asr_impl!r}"
    )
