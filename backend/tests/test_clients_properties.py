"""Property-based tests for the client factories and mock clients (task 4.4).

These ``hypothesis`` property tests cover design Properties 28, 29, and 30 for
the pluggable ASR/LLM client boundary (Req 11.1-11.5). They are deliberately
distinct from the example-based tests in ``test_clients_factory.py`` (task 4.3)
and ``test_clients_mock.py`` (task 4.2): here we assert *universal* properties
across many generated inputs rather than fixed examples.

Each property test runs >= 100 examples and is tagged with the exact design
property text and its ``Validates`` requirement references.

Offline guarantee (Property 29): rather than a function-scoped fixture (which
``hypothesis`` rejects under ``@given``), the network is blocked inside the test
body via the :func:`_block_network` context manager, which patches
``socket.socket`` / ``socket.create_connection`` to raise and restores them
afterward. Any attempt by a mock client to open a socket therefore fails the
example.
"""

from __future__ import annotations

import socket
from contextlib import contextmanager
from typing import Iterator

from hypothesis import given, settings
from hypothesis import strategies as st

from app.clients import (
    AsrClient,
    HttpAsrClient,
    HttpLlmClient,
    LlmClient,
    MockAsrClient,
    MockLlmClient,
    make_asr_client,
    make_llm_client,
)
from app.clients.types import TranscriptInput
from app.config import Settings


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def _settings(**overrides: object) -> Settings:
    """Build a Settings with required fields satisfied and no .env/file reads."""
    base: dict[str, object] = {
        "bootstrap_admin_username": "admin",
        "bootstrap_admin_password": "bootstrap-pass",
        "redis_url": "redis://localhost:6379/0",
    }
    base.update(overrides)
    return Settings(_env_file=None, **base)  # type: ignore[arg-type]


@contextmanager
def _block_network() -> Iterator[None]:
    """Patch the socket layer so any network I/O raises, then restore it.

    Used to prove the mock clients are fully offline (Property 29) without a
    function-scoped fixture, which ``hypothesis`` disallows under ``@given``.
    """
    real_socket = socket.socket
    real_create_connection = socket.create_connection

    def _blocked(*args: object, **kwargs: object):  # pragma: no cover - only on violation
        raise AssertionError("mock client attempted network I/O")

    socket.socket = _blocked  # type: ignore[assignment]
    socket.create_connection = _blocked  # type: ignore[assignment]
    try:
        yield
    finally:
        socket.socket = real_socket  # type: ignore[assignment]
        socket.create_connection = real_create_connection  # type: ignore[assignment]


def _assert_asr_well_formed(segments: list) -> None:
    """Structural validity for an ASR transcript: ordered, contiguous, valid."""
    assert isinstance(segments, list)
    assert len(segments) >= 1
    starts = [s["start"] for s in segments]
    assert starts == sorted(starts)  # ascending by start (Req 5.3)
    prev_end = None
    for seg in segments:
        assert seg["start"] >= 0.0
        assert seg["end"] >= seg["start"]
        assert isinstance(seg["text"], str) and seg["text"]
        assert seg["speaker"] is None or isinstance(seg["speaker"], str)
        if prev_end is not None:
            assert seg["start"] == prev_end
        prev_end = seg["end"]


def _assert_minutes_well_formed(minutes: dict) -> None:
    """Structural validity for generated minutes (Req 6.2, 6.3)."""
    assert set(minutes) == {"summary", "topics", "decisions", "action_items"}
    assert isinstance(minutes["summary"], str) and minutes["summary"]
    assert isinstance(minutes["topics"], list)
    assert all(isinstance(t, str) and t for t in minutes["topics"])
    assert isinstance(minutes["decisions"], list)
    assert all(isinstance(d, str) and d for d in minutes["decisions"])
    assert isinstance(minutes["action_items"], list)
    for item in minutes["action_items"]:
        assert set(item) == {"description", "owner"}
        assert isinstance(item["description"], str) and item["description"]
        assert isinstance(item["owner"], str) and item["owner"]  # non-empty owner


# --------------------------------------------------------------------------- #
# Strategies
# --------------------------------------------------------------------------- #
# Diverse audio paths: unicode, empty, spaces, path-like, synthetic names.
_audio_paths = st.text(
    alphabet=st.characters(min_codepoint=0x20, max_codepoint=0x4FFF),
    min_size=0,
    max_size=80,
)

# A single transcript segment shape (the mock only reads start/end/text/speaker).
_segments = st.lists(
    st.fixed_dictionaries(
        {
            "start": st.floats(min_value=0.0, max_value=1e5, allow_nan=False, allow_infinity=False),
            "end": st.floats(min_value=0.0, max_value=1e5, allow_nan=False, allow_infinity=False),
            "text": st.text(max_size=120),  # includes unicode and empty strings
            "speaker": st.one_of(st.none(), st.text(max_size=20)),
        }
    ),
    max_size=12,  # includes the empty-segments case
)

def _make_transcript(segs: list, lang: object) -> dict:
    """Build a TranscriptInput, omitting ``language`` entirely when ``lang`` is the
    sentinel (exercises the no-language-hint path) and including it otherwise."""
    if lang is _NO_LANGUAGE:
        return {"segments": segs}
    return {"segments": segs, "language": lang}


_NO_LANGUAGE = object()  # sentinel: omit the optional ``language`` key

_transcripts = st.builds(
    _make_transcript,
    _segments,
    st.one_of(st.just(_NO_LANGUAGE), st.none(), st.sampled_from(["zh", "en", "zh-CN"])),
)

