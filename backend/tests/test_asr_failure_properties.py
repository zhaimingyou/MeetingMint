"""Terminal-failure tests for HttpAsrClient.transcribe (task 5.3).

This module covers the ASR_Client's *terminal* failure semantics (Req 5.5, 5.6):

* the design **Property 13** as a ``hypothesis`` property test (>= 100 examples):
  for any number of chunks, any chosen failing chunk, any retryable failure mode
  (unreachable / per-request timeout / error), and any ``max_retries`` in
  ``[0, 5]``, ``transcribe`` *raises* :class:`AsrError` (never returns a partial
  transcript), the error identifies the failing chunk via ``chunk_index``, and
  the failing chunk is attempted exactly ``max_retries + 1`` times;
* example tests for the malformed-response **terminal** decision, each individual
  httpx failure mode, first/middle/last failing-chunk index correctness, and the
  guarantee that earlier successful chunks do not leak as partial output.

All ASR I/O is exercised through the injectable ``chunk_transcriber`` /
``duration_prober`` seams, so no real ASR_Service, network, or ffmpeg is used.

Malformed-response decision (documented):
  A malformed response body (or a missing endpoint URL) surfaces as an
  :class:`AsrError` from the chunk transcriber. Unlike a transport error, this is
  **deterministic** -- retrying the same request cannot make a malformed body
  well-formed -- so it is treated as terminal and is **not** retried. It is still
  attributed to the failing chunk (``chunk_index`` is stamped on the error) to
  satisfy Req 5.6. See ``test_malformed_response_is_terminal_no_retry``.
"""

from __future__ import annotations

import collections

import httpx
import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from app.clients.base import AsrError
from app.clients.chunking import compute_chunks
from app.clients.http import HttpAsrClient
from app.config import Settings


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def _settings(*, chunk_length_seconds: int, max_retries: int) -> Settings:
    return Settings(
        bootstrap_admin_username="admin",
        bootstrap_admin_password="bootstrap-pass",
        redis_url="redis://localhost:6379/0",
        database_url="sqlite://",
        asr_impl="mock",  # avoids requiring an endpoint URL at construction
        asr_chunk_length_seconds=chunk_length_seconds,
        asr_max_retries=max_retries,
    )  # type: ignore[call-arg]


def _client(
    *,
    duration: float,
    chunk_transcriber,
    chunk_length_seconds: int,
    max_retries: int,
) -> HttpAsrClient:
    return HttpAsrClient(
        _settings(chunk_length_seconds=chunk_length_seconds, max_retries=max_retries),
        duration_prober=lambda _path: duration,
        chunk_transcriber=chunk_transcriber,
    )


# The three Req 5.5 failure modes ("unreachable, per-request timeout, or error"),
# expressed as zero-arg factories building a fresh httpx exception each call. All
# are ``httpx.HTTPError`` subclasses, so all drive the in-client retry loop.
def _http_status_error() -> httpx.HTTPStatusError:
    request = httpx.Request("POST", "http://asr.internal:9000/transcribe")
    response = httpx.Response(503, request=request, text="unavailable")
    return httpx.HTTPStatusError("error status", request=request, response=response)


_FAILURE_MODES = [
    ("unreachable", lambda: httpx.ConnectError("connection refused")),
    ("connect-timeout", lambda: httpx.ConnectTimeout("connect timed out")),
    ("read-timeout", lambda: httpx.ReadTimeout("read timed out")),
    ("pool-timeout", lambda: httpx.PoolTimeout("pool timed out")),
    ("write-timeout", lambda: httpx.WriteTimeout("write timed out")),
    ("error-status", _http_status_error),
    ("remote-protocol", lambda: httpx.RemoteProtocolError("server hung up")),
]
_FAILURE_FACTORIES = [factory for _name, factory in _FAILURE_MODES]


def _valid_segment(text: str = "ok") -> dict:
    return {"start": 0.0, "end": 1.0, "text": text, "speaker": None}


