"""Property-based tests for the HTTP ASR client's chunking & assembly (task 5.4).

This module holds the ``hypothesis`` property tests (>= 100 examples each) for
the three design properties owned by the ASR chunking/assembly logic, plus a
focused unit test for the per-request timeout abort (Req 5.1):

* **Property 10** -- chunking is contiguous, non-overlapping, and bounded
  (:func:`app.clients.chunking.compute_chunks`, Req 5.2);
* **Property 11** -- transcript assembly is globally ordered by start timestamp
  with each chunk's timestamps offset by that chunk's start
  (:meth:`app.clients.http.HttpAsrClient.transcribe`, Req 5.3);
* **Property 12** -- speaker labels are preserved exactly when present
  (same ``transcribe`` path, Req 5.4).

Property 13 (terminal ASR failure) lives in ``test_asr_failure_properties.py``
(task 5.3) and is *not* duplicated here.

All ASR I/O is exercised through the injectable ``chunk_transcriber`` /
``duration_prober`` seams, so no real ASR_Service, network, or ffmpeg is used.
The example/edge tests for the same code paths are in ``test_asr_chunking.py``
(task 5.1) and ``test_asr_transcribe.py`` (task 5.2); this module adds only the
universal-property coverage and the timeout unit test.
"""

from __future__ import annotations

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
def _settings(
    *,
    chunk_length_seconds: int = 300,
    max_retries: int = 1,
    request_timeout_seconds: int = 30,
    asr_impl: str = "mock",
    asr_endpoint_url: str | None = None,
) -> Settings:
    kwargs: dict = dict(
        bootstrap_admin_username="admin",
        bootstrap_admin_password="bootstrap-pass",
        redis_url="redis://localhost:6379/0",
        database_url="sqlite://",
        asr_impl=asr_impl,  # "mock" avoids requiring an endpoint URL
        asr_chunk_length_seconds=chunk_length_seconds,
        asr_max_retries=max_retries,
        asr_request_timeout_seconds=request_timeout_seconds,
    )
    if asr_endpoint_url is not None:
        kwargs["asr_endpoint_url"] = asr_endpoint_url
    return Settings(**kwargs)  # type: ignore[call-arg]


def _client(
    *,
    duration: float,
    chunk_transcriber,
    chunk_length_seconds: int = 300,
    max_retries: int = 1,
) -> HttpAsrClient:
    return HttpAsrClient(
        _settings(
            chunk_length_seconds=chunk_length_seconds,
            max_retries=max_retries,
        ),
        duration_prober=lambda _path: duration,
        chunk_transcriber=chunk_transcriber,
    )


# --------------------------------------------------------------------------- #
# Property 10
# --------------------------------------------------------------------------- #
# Feature: meeting-minutes-service, Property 10: Audio chunking is contiguous, non-overlapping, and bounded
# Validates: Requirements 5.2
@settings(max_examples=100)
@given(
    threshold=st.integers(min_value=30, max_value=600),
    # Cover both regimes: small durations (<= threshold -> single window) and
    # large ones (> threshold -> many windows), including non-integer seconds.
    duration=st.floats(
        min_value=0.001,
        max_value=10_000.0,
        allow_nan=False,
        allow_infinity=False,
    ),
)
def test_property_10_chunks_contiguous_nonoverlapping_bounded(threshold, duration):
    chunks = compute_chunks(duration, threshold)

    # A positive-duration audio always yields at least one window.
    assert chunks, "positive duration must produce at least one chunk"

    # Contiguous: starts at 0 and each window begins exactly where the prior ends
    # (this single chain of equalities encodes both gap-free and non-overlapping).
    assert chunks[0][0] == 0.0
    prev_end = 0.0
    for start, end in chunks:
        assert start == pytest.approx(prev_end)  # contiguous + non-overlapping
        assert end > start                       # no empty window
        assert end - start <= threshold + 1e-9   # bounded by the threshold
        prev_end = end

    # Exact coverage: the union of the windows is exactly [0, duration].
    assert chunks[-1][1] == pytest.approx(duration)


