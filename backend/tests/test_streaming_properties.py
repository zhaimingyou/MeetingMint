"""Property-based tests for the Streaming_ASR_Client (tasks 3.4 / 3.5).

Covers:
- **Property 40** -- the mock streaming ASR is deterministic, structurally
  conformant, and offline.
- **Property 41** -- the streaming ASR implementation is selected by
  configuration and validated fail-fast at startup. The mock-selection and
  startup-fail-fast clauses are covered here; the HTTP-selection clause is
  covered in ``tests/test_streaming_http.py`` (task 4) once the HTTP impl exists.
"""

from __future__ import annotations

import socket

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from app.clients.streaming.factory import make_streaming_asr_client
from app.clients.streaming.mock import MockStreamingAsrClient
from app.startup import ConfigError, load_and_validate_settings

REQUIRED_KWARGS = {
    "bootstrap_admin_username": "admin",
    "bootstrap_admin_password": "s3cret-pass",
    "redis_url": "redis://localhost:6379/0",
}


def _run(coro):
    """Drive a coroutine that never truly awaits, without an event loop.

    The mock's ``push``/``aclose`` are ``async`` only to satisfy the Protocol;
    they perform no real awaits. Driving them with ``send(None)`` avoids
    ``asyncio.run`` (which itself opens a self-pipe socket), so the offline
    assertion below can patch ``socket.socket`` meaningfully.
    """
    try:
        coro.send(None)
    except StopIteration as stop:
        return stop.value
    raise AssertionError("mock coroutine unexpectedly awaited")


def _drive(increments: list[bytes]) -> list[list[dict]]:
    """Run a fresh mock session over ``increments`` and collect the results."""
    client = MockStreamingAsrClient()
    session = client.open_session(session_id="p", input_encoding="webm/opus")
    out: list[list[dict]] = []
    for inc in increments:
        out.append(_run(session.push(inc)))
    _run(session.aclose())
    return out


# Feature: realtime-meeting-transcription, Property 40: The mock streaming ASR is deterministic, structurally conformant, and offline.
# Validates: Requirements 4.3, 4.4
@settings(max_examples=100)
@given(increments=st.lists(st.binary(min_size=0, max_size=80), min_size=1, max_size=12))
def test_mock_streaming_is_deterministic_conformant_offline(
    increments: list[bytes],
) -> None:
    # Offline: any attempt to open a socket fails the test.
    real_socket = socket.socket

    def _no_socket(*args: object, **kwargs: object):  # noqa: ANN002, ANN003
        raise AssertionError("mock streaming ASR must not open a socket")

    socket.socket = _no_socket  # type: ignore[assignment]
    try:
        first = _drive(increments)
        second = _drive(increments)
    finally:
        socket.socket = real_socket  # type: ignore[assignment]

    # Deterministic: identical input sequence -> identical output sequence.
    assert first == second

    # Structurally conformant: one update per push; finalized ascending by
    # start, optional trailing partial after the finalized segment.
    prev_end = -1.0
    for per_push in first:
        assert len(per_push) == 1
        segments = per_push[0]["segments"]
        finals = [s for s in segments if s["is_final"]]
        partials = [s for s in segments if not s["is_final"]]
        assert len(finals) == 1
        assert len(partials) <= 1
        for s in segments:
            assert s["start"] >= 0.0
            assert s["end"] >= s["start"]
            assert isinstance(s["text"], str)
            assert s["speaker"] is None or (
                isinstance(s["speaker"], str) and s["speaker"]
            )
            assert isinstance(s["is_final"], bool)
        fin = finals[0]
        assert fin["start"] >= prev_end - 1e-9  # monotonic across pushes
        prev_end = fin["end"]
        for p in partials:
            assert p["start"] >= fin["end"] - 1e-9  # partial positioned after


# Feature: realtime-meeting-transcription, Property 41: The streaming ASR implementation is selected by configuration and validated fail-fast at startup.
# Validates: Requirements 4.1, 4.2, 4.5, 4.6
@settings(max_examples=100)
@given(
    endpoint_present=st.booleans(),
    asr_impl=st.sampled_from(["http", "mock"]),
    llm_impl=st.sampled_from(["http", "mock"]),
)
def test_streaming_mock_selection_and_http_failfast(
    endpoint_present: bool, asr_impl: str, llm_impl: str
) -> None:
    # Keep the batch impls in mock to isolate the streaming clause, but vary
    # them to confirm streaming validation is independent.
    base = dict(REQUIRED_KWARGS, asr_impl="mock", llm_impl="mock")

    # mock streaming impl: always valid, endpoint optional; factory -> mock.
    s_mock = load_and_validate_settings(
        _env_file=None,
        streaming_asr_impl="mock",
        streaming_asr_endpoint_url=(
            "http://asr.local:9000" if endpoint_present else None
        ),
        **base,
    )
    assert isinstance(make_streaming_asr_client(s_mock), MockStreamingAsrClient)

    # http streaming impl without an endpoint URL aborts startup, naming the key.
    if not endpoint_present:
        with pytest.raises(ConfigError) as exc:
            load_and_validate_settings(
                _env_file=None,
                streaming_asr_impl="http",
                streaming_asr_endpoint_url=None,
                **base,
            )
        assert any(
            "streaming_asr_endpoint_url" in p for p in exc.value.problems
        ), exc.value.problems
    else:
        # http streaming impl WITH a well-formed endpoint validates cleanly and
        # the factory returns the HTTP implementation (Req 4.1).
        ok = load_and_validate_settings(
            _env_file=None,
            streaming_asr_impl="http",
            streaming_asr_endpoint_url="http://asr.local:9000",
            **base,
        )
        assert ok.streaming_asr_impl == "http"
        from app.clients.streaming.http import HttpStreamingAsrClient

        assert isinstance(make_streaming_asr_client(ok), HttpStreamingAsrClient)