# --------------------------------------------------------------------------- #
# Property 13
# --------------------------------------------------------------------------- #
# Feature: meeting-minutes-service, Property 13: Terminal ASR failure fails the job with no partial transcript
# Validates: Requirements 5.5, 5.6
@settings(max_examples=100)
@given(
    threshold=st.integers(min_value=30, max_value=600),
    n_chunks=st.integers(min_value=1, max_value=6),
    last_len=st.integers(min_value=1, max_value=600),
    max_retries=st.integers(min_value=0, max_value=5),
    failure_factory=st.sampled_from(_FAILURE_FACTORIES),
    data=st.data(),
)
def test_property_13_terminal_failure_identifies_chunk_no_partial(
    threshold, n_chunks, last_len, max_retries, failure_factory, data
):
    # Build a duration that produces exactly ``n_chunks`` chunks of length
    # ``threshold`` (the last one of length ``last_len`` in (0, threshold]).
    last_len = min(last_len, threshold)
    duration = float(threshold * (n_chunks - 1) + last_len)

    chunks = compute_chunks(duration, threshold)
    assert len(chunks) == n_chunks  # sanity on the generated input space
    start_to_index = {start: idx for idx, (start, _end) in enumerate(chunks)}

    # Pick which chunk repeatedly fails.
    failing_index = data.draw(st.integers(min_value=0, max_value=n_chunks - 1))

    attempts = collections.Counter()

    def transcriber(_path, start, _end):
        idx = start_to_index[start]
        attempts[idx] += 1
        if idx < failing_index:
            return [_valid_segment(f"chunk{idx}")]  # earlier chunks succeed
        if idx == failing_index:
            raise failure_factory()  # the chosen chunk fails on every attempt
        # transcribe processes chunks in order and raises at ``failing_index``,
        # so later chunks must never be reached.
        raise AssertionError("chunk after the failing index should not run")

    client = _client(
        duration=duration,
        chunk_transcriber=transcriber,
        chunk_length_seconds=threshold,
        max_retries=max_retries,
    )

    # transcribe must RAISE (not return) -> no partial transcript leaks.
    with pytest.raises(AsrError) as excinfo:
        client.transcribe("/data/audio/x.wav")

    # The terminal error identifies the failed chunk (Req 5.6).
    assert excinfo.value.chunk_index == failing_index
    # The failing chunk is attempted exactly max_retries + 1 times (Req 5.5):
    # retries are exhausted, not multiplied.
    assert attempts[failing_index] == max_retries + 1
    # Earlier chunks each succeeded on their first attempt (no spurious retries).
    for idx in range(failing_index):
        assert attempts[idx] == 1


# --------------------------------------------------------------------------- #
# Example: malformed-response terminal behavior (documented decision)
# --------------------------------------------------------------------------- #
def test_malformed_response_is_terminal_no_retry():
    # A malformed body surfaces as AsrError from the seam; it must NOT be retried
    # (deterministic) and must still identify the failing chunk.
    calls = {"n": 0}

    def transcriber(_path, _start, _end):
        calls["n"] += 1
        raise AsrError("ASR_Service returned a malformed chunk response")

    client = _client(
        duration=5.0,
        chunk_transcriber=transcriber,
        chunk_length_seconds=300,
        max_retries=3,  # would allow 4 attempts if this were retryable
    )
    with pytest.raises(AsrError) as excinfo:
        client.transcribe("/data/audio/x.wav")

    assert calls["n"] == 1  # terminal: failed immediately, no retry
    assert excinfo.value.chunk_index == 0  # stamped with the failing chunk