# Feature: meeting-minutes-service, Property 10: Audio chunking is contiguous, non-overlapping, and bounded
# Validates: Requirements 5.2
@given(threshold=st.integers(min_value=30, max_value=600))
def test_property_10_zero_duration_has_no_chunks(threshold):
    # The boundary case of the property: a zero-/non-positive-duration audio
    # carries nothing to transcribe, so the window set is empty and the union
    # covers the empty interval [0, 0] vacuously (documented in app.clients.chunking).
    assert compute_chunks(0.0, threshold) == []


# --------------------------------------------------------------------------- #
# Property 11
# --------------------------------------------------------------------------- #
# Feature: meeting-minutes-service, Property 11: Transcript assembly is globally ordered by start timestamp
# Validates: Requirements 5.3
@settings(max_examples=100)
@given(
    threshold=st.integers(min_value=30, max_value=600),
    n_chunks=st.integers(min_value=1, max_value=5),
    last_len=st.integers(min_value=1, max_value=600),
    data=st.data(),
)
def test_property_11_assembly_globally_ordered_and_offset(
    threshold, n_chunks, last_len, data
):
    # Build a duration that produces exactly ``n_chunks`` windows of length
    # ``threshold`` (the last of length ``last_len`` in (0, threshold]).
    last_len = min(last_len, threshold)
    duration = float(threshold * (n_chunks - 1) + last_len)
    chunks = compute_chunks(duration, threshold)
    assert len(chunks) == n_chunks  # sanity on the generated input space
    start_to_index = {start: idx for idx, (start, _end) in enumerate(chunks)}

    # For each chunk draw a list of fake CHUNK-RELATIVE segments with arbitrary
    # starts. Each segment gets a unique text id so we can verify, after
    # assembly, that its absolute start == chunk_start + chunk_relative_start.
    # expected[text] -> (chunk_start, rel_start, rel_end)
    expected: dict[str, tuple[float, float, float]] = {}
    per_chunk_segments: dict[int, list[dict]] = {}
    for idx, (chunk_start, _chunk_end) in enumerate(chunks):
        rel_segments = data.draw(
            st.lists(
                st.tuples(
                    st.floats(min_value=0.0, max_value=600.0, allow_nan=False, allow_infinity=False),
                    st.floats(min_value=0.0, max_value=600.0, allow_nan=False, allow_infinity=False),
                ),
                min_size=0,
                max_size=4,
            ),
            label=f"chunk-{idx}-segments",
        )
        seg_list: list[dict] = []
        for j, (a, b) in enumerate(rel_segments):
            rel_start, rel_end = (a, b) if a <= b else (b, a)
            text = f"seg-{idx}-{j}"
            seg_list.append(
                {"start": rel_start, "end": rel_end, "text": text, "speaker": None}
            )
            expected[text] = (chunk_start, rel_start, rel_end)
        per_chunk_segments[idx] = seg_list

    def transcriber(_path, start, _end):
        return per_chunk_segments[start_to_index[start]]

    client = _client(
        duration=duration,
        chunk_transcriber=transcriber,
        chunk_length_seconds=threshold,
    )
    assembled = client.transcribe("/data/audio/x.wav")

    # Every produced segment appears exactly once.
    assert len(assembled) == len(expected)

    # Globally ordered by ascending absolute start (Req 5.3).
    starts = [seg["start"] for seg in assembled]
    assert starts == sorted(starts)

    # Each timestamp was offset by its chunk's start position (Req 5.3).
    for seg in assembled:
        chunk_start, rel_start, rel_end = expected[seg["text"]]
        assert seg["start"] == pytest.approx(chunk_start + rel_start)
        assert seg["end"] == pytest.approx(chunk_start + rel_end)


# --------------------------------------------------------------------------- #
# Property 12
# --------------------------------------------------------------------------- #
# A per-segment speaker can be: a non-None string (diarized), an explicit None,
# or an entirely absent key. Properties 12 requires the assembled segment to
# carry the label exactly when the response provided a non-None one, else None.
_SPEAKER_KIND = st.sampled_from(["string", "none", "absent"])