# Components for building valid HTTP endpoint Settings (Property 28).
_hosts = st.sampled_from(["192.0.2.20", "10.0.0.5", "asr.internal", "llm.internal", "host"])
_ports = st.integers(min_value=1, max_value=65535)
_timeouts = st.integers(min_value=1, max_value=600)
_chunk_lengths = st.integers(min_value=30, max_value=600)
_retries = st.integers(min_value=0, max_value=5)
_model_names = st.text(
    alphabet=st.characters(whitelist_categories=("Ll", "Lu", "Nd"), whitelist_characters=".-_"),
    min_size=1,
    max_size=20,
)


# --------------------------------------------------------------------------- #
# Property 28
# --------------------------------------------------------------------------- #
# Feature: meeting-minutes-service, Property 28: Client implementation is selected by configuration
# Validates: Requirements 11.1, 11.2
@settings(max_examples=100, deadline=None)
@given(
    kind=st.sampled_from(["asr", "llm"]),
    selector=st.sampled_from(["mock", "http"]),
    host=_hosts,
    port=_ports,
    timeout=_timeouts,
    chunk_len=_chunk_lengths,
    retries=_retries,
    model=_model_names,
)
def test_property_28_factory_selects_by_configuration(
    kind: str,
    selector: str,
    host: str,
    port: int,
    timeout: int,
    chunk_len: int,
    retries: int,
    model: str,
) -> None:
    """For any client kind and any valid selector, the factory returns the
    implementation designated by configuration (type only -- selection, not I/O)."""
    if kind == "asr":
        if selector == "http":
            cfg = _settings(
                asr_impl="http",
                asr_endpoint_url=f"http://{host}:{port}/asr",
                asr_request_timeout_seconds=timeout,
                asr_chunk_length_seconds=chunk_len,
                asr_max_retries=retries,
            )
            expected = HttpAsrClient
        else:
            cfg = _settings(asr_impl="mock")
            expected = MockAsrClient
        client = make_asr_client(cfg)
        assert isinstance(client, expected)
        assert isinstance(client, AsrClient)  # satisfies the Protocol
    else:
        if selector == "http":
            cfg = _settings(
                llm_impl="http",
                llm_endpoint_url=f"http://{host}:{port}/v1/chat/completions",
                llm_model_name=model,
                llm_request_timeout_seconds=timeout,
            )
            expected = HttpLlmClient
        else:
            cfg = _settings(llm_impl="mock")
            expected = MockLlmClient
        client = make_llm_client(cfg)
        assert isinstance(client, expected)
        assert isinstance(client, LlmClient)  # satisfies the Protocol


# --------------------------------------------------------------------------- #
# Property 29
# --------------------------------------------------------------------------- #
# Feature: meeting-minutes-service, Property 29: Mock clients are deterministic, well-formed, and offline
# Validates: Requirements 11.3, 11.4
@settings(max_examples=100, deadline=None)
@given(audio_path=_audio_paths)
def test_property_29_mock_asr_deterministic_wellformed_offline(audio_path: str) -> None:
    """For any input, the mock ASR client produces identical, well-formed,
    ordered segments on repeated calls without issuing any network request."""
    client = MockAsrClient()
    with _block_network():
        first = client.transcribe(audio_path)
        second = client.transcribe(audio_path)
    assert first == second  # deterministic
    _assert_asr_well_formed(first)


# Feature: meeting-minutes-service, Property 29: Mock clients are deterministic, well-formed, and offline
# Validates: Requirements 11.3, 11.4
@settings(max_examples=100, deadline=None)
@given(transcript=_transcripts)
def test_property_29_mock_llm_deterministic_wellformed_offline(
    transcript: TranscriptInput,
) -> None:
    """For any input, the mock LLM client produces identical, schema-valid
    minutes (summary + topics/decisions + owner-bearing action items) on
    repeated calls without issuing any network request."""
    client = MockLlmClient()
    with _block_network():
        first = client.generate_minutes(transcript)
        second = client.generate_minutes(transcript)
    assert first == second  # deterministic
    _assert_minutes_well_formed(first)


# --------------------------------------------------------------------------- #
# Property 30
# --------------------------------------------------------------------------- #
# Feature: meeting-minutes-service, Property 30: Mock-selected steps complete without an endpoint URL
# Validates: Requirements 11.5
@settings(max_examples=100, deadline=None)
@given(audio_path=_audio_paths, transcript=_transcripts)
def test_property_30_mock_steps_complete_without_endpoint_url(
    audio_path: str,
    transcript: TranscriptInput,
) -> None:
    """For any step whose client is configured as a mock, the step completes
    successfully even when the endpoint URL (and model name) is absent."""
    # Mock config carries NO asr_endpoint_url / llm_endpoint_url / llm_model_name.
    cfg = _settings(asr_impl="mock", llm_impl="mock")
    assert cfg.asr_endpoint_url is None
    assert cfg.llm_endpoint_url is None
    assert cfg.llm_model_name is None

    asr = make_asr_client(cfg)
    llm = make_llm_client(cfg)
    assert isinstance(asr, MockAsrClient)
    assert isinstance(llm, MockLlmClient)

    # The "steps" complete and return well-formed output with no endpoint.
    segments = asr.transcribe(audio_path)
    _assert_asr_well_formed(segments)

    minutes = llm.generate_minutes(transcript)
    _assert_minutes_well_formed(minutes)