def test_malformed_response_on_later_chunk_reports_its_index():
    def transcriber(_path, start, _end):
        if start == 0.0:
            return [_valid_segment("chunk0")]
        raise AsrError("malformed body on chunk 1")

    # duration 400, threshold 300 -> chunk0 [0,300] ok, chunk1 [300,400] malformed.
    client = _client(
        duration=400.0,
        chunk_transcriber=transcriber,
        chunk_length_seconds=300,
        max_retries=2,
    )
    with pytest.raises(AsrError) as excinfo:
        client.transcribe("/data/audio/x.wav")
    assert excinfo.value.chunk_index == 1


def test_preexisting_chunk_index_on_asr_error_is_preserved():
    # If the seam already attributes a chunk_index, the retry wrapper keeps it.
    def transcriber(_path, _start, _end):
        raise AsrError("already attributed", chunk_index=7)

    client = _client(
        duration=5.0,
        chunk_transcriber=transcriber,
        chunk_length_seconds=300,
        max_retries=1,
    )
    with pytest.raises(AsrError) as excinfo:
        client.transcribe("/data/audio/x.wav")
    assert excinfo.value.chunk_index == 7


# --------------------------------------------------------------------------- #
# Example: each httpx failure mode exhausts retries -> terminal AsrError
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("name,factory", _FAILURE_MODES, ids=[n for n, _ in _FAILURE_MODES])
def test_each_failure_mode_raises_after_attempts(name, factory):
    attempts = {"n": 0}

    def transcriber(_path, _start, _end):
        attempts["n"] += 1
        raise factory()

    client = _client(
        duration=5.0,
        chunk_transcriber=transcriber,
        chunk_length_seconds=300,
        max_retries=2,
    )
    with pytest.raises(AsrError) as excinfo:
        client.transcribe("/data/audio/x.wav")

    assert attempts["n"] == 3  # max_retries + 1
    assert excinfo.value.chunk_index == 0
    # Descriptive message: chunk index + window + underlying cause.
    msg = str(excinfo.value)
    assert "chunk 0" in msg
    assert "[0.000, 5.000]" in msg


# --------------------------------------------------------------------------- #
# Example: failing-chunk index correctness (first / middle / last)
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("failing_index", [0, 1, 2])
def test_failing_chunk_index_first_middle_last(failing_index):
    # duration 900, threshold 300 -> chunks [0,300],[300,600],[600,900] (indices 0,1,2).
    threshold = 300
    duration = 900.0
    chunks = compute_chunks(duration, threshold)
    start_to_index = {start: idx for idx, (start, _e) in enumerate(chunks)}

    def transcriber(_path, start, _end):
        idx = start_to_index[start]
        if idx == failing_index:
            raise httpx.ConnectError("boom")
        return [_valid_segment(f"chunk{idx}")]

    client = _client(
        duration=duration,
        chunk_transcriber=transcriber,
        chunk_length_seconds=threshold,
        max_retries=1,
    )
    with pytest.raises(AsrError) as excinfo:
        client.transcribe("/data/audio/x.wav")
    assert excinfo.value.chunk_index == failing_index


# --------------------------------------------------------------------------- #
# Example: earlier successful chunks do not leak as partial output
# --------------------------------------------------------------------------- #
def test_earlier_success_does_not_leak_partial_transcript():
    # chunk0 succeeds with content, chunk1 fails terminally. The successful
    # chunk0 segments must NOT be returned -- transcribe raises instead.
    produced = []

    def transcriber(_path, start, _end):
        if start == 0.0:
            seg = _valid_segment("partial-should-not-leak")
            produced.append(seg)
            return [seg]
        raise httpx.ReadTimeout("slow")

    client = _client(
        duration=400.0,  # threshold 300 -> 2 chunks
        chunk_transcriber=transcriber,
        chunk_length_seconds=300,
        max_retries=0,
    )
    with pytest.raises(AsrError) as excinfo:
        client.transcribe("/data/audio/x.wav")

    # chunk0 ran and produced a segment, but transcribe raised on chunk1 so the
    # caller receives nothing (no partial transcript).
    assert produced  # chunk0 did run
    assert excinfo.value.chunk_index == 1