# Feature: meeting-minutes-service, Property 12: Speaker labels are preserved exactly when present
# Validates: Requirements 5.4
@settings(max_examples=100)
@given(
    threshold=st.integers(min_value=30, max_value=600),
    n_chunks=st.integers(min_value=1, max_value=4),
    last_len=st.integers(min_value=1, max_value=600),
    data=st.data(),
)
def test_property_12_speaker_preserved_exactly_when_present(
    threshold, n_chunks, last_len, data
):
    last_len = min(last_len, threshold)
    duration = float(threshold * (n_chunks - 1) + last_len)
    chunks = compute_chunks(duration, threshold)
    assert len(chunks) == n_chunks
    start_to_index = {start: idx for idx, (start, _end) in enumerate(chunks)}

    # expected[text] -> the speaker value we expect on the assembled segment.
    expected_speaker: dict[str, str | None] = {}
    per_chunk_segments: dict[int, list[dict]] = {}
    for idx, _window in enumerate(chunks):
        specs = data.draw(
            st.lists(
                st.tuples(
                    _SPEAKER_KIND,
                    st.text(min_size=1, max_size=8),  # speaker label when "string"
                ),
                min_size=0,
                max_size=4,
            ),
            label=f"chunk-{idx}-speaker-specs",
        )
        seg_list: list[dict] = []
        for j, (kind, label) in enumerate(specs):
            text = f"seg-{idx}-{j}"
            seg: dict = {"start": float(j), "end": float(j) + 0.5, "text": text}
            if kind == "string":
                seg["speaker"] = label
                expected_speaker[text] = label
            elif kind == "none":
                seg["speaker"] = None
                expected_speaker[text] = None
            else:  # "absent" -> no speaker key at all
                expected_speaker[text] = None
            seg_list.append(seg)
        per_chunk_segments[idx] = seg_list

    def transcriber(_path, start, _end):
        return per_chunk_segments[start_to_index[start]]

    client = _client(
        duration=duration,
        chunk_transcriber=transcriber,
        chunk_length_seconds=threshold,
    )
    assembled = client.transcribe("/data/audio/x.wav")

    assert len(assembled) == len(expected_speaker)
    for seg in assembled:
        assert seg["speaker"] == expected_speaker[seg["text"]]


# --------------------------------------------------------------------------- #
# Per-request timeout abort (Req 5.1)
# --------------------------------------------------------------------------- #
def test_per_request_timeout_aborts_chunk_and_fails_as_asr_error(
    tmp_path, monkeypatch
):
    """A per-request timeout aborts the chunk POST, is retried, then terminal.

    Exercises the default ``_transcribe_chunk`` seam over a ``MockTransport``
    whose handler raises :class:`httpx.ReadTimeout` (the per-request timeout
    abort). It must:

    * construct the ``httpx.Client`` with an ``httpx.Timeout`` equal to
      ``asr_request_timeout_seconds`` (Req 5.1), and
    * surface the timeout through the retry loop as a terminal
      :class:`AsrError` identifying the failed chunk (no partial transcript).
    """
    audio = tmp_path / "audio.wav"
    audio.write_bytes(b"RIFFfake")

    attempts = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        attempts["n"] += 1
        # Simulate the per-request deadline being exceeded mid-request.
        raise httpx.ReadTimeout("read timed out", request=request)

    transport = httpx.MockTransport(handler)
    real_client = httpx.Client
    captured_timeout: dict = {}

    def _client_factory(*args, **kwargs):
        captured_timeout["timeout"] = kwargs.get("timeout")
        kwargs.setdefault("transport", transport)
        return real_client(*args, **kwargs)

    monkeypatch.setattr(httpx, "Client", _client_factory)

    settings_http = _settings(
        asr_impl="http",
        asr_endpoint_url="http://asr.internal:9000/transcribe",
        chunk_length_seconds=300,
        max_retries=2,
        request_timeout_seconds=7,
    )
    client = HttpAsrClient(
        settings_http,
        duration_prober=lambda _p: 5.0,
        audio_slicer=lambda path, _s, _e: path,
    )

    with pytest.raises(AsrError) as excinfo:
        client.transcribe(str(audio))

    # The per-request timeout was applied with the configured value (Req 5.1).
    timeout = captured_timeout["timeout"]
    assert isinstance(timeout, httpx.Timeout)
    assert timeout.read == pytest.approx(7.0)
    assert timeout.connect == pytest.approx(7.0)

    # The timeout aborted each attempt; the retry loop exhausted (max_retries+1)
    # and failed terminally for the single chunk, with no partial transcript.
    assert attempts["n"] == 3  # max_retries(2) + 1
    assert excinfo.value.chunk_index == 0
